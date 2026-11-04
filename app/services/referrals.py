"""Реферальная система.

Награда (по умолчанию +7 дней LITE) начисляется пригласившему, когда приглашённый
не просто нажал «Подключиться», а реально воспользовался VPN: по данным Remnawave у него
есть первое подключение или израсходовано не меньше заданного объёма трафика.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import desc, func, select, update

from app.bot.ui import btn, kb
from app.core import subscription as core
from app.core.subscription import Tier
from app.db.models import Referral, User
from app.integrations.remnawave import RemnaError, usage_of
from app.utils import days_word, fmt_dt, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


class ReferralService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ регистрация
    async def register(self, new_user_id: int, ref_code: str) -> bool:
        """Привязывает нового пользователя к пригласившему (только пока он «чистый»)."""
        if not self.ctx.cfg.get("referral_enabled"):
            return False
        code = ref_code.strip().upper()
        async with self.ctx.session() as s:
            referrer = await s.scalar(select(User).where(User.ref_code == code))
            if referrer is None or referrer.id == new_user_id or referrer.is_banned:
                return False
            user = await s.get(User, new_user_id, with_for_update=True)
            if user is None or user.referrer_id is not None or user.trial_used or user.remna_id:
                return False
            user.referrer_id = referrer.id
            s.add(Referral(referrer_id=referrer.id, referred_id=user.id, status="pending"))
            await s.commit()
        return True

    # ------------------------------------------------------------------ награда
    async def _is_used(self, referred: User) -> bool:
        if not referred.remna_id:
            return False
        try:
            data = await self.ctx.remna.get_user(referred.remna_id)
        except RemnaError as e:
            log.warning("referral check %s: %s", referred.id, e)
            return False
        usage = usage_of(data)
        min_bytes = int(self.ctx.cfg.get("referral_min_mb")) * 1024 * 1024
        return bool(usage["first_connected_at"]) or max(usage["used"], usage["lifetime"]) >= min_bytes

    async def try_reward(self, referral_id: int, *, force: bool = False) -> bool:
        async with self.ctx.session() as s:
            ref = await s.get(Referral, referral_id)
            if ref is None or ref.status != "pending":
                return False
            referred = await s.get(User, ref.referred_id)
        if referred is None:
            return False
        if not force and not await self._is_used(referred):
            return False

        cfg = self.ctx.cfg
        days = int(cfg.get("referral_days"))
        tier = Tier(cfg.get("referral_tier"))
        cap = int(cfg.get("referral_max_rewards"))
        subs = self.ctx.subs
        referrer_id = ref.referrer_id
        new_state = None

        async with subs.lock(referrer_id):
            async with self.ctx.session() as s:
                r = await s.scalar(select(Referral).where(Referral.id == referral_id).with_for_update())
                if r is None or r.status != "pending":
                    return False
                referrer = await s.get(User, referrer_id, with_for_update=True)
                if referrer is None or referrer.is_banned:
                    r.status = "rejected"
                    await s.commit()
                    return False
                if cap > 0:
                    done = await s.scalar(
                        select(func.count()).select_from(Referral).where(
                            Referral.referrer_id == referrer_id, Referral.status == "rewarded"
                        )
                    )
                    if int(done or 0) >= cap:
                        r.status = "rejected"
                        await s.commit()
                        return False
                delta = timedelta(days=days)
                _, new_state = subs.apply_op(
                    s,
                    referrer,
                    lambda st, n: core.grant(st, tier, delta, n),
                    kind="referral",
                    tier=tier,
                    seconds=int(delta.total_seconds()),
                    meta={"referred": r.referred_id},
                )
                now = utcnow()
                r.status = "rewarded"
                r.rewarded_at = now
                r.reward_days = days
                await s.execute(
                    update(User).where(User.id == r.referred_id, User.connected_at.is_(None)).values(connected_at=now)
                )
                await s.commit()
            await subs.sync_inside_lock(referrer_id)

        await self.ctx.notify.send(
            referrer_id,
            self.ctx.texts.render("n_ref_reward", bonus=days_word(days), tier=tier.title, until=fmt_dt(new_state.total_end)),
            kb(btn("Моя подписка", "menu:sub")),
        )
        return True

    async def reject(self, referral_id: int) -> None:
        async with self.ctx.session() as s:
            await s.execute(
                update(Referral).where(Referral.id == referral_id, Referral.status == "pending").values(status="rejected")
            )
            await s.commit()

    async def on_user_connected(self, user_id: int) -> None:
        """Вызывается по вебхуку Remnawave (user.first_connected) — награждаем сразу."""
        async with self.ctx.session() as s:
            rid = await s.scalar(
                select(Referral.id).where(Referral.referred_id == user_id, Referral.status == "pending")
            )
        if rid:
            await self.try_reward(rid)

    async def check_pending(self, limit: int = 150) -> int:
        """Воркер: проверяет ожидающие рефералы (не старше 60 дней)."""
        cutoff = utcnow() - timedelta(days=60)
        async with self.ctx.session() as s:
            ids = list(
                (
                    await s.scalars(
                        select(Referral.id)
                        .join(User, User.id == Referral.referred_id)
                        .where(Referral.status == "pending", Referral.created_at > cutoff, User.remna_id.is_not(None))
                        .order_by(Referral.id)
                        .limit(limit)
                    )
                ).all()
            )
        rewarded = 0
        for rid in ids:
            try:
                if await self.try_reward(rid):
                    rewarded += 1
            except Exception:  # noqa: BLE001
                log.exception("referral %s", rid)
            await asyncio.sleep(0.05)
        return rewarded

    # ------------------------------------------------------------------ статистика
    async def stats(self, user_id: int) -> dict:
        async with self.ctx.session() as s:
            invited = await s.scalar(select(func.count()).select_from(Referral).where(Referral.referrer_id == user_id))
            rewarded = await s.scalar(
                select(func.count()).select_from(Referral).where(
                    Referral.referrer_id == user_id, Referral.status == "rewarded"
                )
            )
            days = await s.scalar(
                select(func.coalesce(func.sum(Referral.reward_days), 0)).where(
                    Referral.referrer_id == user_id, Referral.status == "rewarded"
                )
            )
        return {"invited": int(invited or 0), "connected": int(rewarded or 0), "days": int(days or 0)}

    async def overview(self) -> dict:
        async with self.ctx.session() as s:
            rows = (await s.execute(select(Referral.status, func.count()).group_by(Referral.status))).all()
            days = await s.scalar(
                select(func.coalesce(func.sum(Referral.reward_days), 0)).where(Referral.status == "rewarded")
            )
        by = {st: int(n) for st, n in rows}
        return {
            "total": sum(by.values()),
            "pending": by.get("pending", 0),
            "rewarded": by.get("rewarded", 0),
            "rejected": by.get("rejected", 0),
            "days": int(days or 0),
        }

    async def top_inviters(self, limit: int = 10) -> list[tuple[User, int, int]]:
        async with self.ctx.session() as s:
            rewarded = func.count().filter(Referral.status == "rewarded")
            res = await s.execute(
                select(User, func.count(Referral.id), rewarded)
                .join(Referral, Referral.referrer_id == User.id)
                .group_by(User.id)
                .order_by(desc(func.count(Referral.id)))
                .limit(limit)
            )
            return [(u, int(n), int(r)) for u, n, r in res.all()]

    async def children(self, user_id: int, limit: int = 20) -> list[tuple[Referral, User | None]]:
        async with self.ctx.session() as s:
            res = await s.execute(
                select(Referral, User)
                .outerjoin(User, User.id == Referral.referred_id)
                .where(Referral.referrer_id == user_id)
                .order_by(desc(Referral.id))
                .limit(limit)
            )
            return [(r, u) for r, u in res.all()]

    async def pending_list(self, limit: int = 15) -> list[tuple[Referral, User | None]]:
        async with self.ctx.session() as s:
            res = await s.execute(
                select(Referral, User)
                .outerjoin(User, User.id == Referral.referred_id)
                .where(Referral.status == "pending")
                .order_by(desc(Referral.id))
                .limit(limit)
            )
            return [(r, u) for r, u in res.all()]
