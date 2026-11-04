"""Общий каркас админки: фабрика роутеров, реестр обработчиков текстового ввода, главный экран."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.filters import IsAdmin, IsOwner, NotCommand
from app.bot.states import AdminFlow
from app.bot.ui import btn, kb, show, toast
from app.utils import fmt_dt, money, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

InputHandler = Callable[[Message, FSMContext, "AppContext", dict[str, Any]], Awaitable[None]]
_INPUTS: dict[str, InputHandler] = {}


def make_router(name: str, *, owner_only: bool = False) -> Router:
    """Роутер, доступный только админам (или только владельцам)."""
    r = Router(name=f"admin.{name}")
    flt = IsOwner() if owner_only else IsAdmin()
    r.message.filter(flt)
    r.callback_query.filter(flt)
    return r


def on_input(action: str) -> Callable[[InputHandler], InputHandler]:
    def deco(fn: InputHandler) -> InputHandler:
        _INPUTS[action] = fn
        return fn

    return deco


async def ask(
    event: Message | CallbackQuery,
    state: FSMContext,
    action: str,
    prompt: str,
    back_cb: str,
    *,
    keep: bool = False,
    **data: Any,
) -> None:
    """Просит админа прислать текст; результат придёт в обработчик, зарегистрированный через on_input."""
    current = await state.get_data() if keep else {}
    await state.set_state(AdminFlow.input)
    await state.set_data({**current, "action": action, **data})
    await show(event, prompt, kb(btn("Отмена", back_cb)))


router = make_router("common")


@router.message(AdminFlow.input, NotCommand())
async def on_any_input(message: Message, state: FSMContext, ctx: AppContext) -> None:
    data = await state.get_data()
    handler = _INPUTS.get(str(data.get("action")))
    if handler is None:
        await state.clear()
        return
    await handler(message, state, ctx, data)


async def home_screen(ctx: AppContext, user_id: int):
    s = await ctx.stats.overview()
    rev_today, n_today = s["rev_today"]
    text = (
        "⚙️ <b>Админ-панель · Fable | VPN</b>\n\n"
        f"👥 Пользователей: <b>{s['users_total']}</b> (за сутки +{s['new_24h']})\n"
        f"🟢 Активных подписок: <b>{s['premium'] + s['lite']}</b> "
        f"(PREMIUM {s['premium']} · LITE {s['lite']})\n"
        f"💰 Сегодня: <b>{money(rev_today)}</b> ({n_today} оплат)\n"
        f"⏳ Платежей в ожидании: {s['pending_payments']}"
        + (f"\n⚠️ Не синхронизировано с панелью: <b>{s['unsynced']}</b>" if s["unsynced"] else "")
        + f"\n\n<i>Обновлено {fmt_dt(utcnow())}</i>"
    )
    rows = [
        [btn("📊 Статистика", "a:st"), btn("👥 Пользователи", "a:u")],
        [btn("💳 Платежи", "a:pl:all:0"), btn("🏷 Тарифы", "a:t")],
        [btn("🎟 Промокоды", "a:cl:0"), btn("🤝 Рефералка", "a:r")],
        [btn("📣 Рассылки", "a:b"), btn("✏️ Тексты", "a:x:0")],
        [btn("🌐 Панель VPN", "a:i"), btn("⚙️ Настройки", "a:s")],
        [btn("🧰 Система", "a:y")] + ([btn("🛡 Админы", "a:m")] if ctx.is_owner(user_id) else []),
        [btn("← В меню бота", "menu:main")],
    ]
    return text, kb(*rows)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, ctx: AppContext) -> None:
    await state.clear()
    text, markup = await home_screen(ctx, message.from_user.id)
    await show(message, text, markup)


@router.callback_query(F.data == "a:home")
async def cb_home(cb: CallbackQuery, ctx: AppContext) -> None:
    text, markup = await home_screen(ctx, cb.from_user.id)
    await show(cb, text, markup)


async def need_owner(cb: CallbackQuery, ctx: AppContext) -> bool:
    """True, если действие запрещено (не владелец) — уже показали уведомление."""
    if ctx.is_owner(cb.from_user.id):
        return False
    await toast(cb, "Это действие доступно только владельцу.", alert=True)
    return True
