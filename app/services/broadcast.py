"""Рассылки.

Админ присылает боту любое сообщение (текст, фото, видео, файл, кружок…) — мы пересылаем его
пользователям через ``copy_message``, поэтому форматирование и медиа сохраняются без доп. кода.
Рассылка идёт фоном порциями, переживает перезапуск (хранится курсор) и умеет паузу/отмену.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from sqlalchemy import desc, func, select, update

from app.bot.ui import btn, kb
from app.db.models import Broadcast, User
from app.services.users import user_filter
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

BATCH = 100
DELAY = 0.05  # ~20 сообщений/сек при лимите Telegram ~30/сек


class BroadcastService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._tasks: dict[int, asyncio.Task] = {}

    @staticmethod
    def _conds(segment: str) -> list:
        return [User.is_banned.is_(False), User.bot_blocked.is_(False), *user_filter(segment, utcnow())]

    # ------------------------------------------------------------------ CRUD
    async def audience_count(self, segment: str) -> int:
        async with self.ctx.session() as s:
            return int(await s.scalar(select(func.count()).select_from(User).where(*self._conds(segment))) or 0)

    async def create(
        self,
        admin_id: int,
        src_chat_id: int,
        src_message_id: int,
        segment: str,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> Broadcast:
        async with self.ctx.session() as s:
            b = Broadcast(
                created_by=admin_id,
                src_chat_id=src_chat_id,
                src_message_id=src_message_id,
                segment=segment,
                button_text=button_text,
                button_url=button_url,
                status="draft",
            )
            s.add(b)
            await s.commit()
            return b

    async def get(self, bid: int) -> Broadcast | None:
        async with self.ctx.session() as s:
            return await s.get(Broadcast, bid)

    async def recent(self, limit: int = 10) -> list[Broadcast]:
        async with self.ctx.session() as s:
            return list((await s.execute(select(Broadcast).order_by(desc(Broadcast.id)).limit(limit))).scalars().all())

    @staticmethod
    def markup(b: Broadcast):
        if b.button_text and b.button_url:
            return kb(btn(b.button_text, url=b.button_url))
        return None

    async def preview(self, b: Broadcast, chat_id: int) -> None:
        await self.ctx.bot.copy_message(
            chat_id=chat_id, from_chat_id=b.src_chat_id, message_id=b.src_message_id, reply_markup=self.markup(b)
        )

    # ------------------------------------------------------------------ управление
    def is_running(self, bid: int) -> bool:
        t = self._tasks.get(bid)
        return t is not None and not t.done()

    async def start(self, bid: int) -> None:
        async with self.ctx.session() as s:
            b = await s.get(Broadcast, bid)
            if b is None or b.status in ("done", "cancelled"):
                return
            if b.status == "draft":
                b.total = await s.scalar(select(func.count()).select_from(User).where(*self._conds(b.segment))) or 0
                b.started_at = utcnow()
            b.status = "running"
            await s.commit()
        if not self.is_running(bid):
            self._tasks[bid] = asyncio.create_task(self._run(bid), name=f"broadcast-{bid}")

    async def pause(self, bid: int) -> None:
        await self._set_status(bid, "paused", only_from=("running",))

    async def cancel(self, bid: int) -> None:
        await self._set_status(bid, "cancelled", only_from=("running", "paused", "draft"))

    async def _set_status(self, bid: int, status: str, only_from: tuple[str, ...]) -> None:
        async with self.ctx.session() as s:
            await s.execute(
                update(Broadcast).where(Broadcast.id == bid, Broadcast.status.in_(only_from)).values(status=status)
            )
            await s.commit()

    async def resume_running(self) -> None:
        """При старте приложения продолжает рассылки, прерванные перезапуском."""
        async with self.ctx.session() as s:
            ids = list((await s.scalars(select(Broadcast.id).where(Broadcast.status == "running"))).all())
        for bid in ids:
            self._tasks[bid] = asyncio.create_task(self._run(bid), name=f"broadcast-{bid}")

    async def shutdown(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # ------------------------------------------------------------------ отправка
    async def _send_one(self, b: Broadcast, uid: int) -> str:
        markup = self.markup(b)
        for _ in range(3):
            try:
                await self.ctx.bot.copy_message(
                    chat_id=uid, from_chat_id=b.src_chat_id, message_id=b.src_message_id, reply_markup=markup
                )
                return "sent"
            except TelegramRetryAfter as e:
                await asyncio.sleep(min(e.retry_after + 1, 60))
            except TelegramForbiddenError:
                return "blocked"
            except TelegramBadRequest as e:
                text = str(e).lower()
                if "chat not found" in text or "user is deactivated" in text:
                    return "blocked"
                log.warning("broadcast %s -> %s: %s", b.id, uid, e)
                return "failed"
            except TelegramAPIError as e:
                log.warning("broadcast %s -> %s: %s", b.id, uid, e)
                return "failed"
        return "failed"

    async def _run(self, bid: int) -> None:
        try:
            while True:
                async with self.ctx.session() as s:
                    b = await s.get(Broadcast, bid)
                    if b is None or b.status != "running":
                        return
                    ids = list(
                        (
                            await s.scalars(
                                select(User.id)
                                .where(User.id > b.cursor_id, *self._conds(b.segment))
                                .order_by(User.id)
                                .limit(BATCH)
                            )
                        ).all()
                    )
                if not ids:
                    break
                sent = failed = blocked = 0
                blocked_ids: list[int] = []
                for uid in ids:
                    res = await self._send_one(b, uid)
                    if res == "sent":
                        sent += 1
                    elif res == "blocked":
                        blocked += 1
                        blocked_ids.append(uid)
                    else:
                        failed += 1
                    await asyncio.sleep(DELAY)
                async with self.ctx.session() as s:
                    await s.execute(
                        update(Broadcast)
                        .where(Broadcast.id == bid)
                        .values(
                            sent=Broadcast.sent + sent,
                            failed=Broadcast.failed + failed,
                            blocked=Broadcast.blocked + blocked,
                            cursor_id=ids[-1],
                        )
                    )
                    if blocked_ids:
                        await s.execute(update(User).where(User.id.in_(blocked_ids)).values(bot_blocked=True))
                    await s.commit()

            async with self.ctx.session() as s:
                await s.execute(
                    update(Broadcast)
                    .where(Broadcast.id == bid, Broadcast.status == "running")
                    .values(status="done", finished_at=utcnow())
                )
                await s.commit()
                b = await s.get(Broadcast, bid)
            if b is not None and b.status == "done":
                await self.ctx.notify.send(
                    b.created_by,
                    f"📣 <b>Рассылка #{b.id} завершена</b>\n\n"
                    f"Доставлено: <b>{b.sent}</b>\nОшибок: {b.failed}\nЗаблокировали бота: {b.blocked}",
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("broadcast %s crashed", bid)
            await self._set_status(bid, "paused", only_from=("running",))
