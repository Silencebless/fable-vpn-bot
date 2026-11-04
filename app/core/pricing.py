"""Цены, доплата за апгрейд, скидки по промокодам. Чистая логика без БД."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from app.core.subscription import SubState, Tier, normalize, purchase

PROMO_PERCENT = "percent"   # скидка в процентах
PROMO_FIXED = "fixed"       # скидка в рублях
PROMO_DAYS = "days"         # бонусные дни

MIN_PRICE = Decimal("1.00")  # ЮKassa не принимает платёж на 0 ₽
ONE = Decimal("1")
CENT = Decimal("0.01")


def q2(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def upgrade_extra(price_lite: Decimal, price_premium: Decimal, period_days: int, remaining: timedelta) -> Decimal:
    """Доплата за остаток LITE при переходе на PREMIUM: разница цен, пропорциональная остатку.

    Пример: LITE 149, PREMIUM 249, осталось 15 из 30 дней -> (249-149) * 15/30 = 50.
    Округляем вверх до целого рубля.
    """
    diff = Decimal(str(price_premium)) - Decimal(str(price_lite))
    secs = remaining.total_seconds()
    if diff <= 0 or secs <= 0 or period_days <= 0:
        return Decimal("0")
    frac = Decimal(str(secs)) / Decimal(period_days * 86400)
    return (diff * frac).quantize(ONE, rounding=ROUND_CEILING)


def discount_amount(amount: Decimal, kind: str | None, value: Decimal | None) -> Decimal:
    """Размер скидки в рублях (не опускаем цену ниже 1 ₽)."""
    if kind is None or value is None or amount <= MIN_PRICE:
        return Decimal("0.00")
    value = Decimal(str(value))
    if kind == PROMO_PERCENT:
        d = (amount * value / Decimal(100)).quantize(ONE, rounding=ROUND_HALF_UP)
    elif kind == PROMO_FIXED:
        d = value
    else:
        return Decimal("0.00")
    d = max(Decimal("0"), min(d, amount - MIN_PRICE))
    return q2(d)


@dataclass(frozen=True, slots=True)
class Quote:
    tier: Tier
    kind: str                 # "buy" | "upgrade"
    base: Decimal             # цена до скидки
    discount: Decimal         # скидка по промокоду
    amount: Decimal           # итог к оплате
    period_days: int
    extra: Decimal            # доплата за остаток LITE (только upgrade)
    remaining: timedelta      # остаток текущего отрезка (для пояснений)
    before: SubState          # состояние до покупки
    after: SubState           # состояние после покупки


def build_quote(
    state: SubState,
    tier: Tier,
    *,
    price_lite: Decimal,
    price_premium: Decimal,
    period_days: int,
    now: datetime,
    want_upgrade: bool = False,
    promo_kind: str | None = None,
    promo_value: Decimal | None = None,
) -> Quote:
    before = normalize(state, now)
    price = {Tier.LITE: Decimal(str(price_lite)), Tier.PREMIUM: Decimal(str(price_premium))}[tier]
    period = timedelta(days=period_days)

    is_upgrade = bool(want_upgrade and tier == Tier.PREMIUM and before.tier == Tier.LITE)
    extra = Decimal("0")
    remaining = before.remaining(now)
    if is_upgrade:
        extra = upgrade_extra(price_lite, price_premium, period_days, remaining)
    base = q2(price + extra)
    disc = discount_amount(base, promo_kind, promo_value)
    after = purchase(before, tier, period, now, upgrade=is_upgrade)
    return Quote(
        tier=tier,
        kind="upgrade" if is_upgrade else "buy",
        base=base,
        discount=disc,
        amount=q2(base - disc),
        period_days=period_days,
        extra=q2(extra),
        remaining=remaining,
        before=before,
        after=after,
    )
