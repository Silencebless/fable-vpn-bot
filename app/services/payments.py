"""Платежи (ЮKassa).

Принципы:
* Уведомлениям ЮKassa не доверяем слепо — по id всегда запрашиваем платёж через API и сверяем сумму.
* Применение платежа идемпотентно: строка платежа блокируется FOR UPDATE, флаг ``applied`` ставится
  в той же транзакции, что и изменение подписки.
* Если уведомление потерялось, воркер ``reconcile_pending`` сам доберёт статус.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import desc, func, select

from app.bot.ui import btn, kb
from app.core import subscription as core
from app.core.pricing import PROMO_DAYS, Quote, build_quote
from app.core.subscription import SubState, Tier
from app.db.models import Payment, Promo, PromoUse, User
from app.integrations.yookassa import YooKassaError, fmt_amount
from app.utils import days_word, esc, fmt_dt, money, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


class PaymentError(Exception):
    """reason: disabled | no_provider | inactive_plan | provider | not_refundable"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class NeedEmail(PaymentError):
    def __init__(self) -> None:
        super().__init__("need_email")


class PaymentService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ расчёт
    def available(self) -> bool:
        return self.ctx.yk is not None and bool(self.ctx.cfg.get("payments_enabled"))

    async def pending_promo(self, user: User, tier: Tier) -> Promo | None:
        if not user.pending_promo_id:
            return None
        async with self.ctx.session() as s:
            promo = await s.get(Promo, user.pending_promo_id)
        if promo is None or not promo.is_active or promo.kind == PROMO_DAYS:
            return None
        if promo.expires_at and promo.expires_at <= utcnow():
            return None
        if promo.tiers and tier.value not in promo.tiers:
            return None
        return promo

    async def quote(self, user: User, tier: Tier, *, upgrade: bool = False) -> tuple[Quote, Promo | None]:
        plans = self.ctx.plans
        promo = await self.pending_promo(user, tier)
        q = build_quote(
            self.ctx.subs.state_of(user),
            tier,
            price_lite=plans.get(Tier.LITE).price,
            price_premium=plans.get(Tier.PREMIUM).price,
            period_days=plans.get(tier).period_days,
            now=utcnow(),
            want_upgrade=upgrade,
            promo_kind=promo.kind if promo else None,
            promo_value=promo.value if promo else None,
        )
        return q, promo

    # ------------------------------------------------------------------ создание счёта
    def _receipt(self, email: str, description: str, amount: Decimal) -> dict:
        st = self.ctx.settings
        receipt: dict = {
            "customer": {"email": email},
            "items": [
                {
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": fmt_amount(amount),
                    "vat_code": st.yookassa_vat_code,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        }
        if st.yookassa_tax_system_code:
            receipt["tax_system_code"] = st.yookassa_tax_system_code
        return receipt

    async def create(self, user_id: int, tier: Tier, *, upgrade: bool = False) -> Payment:
        if self.ctx.yk is None:
            raise PaymentError("no_provider")
        if not self.ctx.cfg.get("payments_enabled"):
            raise PaymentError("disabled")
        plan = self.ctx.plans.get(tier)
        if not plan.is_active:
            raise PaymentError("inactive_plan")

        async with self.ctx.subs.lock(user_id):
            user = await self.ctx.users.get(user_id)
            if user is None:
                raise LookupError(user_id)
            receipts = bool(self.ctx.cfg.get("receipts_enabled"))
            if receipts and not user.email:
                raise NeedEmail()

            q, promo = await self.quote(user, tier, upgrade=upgrade)
            now = utcnow()
            title = f"Fable | VPN — {plan.title}"
            if q.kind == "upgrade":
                title = f"Fable | VPN — переход на {plan.title}"
            description = f"{title}, {days_word(q.period_days)}"

            async with self.ctx.session() as s:
                existing = await s.scalar(
                    select(Payment)
                    .where(
                        Payment.user_id == user_id,
                        Payment.status == "pending",
                        Payment.plan_code == tier.value,
                        Payment.kind == q.kind,
                        Payment.amount == q.amount,
                        Payment.expires_at > now,
                        Payment.confirmation_url.is_not(None),
                    )
                    .order_by(desc(Payment.id))
                    .limit(1)
                )
                if existing is not None:
                    return existing
                pay = Payment(
                    user_id=user_id,
                    plan_code=tier.value,
                    kind=q.kind,
                    period_days=q.period_days,
                    amount=q.amount,
                    base_amount=q.base,
                    discount=q.discount,
                    status="pending",
                    promo_id=promo.id if promo else None,
                    description=description,
                    receipt_email=user.email if receipts else None,
                    created_at=now,
                    expires_at=now + timedelta(minutes=55),
                    meta={"upgrade_extra": str(q.extra)},
                )
                s.add(pay)
                await s.flush()
                pay_id = pay.id
                await s.commit()

            try:
                obj = await self.ctx.yk.create_payment(
                    amount=q.amount,
                    description=description,
                    return_url=f"https://t.me/{self.ctx.bot_username}",
                    metadata={"payment_id": str(pay_id), "user_id": str(user_id)},
                    idempotence_key=f"fable-pay-{pay_id}",
                    receipt=self._receipt(user.email, description, q.amount) if receipts and user.email else None,
                )
            except YooKassaError as e:
                log.error("ЮKassa: не удалось создать платёж #%s: %s", pay_id, e)
                await self._set_status(pay_id, "canceled", cancel_reason="create_failed")
                raise PaymentError("provider", str(e)) from e

            async with self.ctx.session() as s:
                pay = await s.get(Payment, pay_id)
                assert pay is not None
                pay.external_id = obj.get("id")
                pay.confirmation_url = (obj.get("confirmation") or {}).get("confirmation_url")
                await s.commit()
            return pay

    async def _set_status(self, pay_id: int, status: str, **fields) -> None:
        async with self.ctx.session() as s:
            pay = await s.get(Payment, pay_id)
            if pay:
                pay.status = status
                for k, v in fields.items():
                    setattr(pay, k, v)
                await s.commit()

    # ------------------------------------------------------------------ чтение
    async def get(self, pay_id: int) -> Payment | None:
        async with self.ctx.session() as s:
            return await s.get(Payment, pay_id)

    async def page(self, status: str | None, page: int, size: int = 8, user_id: int | None = None) -> tuple[list[Payment], int]:
        conds = []
        if status:
            conds.append(Payment.status == status)
        if user_id is not None:
            conds.append(Payment.user_id == user_id)
        async with self.ctx.session() as s:
            total = int(await s.scalar(select(func.count()).select_from(Payment).where(*conds)) or 0)
            res = await s.execute(select(Payment).where(*conds).order_by(desc(Payment.id)).limit(size).offset(page * size))
            return list(res.scalars().all()), total

    # ------------------------------------------------------------------ обработка статуса
    async def check(self, pay_id: int, user_id: int) -> Payment | None:
        """Кнопка «Проверить оплату»: обновляет статус по данным ЮKassa."""
        pay = await self.get(pay_id)
        if pay is None or pay.user_id != user_id:
            return None
        if pay.status == "pending" and pay.external_id and self.ctx.yk is not None:
            try:
                await self.settle(pay.external_id)
            except YooKassaError as e:
                log.warning("check %s: %s", pay_id, e)
            pay = await self.get(pay_id)
        return pay

    async def settle(self, external_id: str) -> Payment | None:
        """Запрашивает платёж в ЮKassa и приводит наше состояние в соответствие."""
        assert self.ctx.yk is not None
        obj = await self.ctx.yk.get_payment(external_id)
        async with self.ctx.session() as s:
            pay = await s.scalar(select(Payment).where(Payment.external_id == external_id))
        if pay is None:
            log.warning("Уведомление по неизвестному платежу %s", external_id)
            return None
        status = obj.get("status")
        if status == "succeeded" and obj.get("paid"):
            return await self._apply_success(pay.id, obj)
        if status == "canceled":
            return await self._apply_canceled(pay.id, obj)
        return pay

    async def _apply_success(self, pay_id: int, obj: dict) -> Payment | None:
        pay0 = await self.get(pay_id)
        if pay0 is None:
            return None
        if pay0.applied:
            return pay0
        user_id = pay0.user_id
        if user_id is None:  # пользователь удалён — фиксируем платёж для учёта
            await self._set_status(pay_id, "succeeded", paid_at=utcnow(), applied=True)
            return await self.get(pay_id)

        subs = self.ctx.subs
        async with subs.lock(user_id):
            async with self.ctx.session() as s:
                pay = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
                assert pay is not None
                if pay.applied:
                    return pay
                paid_value = Decimal(str((obj.get("amount") or {}).get("value", "0")))
                if paid_value != Decimal(pay.amount):
                    log.error("Сумма платежа #%s не совпала: ждали %s, пришло %s", pay.id, pay.amount, paid_value)
                    pay.meta = {**(pay.meta or {}), "amount_mismatch": str(paid_value)}
                    await s.commit()
                    await self.ctx.notify.to_admins(
                        f"⚠️ Платёж #{pay.id}: сумма не совпала (ожидали {pay.amount}, пришло {paid_value}). "
                        "Подписка не начислена — проверьте вручную."
                    )
                    return pay
                user = await s.get(User, user_id, with_for_update=True)
                assert user is not None
                tier = Tier(pay.plan_code)
                period = timedelta(days=pay.period_days)
                upgrade = pay.kind == "upgrade"
                old, new = subs.apply_op(
                    s,
                    user,
                    lambda st, now: core.purchase(st, tier, period, now, upgrade=upgrade),
                    kind=pay.kind,
                    tier=tier,
                    seconds=pay.period_days * 86400,
                    amount=pay.amount,
                    meta={"payment_id": pay.id},
                )
                pay.status = "succeeded"
                pay.paid_at = utcnow()
                pay.applied = True
                if pay.promo_id:
                    use = await s.scalar(
                        select(PromoUse).where(PromoUse.promo_id == pay.promo_id, PromoUse.user_id == user_id)
                    )
                    if use is not None:
                        use.status = "used"
                        use.payment_id = pay.id
                    if user.pending_promo_id == pay.promo_id:
                        user.pending_promo_id = None
                await s.commit()
            await subs.sync_inside_lock(user_id)

        await self._after_success(pay, user, old, new)
        return pay

    async def _after_success(self, pay: Payment, user: User, old: SubState, new: SubState) -> None:
        tier = Tier(pay.plan_code)
        extra = ""
        if pay.kind == "upgrade":
            extra = "\n\nОстаток LITE превращён в PREMIUM — ничего не потерялось."
        elif tier == Tier.LITE and new.tier == Tier.PREMIUM and new.queued_lite > core.ZERO:
            start = new.expires_at
            extra = f"\n\nLITE начнётся сразу после PREMIUM — с {fmt_dt(start)}."
        text = self.ctx.texts.render(
            "pay_success", plan=tier.title, until=fmt_dt(new.total_end), extra=extra
        )
        markup = kb([btn("Настройка", "setup:open"), btn("Моя подписка", "menu:sub")])
        await self.ctx.notify.send(user.id, text, markup)

        if self.ctx.cfg.get("notify_sales"):
            who = f"@{user.username}" if user.username else esc(user.display_name)
            await self.ctx.notify.to_admins(
                f"💰 <b>Оплата {money(pay.amount)}</b> · {tier.title}"
                f"{' (апгрейд)' if pay.kind == 'upgrade' else ''}\n{who} · <code>{user.id}</code>"
            )

    async def _apply_canceled(self, pay_id: int, obj: dict) -> Payment | None:
        reason = ((obj.get("cancellation_details") or {}).get("reason")) or "canceled"
        async with self.ctx.session() as s:
            pay = await s.scalar(select(Payment).where(Payment.id == pay_id).with_for_update())
            if pay is None or pay.status != "pending":
                return pay
            pay.status = "canceled"
            pay.cancel_reason = str(reason)[:128]
            await s.commit()
        if pay.user_id and reason != "expired_on_confirmation":
            await self.ctx.notify.send(
                pay.user_id,
                self.ctx.texts.render("pay_canceled"),
                kb(btn("Оплатить подписку", "pay:menu")),
            )
        return pay

    # ------------------------------------------------------------------ вебхук
    async def handle_event(self, payload: dict) -> None:
        event = str(payload.get("event", ""))
        obj = payload.get("object") or {}
        if event.startswith("payment.") and obj.get("id"):
            await self.settle(str(obj["id"]))
        elif event == "refund.succeeded" and obj.get("payment_id"):
            await self._on_external_refund(obj)

    async def _on_external_refund(self, obj: dict) -> None:
        async with self.ctx.session() as s:
            pay = await s.scalar(select(Payment).where(Payment.external_id == str(obj["payment_id"])).with_for_update())
            if pay is None or pay.status == "refunded":
                return
            pay.status = "refunded"
            pay.refunded_at = utcnow()
            pay.refund_id = str(obj.get("id", ""))[:64]
            await s.commit()
        await self.ctx.notify.to_admins(
            f"↩️ По платежу #{pay.id} ({money(pay.amount)}) оформлен возврат в ЮKassa. "
            "Дни подписки автоматически не снимались — при необходимости снимите их в карточке пользователя."
        )

    # ------------------------------------------------------------------ возврат
    async def refund(self, pay_id: int, actor_id: int, *, remove_days: bool) -> Payment:
        pay = await self.get(pay_id)
        if pay is None or pay.status != "succeeded" or not pay.external_id or self.ctx.yk is None:
            raise PaymentError("not_refundable")
        try:
            res = await self.ctx.yk.create_refund(
                payment_id=pay.external_id,
                amount=pay.amount,
                idempotence_key=f"fable-refund-{pay.id}",
                description=f"Возврат платежа #{pay.id}",
            )
        except YooKassaError as e:
            raise PaymentError("provider", str(e)) from e
        if res.get("status") == "canceled":
            raise PaymentError("provider", "возврат отклонён ЮKassa")
        await self._set_status(pay_id, "refunded", refunded_at=utcnow(), refund_id=str(res.get("id", ""))[:64])
        if remove_days and pay.user_id:
            await self.ctx.subs.remove_days(
                pay.user_id, Tier(pay.plan_code), pay.period_days, actor_id=actor_id, kind="refund"
            )
        await self.ctx.audit.log(actor_id, "payment.refund", pay_id, remove_days=remove_days, amount=str(pay.amount))
        return await self.get(pay_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ воркер
    async def reconcile_pending(self, limit: int = 50) -> int:
        """Добирает статусы платежей, по которым не пришло уведомление."""
        if self.ctx.yk is None:
            return 0
        now = utcnow()
        async with self.ctx.session() as s:
            rows = (
                await s.execute(
                    select(Payment)
                    .where(Payment.status == "pending", Payment.created_at < now - timedelta(seconds=75))
                    .order_by(Payment.id)
                    .limit(limit)
                )
            ).scalars().all()
        done = 0
        for p in rows:
            try:
                if p.external_id:
                    res = await self.settle(p.external_id)
                    done += 1 if res and res.status != "pending" else 0
                elif now - p.created_at > timedelta(minutes=10):
                    await self._set_status(p.id, "canceled", cancel_reason="no_external_id")
            except YooKassaError as e:
                log.warning("reconcile %s: %s", p.id, e)
            except Exception:  # noqa: BLE001
                log.exception("reconcile %s", p.id)
        return done
