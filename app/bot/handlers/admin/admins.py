from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete

from app.bot.handlers.admin.common import ask, make_router, on_input
from app.bot.ui import btn, kb, show
from app.db.models import Admin
from app.utils import parse_int

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("admins", owner_only=True)


async def _screen(event: CallbackQuery | Message, ctx: AppContext, flash: str = "") -> None:
    owners = sorted(ctx.settings.owners)
    admins = sorted(ctx.admin_ids - ctx.settings.owners)
    lines = ["🛡 <b>Администраторы</b>\n"]
    if flash:
        lines.insert(0, flash + "\n")
    lines.append("👑 Владельцы (из .env): " + (", ".join(f"<code>{i}</code>" for i in owners) or "—"))
    lines.append("🛠 Админы: " + (", ".join(f"<code>{i}</code>" for i in admins) or "—"))
    lines.append(
        "\nАдмины видят всё, кроме: управления админами, возвратов, удаления пользователей, бэкапа и массового ресинка."
    )
    rows = [btn(f"🗑 Убрать {i}", f"a:md:{i}") for i in admins]
    rows.append(btn("➕ Добавить админа", "a:ma"))
    rows.append(btn("← Назад", "a:home"))
    await show(event, "\n".join(lines), kb(*rows))


@router.callback_query(F.data == "a:m")
async def cb_menu(cb: CallbackQuery, ctx: AppContext) -> None:
    await _screen(cb, ctx)


@router.callback_query(F.data == "a:ma")
async def cb_add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "m_add", "Отправь Telegram ID нового админа (число). Узнать ID можно у бота @userinfobot.", "a:m")


@on_input("m_add")
async def in_add(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    uid = parse_int(message.text, lo=1)
    if uid is None:
        await message.answer("Нужен числовой Telegram ID.")
        return
    await state.clear()
    async with ctx.session() as s:
        if await s.get(Admin, uid) is None:
            s.add(Admin(user_id=uid, added_by=message.from_user.id))
            await s.commit()
    ctx.admin_ids.add(uid)
    await ctx.audit.log(message.from_user.id, "admin.add", uid)
    await _screen(message, ctx, f"✅ Админ <code>{uid}</code> добавлен")


@router.callback_query(F.data.startswith("a:md:"))
async def cb_remove(cb: CallbackQuery, ctx: AppContext) -> None:
    uid = int((cb.data or "").split(":")[2])
    async with ctx.session() as s:
        await s.execute(delete(Admin).where(Admin.user_id == uid))
        await s.commit()
    ctx.admin_ids.discard(uid)
    await ctx.audit.log(cb.from_user.id, "admin.remove", uid)
    await _screen(cb, ctx, f"✅ Админ <code>{uid}</code> убран")
