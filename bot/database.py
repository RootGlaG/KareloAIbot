import aiosqlite
import datetime
import logging
from typing import Optional, Dict, Any, List

from bot.config import DB_PATH, ADMIN_IDS

logger = logging.getLogger(__name__)


async def init_db() -> None:
    """Создаёт таблицы users, violations, chat_settings, chat_members и stats."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER NOT NULL,
                chat_id    INTEGER NOT NULL,
                username   TEXT DEFAULT '',
                full_name  TEXT DEFAULT '',
                warnings   INTEGER DEFAULT 0,
                last_violation_at TIMESTAMP,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS violations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                username     TEXT DEFAULT '',
                reason       TEXT NOT NULL,
                message_text TEXT DEFAULT '',
                action_taken TEXT NOT NULL,
                created_at   TIMESTAMP NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_members (
                user_id    INTEGER NOT NULL,
                chat_id    INTEGER NOT NULL,
                username   TEXT DEFAULT '',
                full_name  TEXT DEFAULT '',
                is_admin   INTEGER DEFAULT 0,
                last_seen  TIMESTAMP,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id         INTEGER PRIMARY KEY,
                botik_thread_id INTEGER DEFAULT NULL,
                ideas_thread_id INTEGER DEFAULT NULL
            )
        """)
        try:
            await db.execute("ALTER TABLE chat_settings ADD COLUMN ideas_thread_id INTEGER DEFAULT NULL")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE chat_members ADD COLUMN photo_url TEXT DEFAULT ''")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                key   TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        await db.execute("INSERT OR IGNORE INTO bot_stats (key, value) VALUES ('checked_messages', 0)")
        await db.execute("INSERT OR IGNORE INTO bot_stats (key, value) VALUES ('total_violations', 0)")
        await db.commit()
    logger.info("Database initialized at %s", DB_PATH)


async def upsert_member(
    user_id: int,
    chat_id: int,
    username: str = "",
    full_name: str = "",
    is_admin: bool = False,
    photo_url: str = ""
) -> None:
    """Добавляет или обновляет информацию об участнике чата."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Если это реальный ID и есть username, проверим и объединим с ручной записью (если была)
        if user_id > 0 and username:
            clean_u = username.strip().lstrip("@").lower()
            # Найдём псевдо-запись (отрицательный ID)
            async with db.execute(
                "SELECT user_id FROM chat_members WHERE LOWER(username) = ? AND chat_id = ? AND user_id < 0",
                (clean_u, chat_id)
            ) as cur:
                p_row = await cur.fetchone()
                if p_row:
                    pseudo_id = p_row[0]
                    # Перенесём варны с псевдо-ID на реальный ID
                    await db.execute("UPDATE OR IGNORE users SET user_id = ? WHERE user_id = ? AND chat_id = ?", (user_id, pseudo_id, chat_id))
                    await db.execute("UPDATE OR IGNORE violations SET user_id = ? WHERE user_id = ? AND chat_id = ?", (user_id, pseudo_id, chat_id))
                    await db.execute("DELETE FROM chat_members WHERE user_id = ? AND chat_id = ?", (pseudo_id, chat_id))

        await db.execute("""
            INSERT INTO chat_members (user_id, chat_id, username, full_name, is_admin, last_seen, photo_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username = CASE WHEN ? != '' THEN ? ELSE username END,
                full_name = CASE WHEN ? != '' THEN ? ELSE full_name END,
                is_admin = ?,
                last_seen = ?,
                photo_url = CASE WHEN ? != '' THEN ? ELSE photo_url END
        """, (
            user_id, chat_id, username, full_name, 1 if is_admin else 0, now, photo_url,
            username, username, full_name, full_name, 1 if is_admin else 0, now, photo_url, photo_url
        ))
        await db.commit()


async def get_all_members(chat_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Возвращает список всех известных участников с их статусом и предупреждениями."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT m.user_id, m.chat_id, m.username, m.full_name, m.is_admin, m.last_seen,
                   COALESCE(m.photo_url, '') as photo_url,
                   COALESCE(u.warnings, 0) as warnings
            FROM chat_members m
            LEFT JOIN users u ON m.user_id = u.user_id AND m.chat_id = u.chat_id
        """
        params = ()
        if chat_id:
            query += " WHERE m.chat_id = ?"
            params = (chat_id,)
        query += " ORDER BY m.is_admin DESC, warnings DESC, m.last_seen DESC LIMIT 100"

        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_member_by_username(username: str, chat_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Ищет участника по username в базе."""
    clean_username = username.strip().lstrip("@").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM chat_members WHERE LOWER(username) = ?"
        params = [clean_username]
        if chat_id:
            query += " AND chat_id = ?"
            params.append(chat_id)
        query += " LIMIT 1"
        async with db.execute(query, tuple(params)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def add_manual_member(username: str, full_name: str, chat_id: int) -> int:
    """Добавляет участника вручную по username (генерируя стабильный псевдо-ID, если нет реального)."""
    clean_username = username.strip().lstrip("@")
    import zlib
    # Генерируем детерминированный отрицательный ID для распознавания мануального профиля до первой реальной активности
    pseudo_id = -(zlib.crc32(clean_username.lower().encode()) & 0x7FFFFFFF)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, может уже есть
        async with db.execute("SELECT user_id FROM chat_members WHERE LOWER(username) = ? AND chat_id = ?", (clean_username.lower(), chat_id)) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]

        await db.execute("""
            INSERT INTO chat_members (user_id, chat_id, username, full_name, is_admin, last_seen)
            VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username = ?,
                full_name = ?
        """, (pseudo_id, chat_id, clean_username, full_name or f"@{clean_username}", now, clean_username, full_name or f"@{clean_username}"))
        await db.commit()
        return pseudo_id


async def set_botik_thread(chat_id: int, thread_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO chat_settings (chat_id, botik_thread_id) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET botik_thread_id = ?
        """, (chat_id, thread_id, thread_id))
        await db.commit()


async def get_botik_thread(chat_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT botik_thread_id FROM chat_settings WHERE chat_id = ?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None


async def set_ideas_thread(chat_id: int, thread_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO chat_settings (chat_id, ideas_thread_id) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET ideas_thread_id = ?
        """, (chat_id, thread_id, thread_id))
        await db.commit()


async def get_ideas_thread(chat_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT ideas_thread_id FROM chat_settings WHERE chat_id = ?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None


async def increment_stat(key: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO bot_stats (key, value) VALUES (?, 1)
            ON CONFLICT(key) DO UPDATE SET value = value + 1
        """, (key,))
        await db.commit()


async def get_warnings(user_id: int, chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT warnings FROM users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def add_warning(user_id: int, chat_id: int, username: str = "", full_name: str = "") -> int:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, chat_id, username, full_name, warnings, last_violation_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username = CASE WHEN ? != '' THEN ? ELSE username END,
                full_name = CASE WHEN ? != '' THEN ? ELSE full_name END,
                warnings = warnings + 1,
                last_violation_at = ?
            """,
            (user_id, chat_id, username, full_name, now, username, username, full_name, full_name, now),
        )
        await db.commit()

        async with db.execute(
            "SELECT warnings FROM users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1


async def reset_warnings(user_id: int, chat_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET warnings = 0 WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        await db.commit()


async def log_violation(
    user_id: int,
    chat_id: int,
    username: str,
    reason: str,
    message_text: str,
    action_taken: str
) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO violations (user_id, chat_id, username, reason, message_text, action_taken, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, chat_id, username, reason, message_text[:500], action_taken, now)
        )
        await db.commit()
    await increment_stat('total_violations')


async def get_user_status(user_id: int, chat_id: Optional[int] = None) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        user = None
        if chat_id:
            async with db.execute(
                "SELECT * FROM users WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id)
            ) as cur:
                user = await cur.fetchone()

        if not user:
            async with db.execute(
                "SELECT * FROM users WHERE user_id = ? ORDER BY last_violation_at DESC LIMIT 1",
                (user_id,)
            ) as cur:
                user = await cur.fetchone()

        # История нарушений
        query = "SELECT * FROM violations WHERE user_id = ? ORDER BY id DESC LIMIT 15"
        async with db.execute(query, (user_id,)) as cur:
            violations = [dict(r) for r in await cur.fetchall()]

        warnings = user["warnings"] if user else 0
        last_violation = user["last_violation_at"] if user else None

        # Проверка, админ ли пользователь
        is_admin = user_id in ADMIN_IDS
        if not is_admin:
            async with db.execute("SELECT is_admin FROM chat_members WHERE user_id = ? AND is_admin = 1", (user_id,)) as cur:
                is_admin = bool(await cur.fetchone())

        return {
            "user_id": user_id,
            "username": user["username"] if user else "",
            "full_name": user["full_name"] if user else "",
            "warnings": warnings,
            "max_warnings": 2,
            "is_restricted": warnings >= 2,
            "is_admin": is_admin,
            "last_violation_at": last_violation,
            "violations_history": violations,
        }


async def get_admin_stats(chat_id: Optional[int] = None) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Счетчики
        stats = {}
        async with db.execute("SELECT key, value FROM bot_stats") as cur:
            for row in await cur.fetchall():
                stats[row["key"]] = row["value"]

        # Нарушители
        query = "SELECT * FROM users WHERE warnings > 0 ORDER BY warnings DESC, last_violation_at DESC LIMIT 30"
        async with db.execute(query) as cur:
            violators = [dict(r) for r in await cur.fetchall()]

        # Список участников
        members = await get_all_members(chat_id)

        # Последние нарушения
        async with db.execute("SELECT * FROM violations ORDER BY id DESC LIMIT 20") as cur:
            recent_violations = [dict(r) for r in await cur.fetchall()]

        return {
            "stats": stats,
            "active_violators_count": len(violators),
            "members_count": len(members),
            "violators": violators,
            "members": members,
            "recent_violations": recent_violations,
        }
