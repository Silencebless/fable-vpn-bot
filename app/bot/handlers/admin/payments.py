from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.types import CallbackQuery

from app.bot.handlers.admin.common import make_router, need_owner
from app.bot.ui import btn, kb, nav_row, page_info, show, toast
from app.integrations.yookassa import YooKassaError
from app.services.payments import PaymentError
from app.utils import esc, fmt_dt, money

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("payments")
PAGE = 8

STATUSES = {"all": "Все", "pending": "⏳ Ожидают", "succeeded": "✅ Оплачены", "canceled": "❌ Отменены", "refunded": "↩️ Возвраты"}
ICON = {"succeeded": "✅", "pending": "⏳", "canceled": "❌", "refunded": "↩️"}


@router.callback_query(F.data.startswith("a:pl:"))
async def cb_list(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, status, page_s = (cb.data or "").split(":")
    if status not in STATUSES:
        status = "all"
    pays, total = await ctx.pay.page(None if status == "all" else status, int(page_s), PAGE)
    page, pages = page_info(total, int(page_s), PAGE)
    if page != int(page_s):
        pays, _ = await ctx.pay.page(None if status == "all" else status, page, PAGE)

    rows = [
        btn(f"{ICON.get(p.status, '•')} #{p.id} · {money(p.amount)} · {p.plan_code.upper()} · {fmt_dt(p.created_at)[:10]}", f"a:pv:{p.id}")
        for p in pays
    ]
    rows.append(nav_row(page, pages, lambda n: f"a:pl:{status}:{n}"))
    filters = [btn(("• " if k == status else "") + title, f"a:pl:{k}:0") for k, title in STATUSES.items()]
    rows += [filters[:3], filters[3:]]
    rows.append([btn("📥 CSV", "a:st:csv:pays"), btn("← Назад", "a:home")])
    await show(cb, f"💳 <b>Платежи</b> · {STATUSES[status]} — {total}", kb(*rows))


async def _card(cb: CallbackQuery, ctx: AppContext, pay_id: int, flash: str = "") -> None:
    p = await ctx.pay.get(pay_id)
    if p is None:
        await show(cb, "Платёж не найден.", kb(btn("← Назад", "a:pl:all:0")))
        return
    user = await ctx.users.get(p.user_id) if p.user_id else None
    who = f"{esc(user.display_name)} (<code>{user.id}</code>)" if user else "— (удалён)"
    lines = [
        f"{ICON.get(p.status, '•')} <b>Платёж #{p.id}</b> — {p.status}",
        f"Пользователь: {who}",
        f"Тариф: {p.plan_code.upper()} · {'апгрейд' if p.kind == 'upgrade' else 'покупка'} · {p.period_days} дн.",
        f"Сумма: <b>{money(p.amount)}</b>"
        + (f" (было {money(p.base_amount)}, скидка {money(p.discount)})" if p.discount and p.discount > 0 else ""),
        f"Создан: {fmt_dt(p.created_at)} · Оплачен: {fmt_dt(p.paid_at)}",
        f"ЮKassa: <code>{esc(p.external_id or '—')}</code>",
    ]
    if p.receipt_email:
        lines.append(f"Email чека: {esc(p.receipt_email)}")
    if p.cancel_reason:
        lines.append(f"Причина отмены: {esc(p.cancel_reason)}")
    if p.promo_id:
        lines.append(f"Промокод: #{p.promo_id}")
    if p.meta and p.meta.get("amount_mismatch"):
        lines.append(f"⚠️ <b>Сумма не совпала</b>, пришло {esc(p.meta['amount_mismatch'])} — подписка не начислена")
    if p.status == "succeeded" and not p.applied:
        lines.append("⚠️ Платёж оплачен, но подписка не начислена")
    text = (f"{flash}\n\n" if flash else "") + "\n".join(lines)
    rows = []
    if p.status == "pending" and p.external_id:
        rows.append([btn("🔄 Проверить в ЮKassa", f"a:pc:{p.id}")])
    if p.status == "succeeded":
        rows.append([btn("↩️ Вернуть деньги", f"a:pr:{p.id}")])
    if p.user_id:
        rows.append([btn("👤 Пользователь", f"a:u:{p.user_id}:card")])
    rows.append([btn("← К списку", "a:pl:all:0")])
    await show(cb, text, kb(*rows))


@router.callback_query(F.data.startswith("a:pv:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext) -> None:
    await _card(cb, ctx, int((cb.data or "").split(":")[2]))


@router.callback_query(F.data.startswith("a:pc:"))
async def cb_check(cb: CallbackQuery, ctx: AppContext) -> None:
    pay_id = int((cb.data or "").split(":")[2])
    p = await ctx.pay.get(pay_id)
    if p is None or not p.external_id or ctx.yk is None:
        await toast(cb, "Нечего проверять", alert=True)
        return
    try:
        await ctx.pay.settle(p.external_id)
    except YooKassaError as e:
        await toast(cb, f"ЮKassa: {e}"[:190], alert=True)
        return
    await _card(cb, ctx, pay_id, "🔄 Статус обновлён")


@router.callback_query(F.data.startswith("a:pr:"))
async def cb_refund_ask(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    pay_id = int((cb.data or "").split(":")[2])
    p = await ctx.pay.get(pay_id)
    if p is None:
        return
    text = (
        f"↩️ <b>Возврат платежа #{p.id}</b> — {money(p.amount)}\n\n"
        "Деньги вернутся на способ оплаты клиента. Что сделать с оплаченными днями?"
    )
    markup = kb(
        btn("Вернуть и снять дни", f"a:pro:{p.id}:1"),
        btn("Вернуть, дни оставить", f"a:pro:{p.id}:0"),
        btn("Отмена", f"a:pv:{p.id}"),
    )
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("a:pro:"))
async def cb_refund(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    _, _, pay_id, flag = (cb.data or "").split(":")
    try:
        await ctx.pay.refund(int(pay_id), cb.from_user.id, remove_days=flag == "1")
    except PaymentError as e:
        await toast(cb, f"Не удалось вернуть: {e.detail or e.reason}"[:190], alert=True)
        return
    await _card(cb, ctx, int(pay_id), "✅ Возврат оформлен")
