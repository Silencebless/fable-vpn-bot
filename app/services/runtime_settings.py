"""Настройки, которые владелец меняет прямо из бота (хранятся в БД, кэшируются в памяти)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Setting
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # пробный период
    "trial_enabled": True,
    "trial_days": 7,
    "trial_tier": "premium",
    # доступ
    "registration_open": True,
    "maintenance": False,
    "payments_enabled": True,
    "receipts_enabled": True,          # перезаписывается значением из .env при старте
    # уведомления
    "reminders_enabled": True,
    "reminder_days": [3, 1],
    "notify_sales": True,
    "alerts_nodes": True,
    # устройства
    "trim_devices_on_downgrade": True,
    # поддержка
    "support_url": "",
    # рефералка
    "referral_enabled": True,
    "referral_days": 7,
    "referral_tier": "lite",
    "referral_min_mb": 10,
    "referral_max_rewards": 0,         # 0 = без ограничений
}

# (ключ, название, тип) — для экрана «Настройки»
SETTING_META: list[tuple[str, str, str]] = [
    ("trial_enabled", "Пробный период", "bool"),
    ("trial_days", "Дней пробного периода", "int"),
    ("trial_tier", "Тариф пробного периода", "tier"),
    ("registration_open", "Приём новых пользователей", "bool"),
    ("payments_enabled", "Приём платежей", "bool"),
    ("receipts_enabled", "Чеки (данные для ЮKassa)", "bool"),
    ("maintenance", "Режим техработ", "bool"),
    ("reminders_enabled", "Напоминания об окончании", "bool"),
    ("reminder_days", "За сколько дней напоминать", "days_list"),
    ("trim_devices_on_downgrade", "Отключать лишние устройства при переходе на LITE", "bool"),
    ("notify_sales", "Уведомлять владельцев об оплатах", "bool"),
    ("alerts_nodes", "Алерты о падении нод", "bool"),
    ("support_url", "Ссылка на поддержку", "text"),
]

REFERRAL_META: list[tuple[str, str, str]] = [
    ("referral_enabled", "Реферальная программа", "bool"),
    ("referral_days", "Бонус пригласившему, дней", "int"),
    ("referral_tier", "Тариф бонуса", "tier"),
    ("referral_min_mb", "Порог «пользуется VPN», МБ трафика", "int"),
    ("referral_max_rewards", "Макс. наград на одного (0 = без лимита)", "int"),
]

META_BY_KEY = {k: (t, kind) for k, t, kind in SETTING_META + REFERRAL_META}


class SettingsService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._cache: dict[str, Any] = {}
        self._defaults = dict(DEFAULTS)
        self._defaults["receipts_enabled"] = ctx.settings.yookassa_receipts

    async def load(self) -> None:
        async with self.ctx.session() as s:
            rows = (await s.execute(select(Setting))).scalars().all()
        self._cache = {r.key: r.value for r in rows}

    def get(self, key: str) -> Any:
        if key in self._cache:
            return self._cache[key]
        return self._defaults[key]

    def is_overridden(self, key: str) -> bool:
        return key in self._cache

    async def set(self, key: str, value: Any) -> None:
        if key not in self._defaults:
            raise KeyError(key)
        async with self.ctx.session() as s:
            stmt = pg_insert(Setting).values(key=key, value=value, updated_at=utcnow())
            stmt = stmt.on_conflict_do_update(
                index_elements=[Setting.key],
                set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
            )
            await s.execute(stmt)
            await s.commit()
        self._cache[key] = value

    async def reset(self, key: str) -> None:
        async with self.ctx.session() as s:
            row = await s.get(Setting, key)
            if row:
                await s.delete(row)
                await s.commit()
        self._cache.pop(key, None)

    def format_value(self, key: str) -> str:
        _, kind = META_BY_KEY[key]
        v = self.get(key)
        if kind == "bool":
            return "включено ✅" if v else "выключено ⛔️"
        if kind == "days_list":
            return ", ".join(str(x) for x in v) or "—"
        if kind == "tier":
            return str(v).upper()
        if kind == "text":
            return str(v) or "— (берётся из .env)"
        return str(v)
