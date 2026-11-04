"""Фоновые задачи. Каждая работает в своём цикле; сбой одной не влияет на остальные."""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from app.bot.ui import btn, kb
from app.core.subscription import Tier
from app.db.models import User
from app.utils import days_word, fmt_dt, to_local, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

# имя -> интервал в секундах (используется и диагностикой в админке)
INTERVALS = {"expiry": 30, "payments": 60, "referrals": 180, "reminders": 600, "sync": 120, "daily": 1800}


def left_phrase(left: timedelta) -> str:
    n = math.ceil(left.total_seconds() / 86400)
    return "меньше чем через сутки" if n <= 1 else f"через {days_word(n)}"


class Workers:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        jobs: dict[str, Callable[[], Awaitable[None]]] = {
            "expiry": self.expiry,
            "payments": self.payments,
            "referrals": self.referrals,
            "reminders": self.reminders,
            "sync": self.sync,
            "daily": self.daily,
        }
        for i, (name, fn) in enumerate(jobs.items()):
            self._tasks.append(asyncio.create_task(self._loop(name, INTERVALS[name], fn, 3 + i * 2), name=f"worker-{name}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self, name: str, interval: int, fn: Callable[[], Awaitable[None]], delay: int) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("воркер %s: ошибка итерации", name)
            self.ctx.heartbeats[name] = utcnow()
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ задачи
    async def expiry(self) -> None:
        """PREMIUM закончился -> LITE из очереди; всё закончилось -> снимаем подписку и уведомляем."""
        n = await self.ctx.subs.advance_due()
        if n:
            log.info("expiry: обработано %s", n)

    async def payments(self) -> None:
        """Добираем статусы платежей, по которым не пришло уведомление."""
        await self.ctx.pay.reconcile_pending()

    async def referrals(self) -> None:
        n = await self.ctx.refs.check_pending()
        if n:
            log.info("referrals: начислено наград %s", n)

    async def sync(self) -> None:
        """Повторяем синхронизацию с панелью для тех, у кого она ранее не удалась."""
        n = await self.ctx.subs.retry_pending()
        if n:
            log.info("sync: восстановлено %s", n)

    async def reminders(self) -> None:
        ctx = self.ctx
        if not ctx.cfg.get("reminders_enabled"):
            return
        days = sorted({int(d) for d in ctx.cfg.get("reminder_days")}, reverse=True)
        if not days:
            return
        now = utcnow()
        async with ctx.session() as s:
            users = (
                await s.scalars(
                    select(User)
                    .where(
                        User.tier.is_not(None),
                        User.queued_lite_s == 0,  # если дальше идёт LITE — это не конец доступа
                        User.expires_at > now,
                        User.expires_at <= now + timedelta(days=days[0]),
                        User.is_banned.is_(False),
                        User.bot_blocked.is_(False),
                    )
                    .limit(500)
                )
            ).all()
        for u in users:
            assert u.expires_at is not None
            left = u.expires_at - now
            stage = sum(1 for d in days if left <= timedelta(days=d))
            done = u.notify_stage if u.notify_expiry == u.expires_at else 0
            if stage <= done:
                continue
            text = ctx.texts.render(
                "n_expire_soon", tier=Tier(u.tier).title, until=fmt_dt(u.expires_at), left=left_phrase(left)
            )
            await ctx.notify.send(u.id, text, kb(btn("Продлить подписку", "pay:menu")))
            async with ctx.session() as s:
                await s.execute(
                    update(User)
                    .where(User.id == u.id, User.expires_at == u.expires_at)
                    .values(notify_expiry=u.expires_at, notify_stage=stage)
                )
                await s.commit()
            await asyncio.sleep(0.05)

    async def daily(self) -> None:
        """Раз в сутки (в BACKUP_HOUR по локальному времени) шлём бэкап БД владельцам."""
        ctx = self.ctx
        local = to_local(utcnow())
        if local.hour != ctx.settings.backup_hour:
            return
        if not await ctx.redis.set(f"backup:{local.date()}", 1, ex=36 * 3600, nx=True):
            return
        targets = ctx.backup.targets()
        if targets:
            await ctx.backup.send(targets)
