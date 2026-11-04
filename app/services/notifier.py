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
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import update

from app.db.models import User

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def _mark_blocked(self, user_id: int) -> None:
        try:
            async with self.ctx.session() as s:
                await s.execute(update(User).where(User.id == user_id).values(bot_blocked=True))
                await s.commit()
        except Exception:  # noqa: BLE001
            log.exception("cannot mark user %s as blocked", user_id)

    async def send(
        self,
        user_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        *,
        silent: bool = False,
    ) -> bool:
        for attempt in range(2):
            try:
                await self.ctx.bot.send_message(user_id, text, reply_markup=markup, disable_notification=silent)
                return True
            except TelegramRetryAfter as e:
                if attempt == 0:
                    await asyncio.sleep(min(e.retry_after + 1, 30))
                    continue
                return False
            except TelegramForbiddenError:
                await self._mark_blocked(user_id)
                return False
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower() or "user is deactivated" in str(e).lower():
                    await self._mark_blocked(user_id)
                else:
                    log.warning("send to %s failed: %s", user_id, e)
                return False
            except TelegramAPIError as e:
                log.warning("send to %s failed: %s", user_id, e)
                return False
        return False

    async def to_admins(self, text: str, *, owners_only: bool = True, markup: InlineKeyboardMarkup | None = None) -> None:
        ids = set(self.ctx.settings.owners) if owners_only else self.ctx.all_admin_ids()
        for uid in ids:
            await self.send(uid, text, markup, silent=False)
