from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.types import CallbackQuery
from sqlalchemy import func, or_, select, text as sql_text

from app.bot.handlers.admin.common import make_router, need_owner
from app.bot.ui import btn, confirm_kb, kb, show, toast
from app.db.models import User
from app.logging_setup import LOG_BUFFER
from app.utils import esc, fmt_dt, spawn, truncate, utcnow
from app.workers.runner import INTERVALS

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("system")

WORKER_INTERVALS = INTERVALS  # ожидаемый интервал воркеров, сек (для оценки «завис ли воркер»)


@router.callback_query(F.data == "a:y")
async def cb_menu(cb: CallbackQuery) -> None:
    markup = kb(
        [btn("🩺 Диагностика", "a:yd"), btn("💾 Бэкап БД", "a:yb")],
        [btn("🔄 Ресинк всех в панель", "a:yr"), btn("⚠️ Журнал ошибок", "a:ye")],
        [btn("🧾 Журнал действий", "a:ya")],
        [btn("← Назад", "a:home")],
    )
    await show(cb, "🧰 <b>Система</b>\n\nДиагностика, резервные копии и обслуживание — без консоли.", markup)


async def _timed(coro, timeout: float = 6.0):
    t0 = time.perf_counter()
    res = await asyncio.wait_for(coro, timeout)
    return res, int((time.perf_counter() - t0) * 1000)


@router.callback_query(F.data == "a:yd")
async def cb_diag(cb: CallbackQuery, ctx: AppContext) -> None:
    lines: list[str] = ["🩺 <b>Диагностика</b>\n"]
    warn: list[str] = []

    async def check(title: str, fn) -> None:
        try:
            detail = await fn()
            lines.append(f"✅ {title}{(' — ' + detail) if detail else ''}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ {title} — {esc(str(e) or type(e).__name__)[:120]}")

    async def db() -> str:
        async with ctx.session() as s:
            _, ms = await _timed(s.execute(sql_text("select 1")))
            n = await s.scalar(select(func.count()).select_from(User))
        return f"{ms} мс, пользователей {n}"

    async def redis() -> str:
        _, ms = await _timed(ctx.redis.ping())
        return f"{ms} мс"

    async def remna() -> str:
        _, ms = await _timed(ctx.remna.system_stats())
        return f"{ms} мс"

    async def yk() -> str:
        if ctx.yk is None:
            raise RuntimeError("ключи YOOKASSA_* не заданы")
        me, ms = await _timed(ctx.yk.me())
        mode = "тестовый магазин" if me.get("test") else "боевой магазин"
        fiscal = me.get("fiscalization_enabled")
        if fiscal and not ctx.cfg.get("receipts_enabled"):
            warn.append(
                "в ЮKassa включены чеки (фискализация), а бот не передаёт данные чека — платежи будут отклоняться. "
                "Включи «Чеки» в Настройках бота."
            )
        fiscal_txt = "чеки ЮKassa: " + ("вкл" if fiscal else "выкл") if fiscal is not None else ""
        return ", ".join(x for x in (mode, fiscal_txt, f"{ms} мс") if x)

    await check("PostgreSQL", db)
    await check("Redis", redis)
    await check("Remnawave API", remna)
    await check("ЮKassa API", yk)

    st = ctx.settings
    for p in ctx.plans.all():
        if p.is_active and not p.squads:
            warn.append(f"у тарифа {p.title} не выбраны сквады — подписчики не получат серверы (Тарифы → Сквады)")
    if not ctx.support_url:
        warn.append("не задана ссылка поддержки (Настройки или SUPPORT_URL)")
    if st.base_url.startswith("http://"):
        warn.append("PUBLIC_BASE_URL без https — вебхуки ЮKassa и страницы Happ не заработают")
    if st.webhook_secret in ("", "change-me"):
        warn.append("WEBHOOK_SECRET не изменён — задай случайную строку")
    if not st.owners:
        warn.append("OWNER_IDS пуст — некому управлять ботом")
    if not st.remnawave_webhook_secret:
        warn.append("REMNAWAVE_WEBHOOK_SECRET пуст — вебхуки Remnawave (награды сразу, алерты нод) отключены")

    now = utcnow()
    hb = []
    for name, interval in WORKER_INTERVALS.items():
        last = ctx.heartbeats.get(name)
        if last is None or (now - last).total_seconds() > interval * 3 + 30:
            hb.append(f"{name}: нет сигнала")
    lines.append("")
    lines.append("🔔 Вебхуки: ЮKassa — " + (fmt_dt(ctx.last_webhook["yookassa"]) if "yookassa" in ctx.last_webhook else "ещё не приходили")
                 + " · Remnawave — " + (fmt_dt(ctx.last_webhook["remnawave"]) if "remnawave" in ctx.last_webhook else "ещё не приходили"))
    lines.append(f"⚙️ Воркеры: {'все работают' if not hb else '⚠️ ' + ', '.join(hb)}")
    lines.append(f"⏱ Аптайм бота: {int((now - ctx.started_at).total_seconds() // 3600)} ч · @{ctx.bot_username}")
    if warn:
        lines.append("\n⚠️ <b>Что стоит поправить:</b>")
        lines += [f"• {esc(w)}" for w in warn]
    else:
        lines.append("\n🎉 Замечаний нет.")
    await show(cb, "\n".join(lines), kb(btn("🔄 Повторить", "a:yd"), btn("← Назад", "a:y")))


@router.callback_query(F.data == "a:yb")
async def cb_backup(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    await toast(cb, "Делаю бэкап… файл придёт отдельным сообщением.")
    ok = await ctx.backup.send({cb.from_user.id})
    await ctx.audit.log(cb.from_user.id, "backup.manual", ok=ok)


@router.callback_query(F.data == "a:yr")
async def cb_resync_ask(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    async with ctx.session() as s:
        n = int(await s.scalar(select(func.count()).select_from(User).where(or_(User.remna_id.is_not(None), User.tier.is_not(None)))) or 0)
    await show(
        cb,
        f"🔄 Отправить в панель актуальные срок, лимит устройств и сквады для <b>{n}</b> пользователей? "
        "Это безопасно, но займёт время — отчёт придёт сообщением.",
        confirm_kb("a:yro", "a:y", yes_text="Да, запустить"),
    )


async def _resync_all(ctx: AppContext, admin_id: int) -> None:
    async with ctx.session() as s:
        ids = list((await s.scalars(select(User.id).where(or_(User.remna_id.is_not(None), User.tier.is_not(None))))).all())
    ok, fail = await ctx.subs.resync_many(ids)
    await ctx.notify.send(admin_id, f"✅ Ресинк завершён: успешно {ok}, с ошибкой {fail} (из {len(ids)}).")


@router.callback_query(F.data == "a:yro")
async def cb_resync(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    spawn(_resync_all(ctx, cb.from_user.id))
    await ctx.audit.log(cb.from_user.id, "resync.all")
    await show(cb, "🔄 Ресинк запущен в фоне — пришлю отчёт.", kb(btn("← Назад", "a:y")))


@router.callback_query(F.data == "a:ye")
async def cb_errors(cb: CallbackQuery) -> None:
    lines = list(LOG_BUFFER)[-12:]
    body = "\n".join(truncate(esc(x), 300) for x in lines) or "Ошибок и предупреждений нет 🎉"
    await show(cb, f"⚠️ <b>Последние предупреждения и ошибки</b>\n\n<pre>{body[:3300]}</pre>", kb(btn("🔄 Обновить", "a:ye"), btn("← Назад", "a:y")))


@router.callback_query(F.data == "a:ya")
async def cb_audit(cb: CallbackQuery, ctx: AppContext) -> None:
    rows = await ctx.audit.recent(20)
    lines = [
        f"{fmt_dt(r.created_at)[:16]} · <code>{r.actor_id or '—'}</code> · {esc(r.action)}"
        + (f" · {esc(truncate(str(r.target), 20))}" if r.target else "")
        for r in rows
    ] or ["Пока пусто."]
    await show(cb, "🧾 <b>Журнал действий</b> (последние 20)\n\n" + "\n".join(lines), kb(btn("🔄 Обновить", "a:ya"), btn("← Назад", "a:y")))
