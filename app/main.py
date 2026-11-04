"""Точка входа: поднимает БД, Redis, клиентов, веб-сервер, воркеры и запускает polling."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeChat
from redis.asyncio import Redis
from sqlalchemy import select

from app.bot.factory import create_dispatcher
from app.config import get_settings
from app.context import AppContext
from app.db.base import create_engine, create_sessionmaker, init_db
from app.db.models import Admin
from app.integrations.remnawave import RemnaClient
from app.integrations.yookassa import YooKassaClient
from app.logging_setup import setup_logging
from app.services import build_services
from app.utils import set_timezone
from app.web.server import start_web
from app.workers.runner import Workers

log = logging.getLogger("app")


async def _init_db_with_retry(engine) -> None:
    for attempt in range(1, 31):
        try:
            await init_db(engine)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("БД ещё не готова (%s/30): %s", attempt, e)
            await asyncio.sleep(2)
    raise RuntimeError("Не удалось подключиться к базе данных")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    set_timezone(settings.timezone)

    if not settings.remnawave_token:
        log.warning("REMNAWAVE_TOKEN не задан — работа с панелью VPN не будет функционировать")
    if not settings.yookassa_enabled:
        log.warning("YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY не заданы — приём платежей отключён")

    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    await _init_db_with_retry(engine)

    redis = Redis.from_url(settings.redis_url)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    remna = RemnaClient(
        settings.remnawave_url,
        settings.remnawave_token,
        proxy_headers=settings.remnawave_proxy_headers,
        caddy_key=settings.remnawave_caddy_key,
        cookie=settings.remnawave_cookie,
    )
    yk = YooKassaClient(settings.yookassa_shop_id, settings.yookassa_secret_key) if settings.yookassa_enabled else None

    ctx = AppContext(settings, sessionmaker, redis, bot, remna, yk)
    build_services(ctx)

    me = await bot.get_me()
    ctx.bot_username = me.username or ""
    log.info("Бот: @%s", ctx.bot_username)

    await ctx.cfg.load()
    await ctx.texts.load()
    await ctx.plans.ensure_defaults()
    async with ctx.session() as s:
        ctx.admin_ids = {a.user_id for a in (await s.scalars(select(Admin))).all()}

    dp = create_dispatcher(ctx, RedisStorage(redis, state_ttl=86400, data_ttl=86400))
    web_runner = await start_web(ctx)
    workers = Workers(ctx)
    workers.start()
    await ctx.bc.resume_running()

    try:
        base = [BotCommand(command="start", description="Главное меню"), BotCommand(command="menu", description="Меню")]
        await bot.set_my_commands(base)
        for owner in settings.owners:
            await bot.set_my_commands(
                base + [BotCommand(command="admin", description="Админ-панель")], scope=BotCommandScopeChat(chat_id=owner)
            )
    except Exception:  # noqa: BLE001
        log.warning("не удалось выставить команды бота", exc_info=True)

    try:
        if await redis.set("boot:notify", 1, ex=600, nx=True):
            await ctx.notify.to_admins("🟢 <b>Fable | VPN запущен.</b> Открой /admin → Система → Диагностика, чтобы проверить подключения.")
    except Exception:  # noqa: BLE001
        pass

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Останавливаюсь…")
        await workers.stop()
        await ctx.bc.shutdown()
        await web_runner.cleanup()
        await remna.close()
        if yk is not None:
            await yk.close()
        await bot.session.close()
        await redis.aclose()
        await engine.dispose()
