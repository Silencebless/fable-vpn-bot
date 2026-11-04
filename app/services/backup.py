from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from aiogram.types import BufferedInputFile
from sqlalchemy.engine import make_url

from app.utils import fmt_dt, to_local, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

TG_LIMIT = 49 * 1024 * 1024  # лимит загрузки файла ботом ~50 МБ


class BackupError(Exception):
    pass


class BackupService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def dump(self) -> tuple[bytes, str]:
        """Делает pg_dump (формат custom, уже сжат). Возвращает (байты, имя файла)."""
        url = make_url(self.ctx.settings.database_url)
        env = {**os.environ, "PGPASSWORD": url.password or ""}
        cmd = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "-h", url.host or "localhost",
            "-p", str(url.port or 5432),
            "-U", url.username or "postgres",
            "-d", url.database or "postgres",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except FileNotFoundError as e:
            raise BackupError("pg_dump не найден в контейнере") from e
        except asyncio.TimeoutError as e:
            raise BackupError("pg_dump завис (таймаут 5 минут)") from e
        if proc.returncode != 0:
            raise BackupError(err.decode(errors="replace")[:400] or f"pg_dump завершился с кодом {proc.returncode}")
        name = f"fable-backup-{to_local(utcnow()).strftime('%Y-%m-%d_%H%M')}.dump"
        return out, name

    async def send(self, chat_ids: set[int] | list[int]) -> bool:
        try:
            data, name = await self.dump()
        except BackupError as e:
            log.error("backup failed: %s", e)
            for cid in chat_ids:
                await self.ctx.notify.send(cid, f"⚠️ Бэкап БД не удался: {e}")
            return False
        if len(data) > TG_LIMIT:
            for cid in chat_ids:
                await self.ctx.notify.send(
                    cid, f"⚠️ Бэкап ({len(data) // 1024 // 1024} МБ) не помещается в Telegram. Снимите дамп на сервере вручную."
                )
            return False
        caption = f"💾 Бэкап БД · {fmt_dt(utcnow())} · {len(data) // 1024} КБ"
        for cid in chat_ids:
            try:
                await self.ctx.bot.send_document(cid, BufferedInputFile(data, name), caption=caption)
            except Exception:  # noqa: BLE001
                log.exception("не удалось отправить бэкап в %s", cid)
        return True

    def targets(self) -> set[int]:
        if self.ctx.settings.backup_chat_id:
            return {self.ctx.settings.backup_chat_id}
        return set(self.ctx.settings.owners)
