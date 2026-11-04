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
from app.core.subscription import Tier
from app.db.models import User
from app.services.payments import NeedEmail, PaymentError
from app.utils import EMAIL_RE, fmt_dt, money

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = Router(name="user.billing")


async def _fresh(ctx: AppContext, user: User) -> User:
    return await ctx.users.get(user.id) or user


@router.callback_query(F.data == "menu:sub")
async def cb_sub(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    text, markup = await views.sub_menu(ctx, await _fresh(ctx, user))
    await show(cb, text, markup)


@router.callback_query(F.data == "pay:menu")
async def cb_pay_menu(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    text, markup = views.pay_menu(ctx, await _fresh(ctx, user))
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("pay:plan:"))
async def cb_plan(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    code = (cb.data or "").split(":")[2]
    screen = await views.plan_view(ctx, await _fresh(ctx, user), code)
    if screen is None:
        await toast(cb, "Этот тариф сейчас недоступен", alert=True)
        return
    await show(cb, *screen)


async def start_payment(
    event: Message | CallbackQuery,
    ctx: AppContext,
    user: User,
    state: FSMContext,
    code: str,
    upgrade: bool,
) -> None:
    if code not in ("lite", "premium") or not ctx.plans.get(code).is_active:
        if isinstance(event, CallbackQuery):
            await toast(event, "Этот тариф сейчас недоступен", alert=True)
        return
    tier = Tier(code)
    try:
        pay = await ctx.pay.create(user.id, tier, upgrade=upgrade)
    except NeedEmail:
        await state.set_state(UserFlow.email)
        await state.update_data(code=code, upgrade=upgrade)
        await show(event, ctx.texts.render("email_ask"), kb(btn("Назад", f"pay:plan:{code}")))
        return
    except PaymentError as e:
        log.warning("оплата недоступна для %s: %s", user.id, e)
        await show(event, ctx.texts.render("pay_unavailable"), kb(btn("Назад", "pay:menu")))
        return

    discount_line = f"\nСкидка по промокоду: −{money(pay.discount)}" if pay.discount and pay.discount > 0 else ""
    text = ctx.texts.render(
        "pay_created", plan=tier.title, amount=money(pay.amount), discount_line=discount_line
    )
    markup = kb(
        btn("Перейти к оплате", url=pay.confirmation_url) if pay.confirmation_url else None,
        btn("Проверить оплату", f"pay:chk:{pay.id}"),
        btn("Назад", "pay:menu"),
    )
    await show(event, text, markup)


@router.callback_query(F.data.startswith("pay:go:"))
async def cb_pay_go(cb: CallbackQuery, ctx: AppContext, user: User, state: FSMContext) -> None:
    parts = (cb.data or "").split(":")
    code = parts[2]
    upgrade = len(parts) > 3 and parts[3] == "up"
    await start_payment(cb, ctx, await _fresh(ctx, user), state, code, upgrade)


@router.callback_query(F.data.startswith("pay:chk:"))
async def cb_pay_check(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    try:
        pay_id = int((cb.data or "").split(":")[2])
    except (IndexError, ValueError):
        await toast(cb, "")
        return
    pay = await ctx.pay.check(pay_id, user.id)
    if pay is None:
        await toast(cb, "Платёж не найден", alert=True)
        return
    if pay.status == "succeeded":
        fresh = await _fresh(ctx, user)
        st = ctx.subs.view(fresh)
        text = ctx.texts.render("pay_success", plan=Tier(pay.plan_code).title, until=fmt_dt(st.total_end), extra="")
        await show(cb, text, kb([btn("Настройка", "setup:open"), btn("Моя подписка", "menu:sub")]))
    elif pay.status in ("canceled", "refunded"):
        await show(cb, ctx.texts.render("pay_canceled"), kb(btn("Оплатить подписку", "pay:menu"), btn("Назад", "menu:sub")))
    else:
        await toast(cb, ctx.texts.render("pay_pending"), alert=True)


# ───────────────────────── email для чека ─────────────────────────
@router.message(UserFlow.email, F.text, NotCommand())
async def email_input(message: Message, ctx: AppContext, user: User, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if len(email) > 200 or not EMAIL_RE.match(email):
        await message.answer(ctx.texts.render("email_bad"))
        return
    data = await state.get_data()
    await state.clear()
    await ctx.users.set_email(user.id, email.lower())
    await start_payment(message, ctx, await _fresh(ctx, user), state, str(data.get("code", "lite")), bool(data.get("upgrade")))


@router.message(UserFlow.email)
async def email_other(message: Message, ctx: AppContext) -> None:
    await message.answer(ctx.texts.render("email_bad"))
