"""Контейнер зависимостей приложения (передаётся в хендлеры как ``ctx``)."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.integrations.remnawave import RemnaClient
from app.integrations.yookassa import YooKassaClient
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.services.audit import AuditService
    from app.services.backup import BackupService
    from app.services.broadcast import BroadcastService
    from app.services.devices import DeviceService
    from app.services.notifier import Notifier
    from app.services.payments import PaymentService
    from app.services.plans import PlanService
    from app.services.promos import PromoService
    from app.services.referrals import ReferralService
    from app.services.runtime_settings import SettingsService
    from app.services.stats import StatsService
    from app.services.subscriptions import SubscriptionService
    from app.services.text_service import TextService
    from app.services.users import UserService


class AppContext:
    cfg: SettingsService
    texts: TextService
    users: UserService
    plans: PlanService
    subs: SubscriptionService
    pay: PaymentService
    promos: PromoService
    refs: ReferralService
    devices: DeviceService
    stats: StatsService
    bc: BroadcastService
    audit: AuditService
    notify: Notifier
    backup: BackupService

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        bot: Bot,
        remna: RemnaClient,
        yk: YooKassaClient | None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.bot = bot
        self.remna = remna
        self.yk = yk
        self.bot_username: str = ""
        self.admin_ids: set[int] = set()
        self.started_at: datetime = utcnow()
        self.heartbeats: dict[str, datetime] = {}
        self.last_webhook: dict[str, datetime] = {}

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    # --- права ---
    def is_owner(self, user_id: int) -> bool:
        return user_id in self.settings.owners

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.owners or user_id in self.admin_ids

    def all_admin_ids(self) -> set[int]:
        return set(self.settings.owners) | set(self.admin_ids)

    # --- ссылки ---
    @property
    def support_url(self) -> str:
        raw = (self.cfg.get("support_url") or self.settings.support_url or "").strip()
        if raw.startswith("@"):
            raw = "https://t.me/" + raw[1:]
        elif raw.startswith(("t.me/", "telegram.me/")):
            raw = "https://" + raw
        return raw if raw.startswith(("http://", "https://", "tg://")) else ""

    @property
    def web_ready(self) -> bool:
        """Публичный адрес настроен (Telegram не принимает localhost в URL-кнопках)."""
        u = self.settings.base_url.lower()
        return u.startswith(("http://", "https://")) and not any(h in u for h in ("localhost", "127.0.0.1", "0.0.0.0"))

    def ref_link(self, ref_code: str) -> str:
        return f"https://t.me/{self.bot_username}?start=ref_{ref_code}"

    def dl_url(self) -> str:
        return f"{self.settings.base_url}/dl"

    def add_url(self, short_uuid: str) -> str:
        return f"{self.settings.base_url}/add/{short_uuid}"
