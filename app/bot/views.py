"""Экраны пользовательской части: каждая функция возвращает (текст, клавиатура)."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.ui import btn, kb
from app.core.pricing import PROMO_DAYS
from app.core.subscription import Tier
from app.db.models import User
from app.integrations.remnawave import RemnaError
from app.services.devices import platform_emoji
from app.utils import days_left, days_word, esc, fmt_date, fmt_dt, money, money_btn, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

Screen = tuple[str, InlineKeyboardMarkup]


def support_button(ctx: AppContext) -> InlineKeyboardButton:
    url = ctx.support_url
    return btn("Поддержка", url=url) if url else btn("Поддержка", "sup:none")


# ───────────────────────── статус ─────────────────────────
def status_line(ctx: AppContext, user: User) -> str:
    now = utcnow()
    st = ctx.subs.view(user, now)
    if st.is_active(now):
        line = f"🟢 <b>{st.tier.title}</b> · до {fmt_date(st.expires_at)} (ещё {days_word(days_left(st.expires_at, now))})"
        if st.queued_lite.total_seconds() > 0:
            line += f"\n➕ затем LITE до {fmt_date(st.total_end)}"
        return line
    if user.remna_id or user.trial_used:
        return "🔴 Подписка не активна"
    return "⚪️ Подписки пока нет"


def main_menu(ctx: AppContext, user: User) -> Screen:
    text = ctx.texts.render("main_menu", status=status_line(ctx, user))
    rows = [
        btn("Моя подписка", "menu:sub"),
        btn("Реферальная система", "ref:menu"),
        support_button(ctx),
    ]
    if ctx.is_admin(user.id):
        rows.append(btn("⚙️ Админ-панель", "a:home"))
    return text, kb(*rows)


# ───────────────────────── моя подписка ─────────────────────────
async def _devices_count(ctx: AppContext, user: User) -> int | None:
    if not user.remna_id:
        return 0
    try:
        return len(await asyncio.wait_for(ctx.devices.list(user), 4))
    except (RemnaError, asyncio.TimeoutError):
        return None


async def sub_menu(ctx: AppContext, user: User) -> Screen:
    now = utcnow()
    st = ctx.subs.view(user, now)
    active = st.is_active(now)
    lines: list[str] = []
    if active:
        lines += [
            "Статус: 🟢 активна",
            f"Тариф: <b>{st.tier.title}</b>",
            f"Действует до: <b>{fmt_dt(st.expires_at)}</b> (ещё {days_word(days_left(st.expires_at, now))})",
        ]
        if st.queued_lite.total_seconds() > 0:
            q_days = round(st.queued_lite.total_seconds() / 86400)
            lines.append(f"Дальше: LITE ещё {days_word(q_days)} — до {fmt_date(st.total_end)}")
        used = await _devices_count(ctx, user)
        limit = ctx.plans.get(st.tier).device_limit
        lines.append(f"Устройства: {'—' if used is None else used} из {limit}")
        if user.pending_promo_id:
            lines.append("🎟 Есть активный промокод со скидкой")
    elif user.remna_id or user.trial_used:
        lines += ["Статус: 🔴 закончилась", f"Была до: {fmt_dt(st.expires_at)}", "", "Оформи тариф, чтобы вернуть доступ."]
    else:
        lines += ["Статус: ⚪️ подписки пока нет", "", "Выбери тариф — подключение займёт минуту."]

    text = ctx.texts.render("sub_menu", details="\n".join(lines))
    markup = kb(
        btn("Продлить подписку" if active else "Оплатить подписку", "pay:menu"),
        btn("Настройка", "setup:open"),
        btn("Список устройств", "dev:list"),
        btn("Назад", "menu:main"),
    )
    return text, markup


# ───────────────────────── тарифы и оплата ─────────────────────────
def pay_menu(ctx: AppContext, user: User) -> Screen:
    plans = ctx.plans.active()
    summary = "\n\n".join(ctx.plans.short_line(p) for p in plans) or "Тарифы временно недоступны."
    text = ctx.texts.render("pay_menu", status=status_line(ctx, user), plans=summary)
    rows = [btn(ctx.plans.button_label(p), f"pay:plan:{p.code}") for p in plans]
    rows.append(btn("Назад", "menu:sub"))
    return text, kb(*rows)


async def plan_view(ctx: AppContext, user: User, code: str) -> Screen | None:
    if code not in ("lite", "premium"):
        return None
    plan = ctx.plans.get(code)
    if not plan.is_active:
        return None
    tier = Tier(code)
    q, promo = await ctx.pay.quote(user, tier, upgrade=True)
    st = q.before
    t = ctx.texts
    now = utcnow()

    if q.kind == "upgrade":
        context = t.render(
            "ctx_upgrade",
            until=fmt_date(st.expires_at),
            left=days_word(days_left(st.expires_at, now)),
            extra=money(q.extra),
            premium_price=money(plan.price),
            total=money(q.base),
            new_until=fmt_date(q.after.total_end),
        )
    elif st.is_active(now) and st.tier == Tier.PREMIUM and tier == Tier.LITE:
        context = t.render(
            "ctx_queue",
            until=fmt_date(st.expires_at),
            period=days_word(q.period_days),
            new_until=fmt_date(q.after.total_end),
        )
    elif st.is_active(now) and st.tier == tier:
        context = t.render(
            "ctx_renew", tier=tier.title, until=fmt_date(st.expires_at), new_until=fmt_date(q.after.expires_at)
        )
    else:
        context = t.render("ctx_new", new_until=fmt_date(q.after.total_end))

    if q.discount > 0 and promo is not None:
        context += (
            f"\n\n🎟 Промокод <b>{esc(promo.code)}</b>: −{money(q.discount)}. "
            f"К оплате: <b>{money(q.amount)}</b>"
        )

    text = t.render(
        "plan_view",
        title=esc(plan.title),
        price=money(plan.price),
        period=days_word(plan.period_days),
        features=ctx.plans.features_block(plan),
        context=context,
    )
    if q.kind == "upgrade":
        markup = kb(
            btn(f"Доплатить {money_btn(q.amount)} и перейти на {plan.title}", f"pay:go:{code}:up"),
            btn("Дождаться окончания LITE", "menu:sub"),
            btn("Назад", "pay:menu"),
        )
    else:
        markup = kb(btn("Оплатить", f"pay:go:{code}"), btn("Назад", "pay:menu"))
    return text, markup


# ───────────────────────── настройка ─────────────────────────
def setup_screen(ctx: AppContext, user: User, back_cb: str = "menu:sub", back_text: str = "Назад") -> Screen:
    if not user.sub_url or not user.short_uuid:
        return ctx.texts.render("setup_no_sub"), kb(btn("Оплатить подписку", "pay:menu"), btn(back_text, back_cb))
    text = ctx.texts.render("setup", sub_block=f"<code>{esc(user.sub_url)}</code>")
    if not ctx.subs.view(user).is_active(utcnow()):
        text += "\n\n⚠️ Подписка сейчас не активна — продли её, чтобы VPN заработал."
    markup = kb(
        btn("Скачать Happ", url=ctx.dl_url()) if ctx.web_ready else None,
        btn("Добавить подписку", url=ctx.add_url(user.short_uuid)) if ctx.web_ready else None,
        support_button(ctx),
        btn(back_text, back_cb),
    )
    return text, markup


# ───────────────────────── устройства ─────────────────────────
async def devices_screen(ctx: AppContext, user: User) -> Screen:
    now = utcnow()
    st = ctx.subs.view(user, now)
    if not st.is_active(now) or not user.remna_id:
        return ctx.texts.render("setup_no_sub"), kb(btn("Оплатить подписку", "pay:menu"), btn("Назад", "menu:sub"))
    try:
        devices = await asyncio.wait_for(ctx.devices.list(user), 8)
    except (RemnaError, asyncio.TimeoutError):
        return ctx.texts.render("panel_unavailable"), kb(btn("Назад", "menu:sub"))

    limit = ctx.devices.limit_for(user)
    if devices:
        lines = "\n".join(f"{i}. {platform_emoji(d.platform)} {esc(d.label)}" for i, d in enumerate(devices, 1))
    else:
        lines = ctx.texts.render("devices_empty")
    text = ctx.texts.render("devices_menu", used=len(devices), limit=limit, list=lines)
    rows: list = [btn(d.button, f"dev:v:{d.key}") for d in devices]
    rows += [btn("Добавить устройство", "dev:add"), btn("Назад", "menu:sub")]
    return text, kb(*rows)


async def device_limit_screen(ctx: AppContext, user: User) -> Screen:
    """Лимит исчерпан: предлагаем удалить устройство или перейти на PREMIUM."""
    devices = await ctx.devices.list(user)
    limit = ctx.devices.limit_for(user)
    st = ctx.subs.view(user)
    hint = ""
    premium = ctx.plans.get(Tier.PREMIUM)
    can_upgrade = st.tier == Tier.LITE and premium.is_active and premium.device_limit > limit
    if can_upgrade:
        hint = ctx.texts.render("device_limit_upgrade", premium_limit=premium.device_limit)
    text = ctx.texts.render("device_limit", used=len(devices), limit=limit, upgrade_hint=hint)
    rows: list = [btn(f"🗑 {d.label}", f"dev:del:{d.key}") for d in devices]
    if can_upgrade:
        rows.append(btn("Перейти на PREMIUM", "pay:plan:premium"))
    rows.append(btn("Назад", "dev:list"))
    return text, kb(*rows)


# ───────────────────────── рефералка ─────────────────────────
async def referral_screen(ctx: AppContext, user: User) -> Screen:
    if not ctx.cfg.get("referral_enabled"):
        return ctx.texts.render("ref_disabled"), kb(btn("Ввести промокод", "promo:ask"), btn("Назад", "menu:main"))
    stats = await ctx.refs.stats(user.id)
    link = ctx.ref_link(user.ref_code)
    tier = Tier(ctx.cfg.get("referral_tier"))
    bonus_days = int(ctx.cfg.get("referral_days"))
    text = ctx.texts.render(
        "ref_menu",
        bonus=days_word(bonus_days),
        tier=tier.title,
        link=link,
        invited=stats["invited"],
        connected=stats["connected"],
        earned=days_word(stats["days"]),
    )
    trial = (
        f"{days_word(int(ctx.cfg.get('trial_days')))} {Tier(ctx.cfg.get('trial_tier')).title}"
        if ctx.cfg.get("trial_enabled")
        else "доступ"
    )
    share_text = ctx.texts.render("ref_share", trial_days=trial)
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    markup = kb(
        btn("Пригласить друга", url=share_url),
        btn("Ввести промокод", "promo:ask"),
        btn("Назад", "menu:main"),
    )
    return text, markup


def promo_ok_text(ctx: AppContext, promo, new_state) -> str:
    from app.services.promos import describe_value

    if promo.kind == PROMO_DAYS:
        return ctx.texts.render(
            "promo_ok_days",
            days=int(promo.value),
            tier=str(promo.days_tier).upper(),
            until=fmt_dt(new_state.total_end),
        )
    tiers = ""
    if promo.tiers:
        tiers = " (тариф " + ", ".join(str(x).upper() for x in promo.tiers) + ")"
    return ctx.texts.render("promo_ok_discount", discount=describe_value(promo), tiers=tiers)
