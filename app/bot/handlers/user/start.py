from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import views
from app.bot.ui import btn, kb, show, toast
from app.core.subscription import Tier
from app.db.models import User
from app.services.subscriptions import TrialError
from app.utils import days_word, esc, fmt_dt, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

router = Router(name="user.start")


async def send_home(event: Message | CallbackQuery, ctx: AppContext, user: User) -> None:
    """Первый запуск — приветствие с кнопкой «Подключиться», иначе — главное меню."""
    user = await ctx.users.get(user.id) or user
    now = utcnow()
    first_launch = not user.remna_id and not user.trial_used and not ctx.subs.view(user, now).is_active(now)
    if first_launch:
        name = esc(user.first_name or "друг")
        if ctx.cfg.get("trial_enabled"):
            tier = Tier(ctx.cfg.get("trial_tier"))
            text = ctx.texts.render(
                "welcome_new",
                name=name,
                trial_days=days_word(int(ctx.cfg.get("trial_days"))),
                trial_tier=tier.title,
            )
        else:
            text = ctx.texts.render("welcome_no_trial", name=name)
        await show(event, text, kb(btn("Подключиться", "connect")))
        return
    text, markup = views.main_menu(ctx, user)
    await show(event, text, markup)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    ctx: AppContext,
    user: User,
    is_new_user: bool,
) -> None:
    await state.clear()
    if is_new_user and command.args and command.args.startswith("ref_"):
        await ctx.refs.register(user.id, command.args[4:])
    await send_home(message, ctx, user)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, ctx: AppContext, user: User) -> None:
    await state.clear()
    await send_home(message, ctx, user)


@router.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    await send_home(cb, ctx, user)


@router.callback_query(F.data == "connect")
async def cb_connect(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    try:
        new = await ctx.subs.activate_trial(user.id)
    except TrialError as e:
        if e.reason == "panel":
            await show(cb, ctx.texts.render("connect_unavailable"), kb(btn("Подключиться", "connect")))
            return
        # пробный период недоступен или уже использован — просто ведём в меню
        await send_home(cb, ctx, user)
        return

    user = await ctx.users.get(user.id) or user
    if not user.short_uuid or not user.sub_url:
        await send_home(cb, ctx, user)
        return
    plan = ctx.plans.get(new.tier)
    text = ctx.texts.render(
        "connected", tier=new.tier.title, until=fmt_dt(new.total_end), devices=plan.device_limit
    )
    markup = kb(
        btn("Скачать Happ", url=ctx.dl_url()) if ctx.web_ready else None,
        btn("Добавить подписку", url=ctx.add_url(user.short_uuid)) if ctx.web_ready else None,
        views.support_button(ctx),
        btn("Главное меню", "menu:main"),
    )
    await show(cb, text, markup)
    await ctx.audit.log(user.id, "trial.activated", user.id)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await toast(cb, "")


@router.callback_query(F.data == "sup:none")
async def cb_no_support(cb: CallbackQuery) -> None:
    await toast(cb, "Контакты поддержки пока не настроены. Загляни чуть позже.", alert=True)


# Любой другой текст вне сценариев — показываем меню
@router.message(StateFilter(None), F.text)
async def fallback(message: Message, ctx: AppContext, user: User) -> None:
    text, markup = views.main_menu(ctx, await ctx.users.get(user.id) or user)
    await message.answer(ctx.texts.render("unknown_input"))
    await message.answer(text, reply_markup=markup)
