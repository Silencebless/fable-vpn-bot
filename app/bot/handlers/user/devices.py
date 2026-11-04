from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import views
from app.bot.filters import NotCommand
from app.bot.states import UserFlow
from app.bot.ui import btn, kb, show, toast
from app.db.models import User
from app.integrations.remnawave import RemnaError
from app.utils import esc, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = Router(name="user.devices")


async def _fresh(ctx: AppContext, user: User) -> User:
    return await ctx.users.get(user.id) or user


def _key(cb: CallbackQuery) -> str:
    parts = (cb.data or "").split(":")
    return parts[2] if len(parts) > 2 else ""


@router.callback_query(F.data == "setup:open")
async def cb_setup(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    text, markup = views.setup_screen(ctx, await _fresh(ctx, user))
    await show(cb, text, markup)


@router.callback_query(F.data == "dev:list")
async def cb_list(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    text, markup = await views.devices_screen(ctx, await _fresh(ctx, user))
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("dev:v:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    try:
        dev = await ctx.devices.find(user, _key(cb))
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуй ещё раз.", alert=True)
        return
    if dev is None:
        text, markup = await views.devices_screen(ctx, user)
        await show(cb, text, markup)
        return
    text = ctx.texts.render("device_card", label=esc(dev.label), info=dev.info())
    markup = kb(
        [btn("Переименовать", f"dev:rn:{dev.key}"), btn("Удалить", f"dev:del:{dev.key}")],
        btn("Назад", "dev:list"),
    )
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("dev:del:"))
async def cb_delete_ask(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    try:
        dev = await ctx.devices.find(user, _key(cb))
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуй ещё раз.", alert=True)
        return
    if dev is None:
        text, markup = await views.devices_screen(ctx, user)
        await show(cb, text, markup)
        return
    text = ctx.texts.render("device_delete_confirm", label=esc(dev.label))
    await show(cb, text, kb(btn("Да, удалить", f"dev:ok:{dev.key}"), btn("Отмена", f"dev:v:{dev.key}")))


@router.callback_query(F.data.startswith("dev:ok:"))
async def cb_delete(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    try:
        await ctx.devices.delete(user, _key(cb))
    except RemnaError:
        await toast(cb, "Не удалось удалить устройство. Попробуй ещё раз.", alert=True)
        return
    await show(cb, ctx.texts.render("device_deleted"), kb(btn("Список устройств", "dev:list")))


@router.callback_query(F.data.startswith("dev:rn:"))
async def cb_rename_ask(cb: CallbackQuery, ctx: AppContext, user: User, state: FSMContext) -> None:
    user = await _fresh(ctx, user)
    try:
        dev = await ctx.devices.find(user, _key(cb))
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуй ещё раз.", alert=True)
        return
    if dev is None:
        await toast(cb, "Устройство не найдено", alert=True)
        return
    await state.set_state(UserFlow.device_name)
    await state.update_data(key=dev.key)
    await show(cb, ctx.texts.render("device_rename_ask", label=esc(dev.label)), kb(btn("Назад", f"dev:v:{dev.key}")))


@router.message(UserFlow.device_name, F.text, NotCommand())
async def rename_input(message: Message, ctx: AppContext, user: User, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 24:
        await message.answer("Название должно быть от 1 до 24 символов. Попробуй ещё раз.")
        return
    data = await state.get_data()
    await state.clear()
    user = await _fresh(ctx, user)
    try:
        await ctx.devices.rename(user, str(data.get("key", "")), name)
    except RemnaError:
        await message.answer(ctx.texts.render("panel_unavailable"))
        return
    text, markup = await views.devices_screen(ctx, user)
    await show(message, text, markup)


@router.callback_query(F.data == "dev:add")
async def cb_add(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    now = utcnow()
    if not ctx.subs.view(user, now).is_active(now) or not user.remna_id:
        text, markup = await views.devices_screen(ctx, user)
        await show(cb, text, markup)
        return
    try:
        used = len(await ctx.devices.list(user))
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуй ещё раз.", alert=True)
        return
    if used < ctx.devices.limit_for(user):
        text, markup = views.setup_screen(ctx, user, back_cb="dev:list")
    else:
        text, markup = await views.device_limit_screen(ctx, user)
    await show(cb, text, markup)
