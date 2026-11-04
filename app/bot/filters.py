from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext


class NotCommand(Filter):
    """Сообщение не является командой (/start, /admin …). Работает и для медиа без текста."""

    async def __call__(self, message: Message) -> bool:
        return not (message.text and message.text.startswith("/"))


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, ctx: AppContext) -> bool:
        u = event.from_user
        return bool(u and ctx.is_admin(u.id))


class IsOwner(Filter):
    async def __call__(self, event: Message | CallbackQuery, ctx: AppContext) -> bool:
        u = event.from_user
        return bool(u and ctx.is_owner(u.id))
