from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

Handler = Callable[[Any, dict[str, Any]], Awaitable[Any]]


async def _tell(event: Message | CallbackQuery, text: str) -> None:
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(text[:190], show_alert=True)
        else:
            await event.answer(text)
    except TelegramAPIError:
        pass


class ThrottleMiddleware(BaseMiddleware):
    """Не даёт спамить: не чаще одного действия за ~0.4 сек от пользователя (админы без ограничений)."""

    def __init__(self, interval_ms: int = 400) -> None:
        self.interval_ms = interval_ms

    async def __call__(self, handler: Handler, event: Message | CallbackQuery, data: dict[str, Any]) -> Any:
        ctx: AppContext = data["ctx"]
        tg = data.get("event_from_user")
        if tg is not None and not ctx.is_admin(tg.id):
            ok = await ctx.redis.set(f"thr:{tg.id}", 1, px=self.interval_ms, nx=True)
            if not ok:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except TelegramAPIError:
                        pass
                return None
        return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Загружает/создаёт пользователя и применяет бан и режим техработ."""

    async def __call__(self, handler: Handler, event: Message | CallbackQuery, data: dict[str, Any]) -> Any:
        ctx: AppContext = data["ctx"]
        tg = data.get("event_from_user")
        if tg is None or tg.is_bot:
            return await handler(event, data)

        is_admin = ctx.is_admin(tg.id)
        allow_create = is_admin or bool(ctx.cfg.get("registration_open"))
        user, created = await ctx.users.upsert_from_telegram(tg, allow_create=allow_create)
        if user is None:
            await _tell(event, "Приём новых пользователей временно закрыт. Загляни позже 🙂")
            return None
        data["user"] = user
        data["is_new_user"] = created

        if not is_admin:
            if user.is_banned:
                await _tell(event, ctx.texts.render("banned"))
                return None
            if ctx.cfg.get("maintenance"):
                await _tell(event, ctx.texts.render("maintenance"))
                return None
        return await handler(event, data)


class ResetStateMiddleware(BaseMiddleware):
    """Любое нажатие кнопки выводит из режима ожидания текстового ввода (данные wizard-ов сохраняются)."""

    async def __call__(self, handler: Handler, event: CallbackQuery, data: dict[str, Any]) -> Any:
        state: FSMContext | None = data.get("state")
        if state is not None and await state.get_state() is not None:
            await state.set_state(None)
        return await handler(event, data)
