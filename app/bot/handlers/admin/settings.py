from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import ask, make_router, on_input
from app.bot.ui import btn, kb, show
from app.services.runtime_settings import META_BY_KEY, SETTING_META
from app.utils import esc, parse_int

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("settings")

INT_BOUNDS = {
    "trial_days": (1, 365),
    "referral_days": (1, 365),
    "referral_min_mb": (0, 100000),
    "referral_max_rewards": (0, 100000),
}
BACKS = {"s": "a:s", "r": "a:r"}
HINTS = {
    "trial_enabled": "Если выключить, новые пользователи не получат пробный доступ.",
    "trial_days": "Длительность пробного периода для новых пользователей.",
    "trial_tier": "На каком тарифе выдаётся пробный период.",
    "registration_open": "Если выключить, новые пользователи не смогут начать пользоваться ботом (старые — смогут).",
    "payments_enabled": "Если выключить, кнопки оплаты покажут «временно недоступно».",
    "receipts_enabled": "Передавать в ЮKassa данные чека (нужен email клиента). Отключай, только если чеки у тебя не нужны.",
    "maintenance": "Бот отвечает «технические работы» всем, кроме админов.",
    "reminders_enabled": "Напоминать пользователям об окончании подписки.",
    "reminder_days": "За сколько дней напоминать, через запятую: например «3,1».",
    "trim_devices_on_downgrade": "При переходе PREMIUM → LITE отключать самые новые устройства сверх лимита LITE.",
    "notify_sales": "Присылать владельцам уведомление о каждой оплате.",
    "alerts_nodes": "Присылать владельцам алерт, если нода пропала из сети (нужен вебхук Remnawave).",
    "support_url": "Ссылка для кнопки «Поддержка», например https://t.me/your_support. Пусто — берётся из .env.",
    "referral_enabled": "Включает/выключает реферальную программу.",
    "referral_days": "Сколько дней получает пригласивший.",
    "referral_tier": "Какой тариф получает пригласивший.",
    "referral_min_mb": "Сколько МБ трафика должен потратить друг, чтобы считаться «воспользовался VPN» (или первое подключение).",
    "referral_max_rewards": "Максимум наград на одного пригласившего. 0 — без ограничений.",
}


@router.callback_query(F.data == "a:s")
async def cb_menu(cb: CallbackQuery, ctx: AppContext) -> None:
    rows = [btn(f"{title}: {ctx.cfg.format_value(key)}"[:62], f"a:sv:{key}:s") for key, title, _ in SETTING_META]
    rows.append(btn("← Назад", "a:home"))
    await show(cb, "⚙️ <b>Настройки</b>\n\nВсё применяется сразу, без перезапуска.", kb(*rows))


async def view_setting(event: CallbackQuery | Message, ctx: AppContext, key: str, back: str, flash: str = "") -> None:
    title, kind = META_BY_KEY[key]
    back_cb = BACKS.get(back, "a:s")
    text = (
        (f"{flash}\n\n" if flash else "")
        + f"⚙️ <b>{esc(title)}</b>\n\nСейчас: <b>{esc(ctx.cfg.format_value(key))}</b>\n\n<i>{esc(HINTS.get(key, ''))}</i>"
    )
    rows = []
    if kind == "bool":
        rows.append(btn("Выключить" if ctx.cfg.get(key) else "Включить", f"a:sb:{key}:{back}"))
    elif kind == "tier":
        rows.append([btn("LITE", f"a:sst:{key}:lite:{back}"), btn("PREMIUM", f"a:sst:{key}:premium:{back}")])
    else:
        rows.append(btn("✏️ Изменить", f"a:se:{key}:{back}"))
    if ctx.cfg.is_overridden(key):
        rows.append(btn("♻️ Вернуть по умолчанию", f"a:sr:{key}:{back}"))
    rows.append(btn("← Назад", back_cb))
    await show(event, text, kb(*rows))


@router.callback_query(F.data.startswith("a:sv:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, key, back = (cb.data or "").split(":")
    await view_setting(cb, ctx, key, back)


@router.callback_query(F.data.startswith("a:sb:"))
async def cb_toggle(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, key, back = (cb.data or "").split(":")
    new = not ctx.cfg.get(key)
    await ctx.cfg.set(key, new)
    await ctx.audit.log(cb.from_user.id, "setting.set", key, value=new)
    await view_setting(cb, ctx, key, back, "✅ Сохранено")


@router.callback_query(F.data.startswith("a:sst:"))
async def cb_tier(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, key, tier, back = (cb.data or "").split(":")
    await ctx.cfg.set(key, tier)
    await ctx.audit.log(cb.from_user.id, "setting.set", key, value=tier)
    await view_setting(cb, ctx, key, back, "✅ Сохранено")


@router.callback_query(F.data.startswith("a:sr:"))
async def cb_reset(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, key, back = (cb.data or "").split(":")
    await ctx.cfg.reset(key)
    await ctx.audit.log(cb.from_user.id, "setting.reset", key)
    await view_setting(cb, ctx, key, back, "♻️ Возвращено значение по умолчанию")


@router.callback_query(F.data.startswith("a:se:"))
async def cb_edit(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, key, back = (cb.data or "").split(":")
    title, kind = META_BY_KEY[key]
    hint = {
        "int": "Отправь число.",
        "text": "Отправь текст (или «-», чтобы очистить).",
        "days_list": "Отправь числа дней через запятую, например: 3,1",
    }[kind]
    await ask(cb, state, "s_edit", f"✏️ <b>{esc(title)}</b>\n\n{hint}", f"a:sv:{key}:{back}", key=key, back=back)


@on_input("s_edit")
async def in_edit(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    key, back = str(data["key"]), str(data["back"])
    _, kind = META_BY_KEY[key]
    raw = (message.text or "").strip()
    value: object
    if kind == "int":
        lo, hi = INT_BOUNDS.get(key, (0, 100000))
        parsed = parse_int(raw, lo=lo, hi=hi)
        if parsed is None:
            await message.answer(f"Нужно целое число от {lo} до {hi}.")
            return
        value = parsed
    elif kind == "days_list":
        try:
            nums = sorted({int(x) for x in raw.replace(" ", "").split(",") if x}, reverse=True)
        except ValueError:
            nums = []
        if not nums or len(nums) > 5 or any(n < 1 or n > 30 for n in nums):
            await message.answer("Нужно от 1 до 5 чисел (1–30) через запятую, например: 3,1")
            return
        value = nums
    else:
        value = "" if raw == "-" else raw[:300]
        if value and not str(value).startswith(("http://", "https://")) and key == "support_url":
            await message.answer("Ссылка должна начинаться с https:// (например https://t.me/your_support).")
            return
    await state.clear()
    await ctx.cfg.set(key, value)
    await ctx.audit.log(message.from_user.id, "setting.set", key, value=str(value)[:100])
    await view_setting(message, ctx, key, back, "✅ Сохранено")
