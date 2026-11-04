"""Сервис подписок.

Источник истины — наша БД (users.tier / expires_at / queued_lite_s). Remnawave — «исполнитель»:
после каждого изменения мы синхронизируем в панель срок, лимит устройств и сквады тарифа.
Если панель недоступна, пользователь помечается sync_pending и воркер повторяет синхронизацию.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.bot.ui import btn, kb
from app.core import subscription as core
from app.core.subscription import SubState, Tier, normalize
from app.db.models import Plan, SubEvent, User
from app.integrations.remnawave import RemnaError, RemnaNotFound
from app.utils import fmt_dt, iso_z, truncate, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.context import AppContext

log = logging.getLogger(__name__)

Op = Callable[[SubState, datetime], SubState]


class TrialError(Exception):
    """reason: disabled | used | active | panel"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SubscriptionService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ блокировки
    def lock(self, user_id: int) -> asyncio.Lock:
        lk = self._locks.get(user_id)
        if lk is None:
            if len(self._locks) > 5000:
                for k, v in list(self._locks.items()):
                    if not v.locked():
                        del self._locks[k]
            lk = self._locks[user_id] = asyncio.Lock()
        return lk

    # ------------------------------------------------------------------ состояние
    @staticmethod
    def state_of(u: User) -> SubState:
        return SubState(
            Tier(u.tier) if u.tier else None,
            u.expires_at,
            timedelta(seconds=int(u.queued_lite_s or 0)),
        )

    @classmethod
    def view(cls, u: User, now: datetime | None = None) -> SubState:
        """Актуальное состояние на текущий момент (даже если воркер ещё не продвинул строку)."""
        return normalize(cls.state_of(u), now or utcnow())

    @staticmethod
    def apply_to_row(u: User, st: SubState) -> None:
        u.tier = st.tier.value if st.tier else None
        u.expires_at = st.expires_at
        u.queued_lite_s = int(st.queued_lite.total_seconds())

    def apply_op(
        self,
        session: AsyncSession,
        user: User,
        op: Op,
        *,
        kind: str,
        tier: Tier | None = None,
        seconds: int = 0,
        amount: Decimal | None = None,
        actor_id: int | None = None,
        meta: dict | None = None,
    ) -> tuple[SubState, SubState]:
        """Применяет операцию к строке пользователя внутри чужой транзакции (без commit)."""
        now = utcnow()
        old = normalize(self.state_of(user), now)
        new = op(old, now)
        self.apply_to_row(user, new)
        user.sync_pending = True
        session.add(
            SubEvent(
                user_id=user.id,
                kind=kind,
                tier=tier.value if tier else None,
                seconds=seconds,
                amount=amount,
                actor_id=actor_id,
                meta=meta or {},
            )
        )
        return old, new

    async def mutate(
        self,
        user_id: int,
        op: Op,
        *,
        kind: str,
        tier: Tier | None = None,
        seconds: int = 0,
        amount: Decimal | None = None,
        actor_id: int | None = None,
        meta: dict | None = None,
        sync: bool = True,
    ) -> tuple[SubState, SubState]:
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                if user is None:
                    raise LookupError(f"user {user_id} not found")
                old, new = self.apply_op(
                    s, user, op, kind=kind, tier=tier, seconds=seconds, amount=amount, actor_id=actor_id, meta=meta
                )
                await s.commit()
            if sync:
                await self._sync_locked(user_id)
        return old, new

    # ------------------------------------------------------------------ публичные операции
    async def grant_days(
        self, user_id: int, tier: Tier, days: int, *, kind: str, actor_id: int | None = None, meta: dict | None = None
    ) -> tuple[SubState, SubState]:
        delta = timedelta(days=days)
        return await self.mutate(
            user_id,
            lambda st, now: core.grant(st, tier, delta, now),
            kind=kind,
            tier=tier,
            seconds=int(delta.total_seconds()),
            actor_id=actor_id,
            meta=meta,
        )

    async def remove_days(
        self, user_id: int, tier: Tier, days: int, *, actor_id: int | None = None, kind: str = "admin_remove"
    ) -> tuple[SubState, SubState]:
        delta = timedelta(days=days)
        return await self.mutate(
            user_id,
            lambda st, now: core.remove(st, tier, delta, now),
            kind=kind,
            tier=tier,
            seconds=-int(delta.total_seconds()),
            actor_id=actor_id,
        )

    async def set_exact(
        self, user_id: int, tier: Tier | None, days_from_now: int, *, actor_id: int | None = None
    ) -> tuple[SubState, SubState]:
        def op(_: SubState, now: datetime) -> SubState:
            return core.set_exact(tier, now + timedelta(days=days_from_now) if tier else None, now)

        return await self.mutate(
            user_id, op, kind="admin_set", tier=tier, seconds=days_from_now * 86400, actor_id=actor_id
        )

    # ------------------------------------------------------------------ пробный период
    async def activate_trial(self, user_id: int) -> SubState:
        cfg = self.ctx.cfg
        if not cfg.get("trial_enabled"):
            raise TrialError("disabled")
        tier = Tier(cfg.get("trial_tier"))
        days = int(cfg.get("trial_days"))
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                if user is None:
                    raise LookupError(user_id)
                if user.trial_used:
                    raise TrialError("used")
                now = utcnow()
                old = normalize(self.state_of(user), now)
                if old.is_active(now):
                    raise TrialError("active")
                new = core.grant(old, tier, timedelta(days=days), now)
                try:
                    info = await self._push(user, new, self.ctx.plans.get(tier))
                except RemnaError as e:
                    log.error("trial: панель недоступна для %s: %s", user_id, e)
                    raise TrialError("panel") from e
                self._apply_info(user, info)
                self.apply_to_row(user, new)
                user.sync_pending = False
                user.sync_error = None
                user.synced_at = now
                user.trial_used = True
                user.trial_at = now
                s.add(SubEvent(user_id=user_id, kind="trial", tier=tier.value, seconds=days * 86400, meta={}))
                await s.commit()
        return new

    # ------------------------------------------------------------------ синхронизация с Remnawave
    @staticmethod
    def _description(user: User) -> str:
        parts = [f"tg:{user.id}"]
        if user.username:
            parts.append(f"@{user.username}")
        if user.first_name:
            parts.append(user.first_name)
        return truncate(" ".join(parts), 240)

    @staticmethod
    def _extract(data: dict) -> dict:
        info: dict[str, Any] = {}
        if data.get("id") is not None:
            info["remna_id"] = int(data["id"])
        if data.get("shortUuid"):
            info["short_uuid"] = data["shortUuid"]
        if data.get("subscriptionUrl"):
            info["sub_url"] = data["subscriptionUrl"]
        if data.get("username"):
            info["remna_username"] = data["username"]
        return info

    @staticmethod
    def _apply_info(user: User, info: dict) -> None:
        for k, v in info.items():
            setattr(user, k, v)

    def _patch_fields(self, user: User, st: SubState, plan: Plan | None, now: datetime) -> dict:
        f: dict[str, Any] = {"telegramId": user.id, "description": self._description(user)}
        if st.is_active(now) and st.total_end is not None:
            f["expireAt"] = iso_z(st.total_end)
            f["status"] = "DISABLED" if user.is_banned else "ACTIVE"
            if plan is not None:
                f["hwidDeviceLimit"] = plan.device_limit
                if plan.squads:
                    f["activeInternalSquads"] = list(plan.squads)
        else:
            f["expireAt"] = iso_z(min(st.expires_at or now, now))
            if user.is_banned:
                f["status"] = "DISABLED"
        return f

    @staticmethod
    def _is_conflict(e: RemnaError) -> bool:
        msg = str(e).lower()
        return e.code == "A019" or e.status == 409 or "already exist" in msg

    async def _push(self, user: User, st: SubState, plan: Plan | None) -> dict:
        """Приводит пользователя в панели к состоянию ``st``. Возвращает данные для users.*"""
        remna = self.ctx.remna
        now = utcnow()
        if user.remna_id is None:
            if not st.is_active(now) or plan is None:
                return {}  # создавать в панели пока нечего
            username = f"fable_{user.id}"
            try:
                data = await remna.create_user(
                    username=username,
                    expire_at=st.total_end or now,
                    hwid_limit=plan.device_limit,
                    squads=list(plan.squads or []),
                    telegram_id=user.id,
                    description=self._description(user),
                )
            except RemnaError as e:
                if not self._is_conflict(e):
                    raise
                found = await remna.resolve_user(username=username)  # уже есть (например, после сбоя) — подхватываем
                if not found or found.get("id") is None:
                    raise
                data = await remna.update_user(int(found["id"]), **self._patch_fields(user, st, plan, now))
                data = {**found, **data}
            info = self._extract(data)
            if user.is_banned and info.get("remna_id"):
                await remna.disable_user(info["remna_id"])
            return info

        try:
            data = await remna.update_user(user.remna_id, **self._patch_fields(user, st, plan, now))
        except RemnaNotFound:
            log.warning("пользователь %s удалён из панели — создаю заново", user.id)
            user.remna_id = None
            user.short_uuid = None
            user.sub_url = None
            return await self._push(user, st, plan)
        return self._extract(data) if data else {}

    async def _sync_locked(self, user_id: int) -> bool:
        """Синхронизация; вызывать, удерживая self.lock(user_id)."""
        now = utcnow()
        async with self.ctx.session() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None:
                return False
            st = normalize(self.state_of(user), now)
            plan = self.ctx.plans.get(st.tier) if st.tier else None
            try:
                info = await self._push(user, st, plan)
            except RemnaError as e:
                user.sync_pending = True
                user.sync_error = str(e)[:500]
                await s.commit()
                log.warning("синхронизация %s не удалась: %s", user_id, e)
                return False
            self._apply_info(user, info)
            user.sync_pending = False
            user.sync_error = None
            user.synced_at = now
            await s.commit()
            return True

    async def sync_now(self, user_id: int) -> bool:
        async with self.lock(user_id):
            return await self._sync_locked(user_id)

    async def sync_inside_lock(self, user_id: int) -> bool:
        return await self._sync_locked(user_id)

    async def resync_many(self, user_ids: list[int], *, concurrency: int = 5) -> tuple[int, int]:
        sem = asyncio.Semaphore(concurrency)
        ok = fail = 0

        async def one(uid: int) -> None:
            nonlocal ok, fail
            async with sem:
                try:
                    good = await self.sync_now(uid)
                except Exception:  # noqa: BLE001
                    log.exception("resync %s", uid)
                    good = False
                if good:
                    ok += 1
                else:
                    fail += 1

        for i in range(0, len(user_ids), 200):
            await asyncio.gather(*(one(u) for u in user_ids[i : i + 200]))
        return ok, fail

    async def retry_pending(self, limit: int = 100) -> int:
        async with self.ctx.session() as s:
            ids = list((await s.scalars(select(User.id).where(User.sync_pending.is_(True)).limit(limit))).all())
        if not ids:
            return 0
        ok, _ = await self.resync_many(ids)
        return ok

    # ------------------------------------------------------------------ ссылка подписки
    async def revoke_link(self, user_id: int) -> dict:
        """Выпускает новую ссылку подписки (старая перестаёт работать)."""
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                if user is None or not user.remna_id:
                    raise LookupError("у пользователя нет учётной записи в панели")
                data = await self.ctx.remna.revoke_subscription(user.remna_id)
                info = self._extract(data or {})
                if not info.get("sub_url") or not info.get("short_uuid"):
                    info = self._extract(await self.ctx.remna.get_user(user.remna_id))
                info.pop("remna_id", None)
                self._apply_info(user, info)
                await s.commit()
        return info

    # ------------------------------------------------------------------ истечение сроков
    async def advance_user(self, user_id: int) -> str | None:
        """Переводит пользователя на следующий отрезок (PREMIUM -> LITE) или снимает подписку.

        Возвращает "downgrade" | "expire" | None и сразу уведомляет пользователя.
        """
        kind: str | None = None
        new: SubState | None = None
        async with self.lock(user_id):
            async with self.ctx.session() as s:
                user = await s.get(User, user_id, with_for_update=True)
                now = utcnow()
                if user is None or user.tier is None or user.expires_at is None or user.expires_at > now:
                    return None
                old = self.state_of(user)
                new = normalize(old, now)
                kind = "downgrade" if (new.tier == Tier.LITE and old.tier == Tier.PREMIUM) else "expire"
                self.apply_to_row(user, new)
                user.sync_pending = True
                s.add(SubEvent(user_id=user_id, kind=kind, tier=old.tier.value if old.tier else None, meta={}))
                await s.commit()
            await self._sync_locked(user_id)

        texts, notify = self.ctx.texts, self.ctx.notify
        if kind == "downgrade" and new is not None:
            note = ""
            if self.ctx.cfg.get("trim_devices_on_downgrade"):
                removed = await self.ctx.devices.trim_to_limit(user_id)
                if removed:
                    note = texts.render(
                        "n_devices_trimmed", limit=self.ctx.plans.get(Tier.LITE).device_limit, removed=", ".join(removed)
                    )
            await notify.send(
                user_id,
                texts.render("n_downgraded", until=fmt_dt(new.expires_at), devices_note=note),
                kb(btn("Моя подписка", "menu:sub")),
            )
        elif kind == "expire":
            await notify.send(user_id, texts.render("n_expired"), kb(btn("Оплатить подписку", "pay:menu")))
        return kind

    async def advance_due(self, limit: int = 200) -> int:
        now = utcnow()
        async with self.ctx.session() as s:
            ids = list(
                (
                    await s.scalars(
                        select(User.id).where(User.tier.is_not(None), User.expires_at <= now).limit(limit)
                    )
                ).all()
            )
        done = 0
        for uid in ids:
            try:
                if await self.advance_user(uid):
                    done += 1
            except Exception:  # noqa: BLE001
                log.exception("advance_user %s", uid)
        return done
