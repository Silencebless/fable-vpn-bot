"""HTTP-сервер: страницы /dl и /add, вебхуки ЮKassa и Remnawave, healthcheck."""
from __future__ import annotations

import hmac
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web
from sqlalchemy import select

from app.db.models import User
from app.utils import esc, spawn, utcnow
from app.web import pages
from app.web.security import ip_allowed, verify_signature

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


SEC_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def client_ip(request: web.Request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else request.remote) or ""


def _html(text: str, status: int = 200) -> web.Response:
    return web.Response(text=text, status=status, content_type="text/html", charset="utf-8", headers=SEC_HEADERS)


def create_web_app(ctx: AppContext) -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app["ctx"] = ctx

    async def index(_: web.Request) -> web.Response:
        return _html(pages.landing_page(ctx.bot_username))

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def dl(_: web.Request) -> web.Response:
        return _html(pages.download_page())

    async def add_sub(request: web.Request) -> web.Response:
        short = request.match_info["short"]
        async with ctx.session() as s:
            sub_url = await s.scalar(select(User.sub_url).where(User.short_uuid == short))
        if not sub_url:
            return _html(pages.not_found_page(ctx.bot_username), 404)
        return _html(pages.add_subscription_page(sub_url))

    # ------------------------------------------------------------------ ЮKassa
    async def yookassa_hook(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.match_info["secret"], ctx.settings.webhook_secret):
            return web.Response(status=404)
        if ctx.settings.yookassa_check_ip and not ip_allowed(client_ip(request)):
            log.warning("уведомление ЮKassa с чужого IP %s", client_ip(request))
            return web.Response(status=403)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400)
        ctx.last_webhook["yookassa"] = utcnow()
        # отвечаем сразу; если обработка упадёт — платёж добёрет воркер reconcile
        spawn(ctx.pay.handle_event(payload))
        return web.Response(text="ok")

    # ------------------------------------------------------------------ Remnawave
    async def remnawave_hook(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.match_info["secret"], ctx.settings.webhook_secret):
            return web.Response(status=404)
        raw = await request.read()
        secret = ctx.settings.remnawave_webhook_secret
        if secret and not verify_signature(raw, request.headers.get("X-Remnawave-Signature", ""), secret):
            log.warning("вебхук Remnawave: неверная подпись")
            return web.Response(status=401)
        try:
            payload = json.loads(raw)
        except ValueError:
            return web.Response(status=400)
        ctx.last_webhook["remnawave"] = utcnow()
        spawn(_handle_remna_event(ctx, str(payload.get("event", "")), payload.get("data") or {}))
        return web.Response(text="ok")

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/dl", dl)
    app.router.add_get("/add/{short}", add_sub)
    app.router.add_post("/webhooks/yookassa/{secret}", yookassa_hook)
    app.router.add_post("/webhooks/remnawave/{secret}", remnawave_hook)
    return app


async def _handle_remna_event(ctx: AppContext, event: str, data: dict) -> None:
    if event == "user.first_connected":
        uid: int | None = None
        tg = data.get("telegramId")
        async with ctx.session() as s:
            if tg:
                uid = await s.scalar(select(User.id).where(User.id == int(tg)))
            if uid is None and data.get("id") is not None:
                uid = await s.scalar(select(User.id).where(User.remna_id == int(data["id"])))
        if uid:
            await ctx.refs.on_user_connected(uid)
        return

    if event.startswith("node.") and ctx.cfg.get("alerts_nodes"):
        name = esc(data.get("name") or data.get("address") or "нода")
        if "connection_lost" in event or "offline" in event:
            await ctx.notify.to_admins(f"🔴 <b>Нода недоступна:</b> {name}")
        elif "connection_restored" in event or "online" in event:
            await ctx.notify.to_admins(f"🟢 <b>Нода снова в сети:</b> {name}")


async def start_web(ctx: AppContext) -> web.AppRunner:
    app = create_web_app(ctx)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, ctx.settings.web_host, ctx.settings.web_port)
    await site.start()
    log.info("Web: слушаю %s:%s", ctx.settings.web_host, ctx.settings.web_port)
    return runner
