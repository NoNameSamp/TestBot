"""
🤖 Telegram Admin Bot
Бот для управления группой: автопринятие заявок на вступление,
статистика вступлений/выходов, панель администратора, бэкап/восстановление
данных, экспорт статистики, графики.
"""

import io
import csv
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6788511742"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))
DATABASE_URL = os.environ["DATABASE_URL"]
DB_SSL = os.environ.get("DB_SSL", "require")

LOCAL_TZ = ZoneInfo("Europe/Berlin")

ADD_ADMIN_BTN = "➕ Добавить администратора"
REMOVE_ADMIN_BTN = "➖ Убрать администратора"
SETTINGS_BTN = "⚙️ Настройки"
AUTOJOIN_BTN = "🚦 Автопринятие"
FIND_USER_BTN = "🔎 Поиск пользователя"
LOGS_BTN = "🧾 Логи"
RESET_STATS_BTN = "♻️ Сброс статистики"
CHART_BTN = "📈 Графики"
EXPORT_BTN = "📊 Экспорт"
BACKUP_BTN = "💾 Резервная копия"
SUBSCRIBERS_BTN = "👥 Подписчики"
CLOSE_PANEL_BTN = "🔙 Закрыть панель"

DEFAULT_SETTINGS = {
    "auto_approve_join_requests": "1",
    "notify_admin_join_requests": "1",
    "notify_admin_joins": "1",
    "notify_admin_leaves": "1",
    "notify_admin_admin_changes": "1",
    "notify_user_admin_changes": "1",
}

SETTING_TITLES = {
    "auto_approve_join_requests": "Автопринятие заявок",
    "notify_admin_join_requests": "Уведомления о заявках",
    "notify_admin_joins": "Уведомления о вступлениях",
    "notify_admin_leaves": "Уведомления о выходах",
    "notify_admin_admin_changes": "Уведомления о смене админки",
    "notify_user_admin_changes": "Уведомления пользователю о смене админки",
}

_admin_input_mode: dict[int, str] = {}


def _is_admin(user_id: int, admin_ids: set[int] | None = None) -> bool:
    if admin_ids is None:
        admin_ids = {ADMIN_ID}
    return user_id == ADMIN_ID or user_id in admin_ids


class _AwaitingAdminInputFilter(filters.MessageFilter):
    def filter(self, message):
        user = message.from_user
        return bool(user and user.id in _admin_input_mode)


def _admin_panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(ADD_ADMIN_BTN), KeyboardButton(REMOVE_ADMIN_BTN)],
            [KeyboardButton(SETTINGS_BTN), KeyboardButton(AUTOJOIN_BTN)],
            [KeyboardButton(FIND_USER_BTN), KeyboardButton(LOGS_BTN)],
            [KeyboardButton(RESET_STATS_BTN), KeyboardButton(CHART_BTN)],
            [KeyboardButton(EXPORT_BTN), KeyboardButton(BACKUP_BTN)],
            [KeyboardButton(SUBSCRIBERS_BTN), KeyboardButton(CLOSE_PANEL_BTN)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _safe_username(username: str | None) -> str:
    return f"@{username}" if username else "(без username)"


def _fmt_dt(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _yes_no_icon(value: bool) -> str:
    return "✅" if value else "❌"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def _init_db(application: Application):
    ssl_arg = False if DB_SSL == "disable" else DB_SSL
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, ssl=ssl_arg)
    application.bot_data["db_pool"] = pool

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT,
                event_type TEXT NOT NULL,
                ts TIMESTAMPTZ NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                added_by BIGINT NOT NULL,
                added_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                actor_id BIGINT NOT NULL,
                action TEXT NOT NULL,
                target_id BIGINT,
                details TEXT,
                ts TIMESTAMPTZ NOT NULL
            )
            """
        )

        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
                key,
                value,
            )

        await conn.execute(
            "INSERT INTO admins (user_id, username, added_by, added_at) "
            "VALUES ($1, NULL, $1, $2) ON CONFLICT (user_id) DO NOTHING",
            ADMIN_ID,
            datetime.now(timezone.utc),
        )
        rows = await conn.fetch("SELECT user_id FROM admins")

    application.bot_data["admin_ids"] = {r["user_id"] for r in rows} | {ADMIN_ID}
    logger.info("Подключение к PostgreSQL установлено, таблицы готовы")


async def _close_db(application: Application):
    pool = application.bot_data.get("db_pool")
    if pool:
        await pool.close()
        logger.info("Пул подключений к PostgreSQL закрыт")


async def _get_settings_map(pool: asyncpg.Pool) -> dict[str, bool]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM settings")
    current = {r["key"]: r["value"] for r in rows}
    return {key: current.get(key, default) == "1" for key, default in DEFAULT_SETTINGS.items()}


async def _get_setting(pool: asyncpg.Pool, key: str) -> bool:
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)
    if val is None:
        return DEFAULT_SETTINGS.get(key, "0") == "1"
    return val == "1"


async def _set_setting(pool: asyncpg.Pool, key: str, enabled: bool):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key,
            "1" if enabled else "0",
        )


async def _log_event(pool: asyncpg.Pool, chat_id: int, user_id: int, username: str, full_name: str, event_type: str):
    async with pool.acquire() as conn:
        if event_type == "join":
            ex = await conn.fetchval(
                "SELECT 1 FROM events WHERE chat_id = $1 AND user_id = $2 AND event_type = 'join' LIMIT 1",
                chat_id,
                user_id,
            )
            if ex:
                return

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO events (chat_id, user_id, username, full_name, event_type, ts) VALUES ($1, $2, $3, $4, $5, $6)",
            chat_id,
            user_id,
            username,
            full_name,
            event_type,
            datetime.now(timezone.utc),
        )


async def _reset_stats(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM events")


# ---------------------------------------------------------------------------
# Графики статистики (v2.5)
# ---------------------------------------------------------------------------

CHART_PERIOD_LABELS = {"day": "за 24 часа", "week": "за неделю", "month": "за месяц"}


async def _get_stats_series(pool: asyncpg.Pool, period: str):
    now = datetime.now(timezone.utc)
    if period == "day":
        since = now - timedelta(hours=24)
        trunc, fmt = "hour", "%H:%M"
    elif period == "week":
        since = now - timedelta(days=7)
        trunc, fmt = "day", "%d.%m"
    else:
        since = now - timedelta(days=30)
        trunc, fmt = "day", "%d.%m"

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT date_trunc('{trunc}', ts) AS bucket, event_type, COUNT(*) AS cnt
            FROM events
            WHERE ts >= $1
            GROUP BY bucket, event_type
            ORDER BY bucket
            """,
            since,
        )

    data: dict[datetime, dict[str, int]] = {}
    for r in rows:
        bucket = data.setdefault(r["bucket"], {"join": 0, "leave": 0})
        bucket[r["event_type"]] = r["cnt"]

    buckets = sorted(data.keys())
    labels = [b.astimezone(LOCAL_TZ).strftime(fmt) for b in buckets]
    joins = [data[b].get("join", 0) for b in buckets]
    leaves = [data[b].get("leave", 0) for b in buckets]
    return labels, joins, leaves


def _render_stats_chart(labels: list[str], joins: list[int], leaves: list[int], period_label: str) -> bytes:
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    x = range(len(labels))
    ax.plot(list(x), joins, marker="o", label="Вступления", color="#2ecc71")
    ax.plot(list(x), leaves, marker="o", label="Выходы", color="#e74c3c")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_title(f"Статистика {period_label}")
    ax.set_ylabel("Количество")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _chart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("День", callback_data="chart_day"),
            InlineKeyboardButton("Неделя", callback_data="chart_week"),
            InlineKeyboardButton("Месяц", callback_data="chart_month"),
        ]]
    )


async def _send_stats_chart(context: ContextTypes.DEFAULT_TYPE, pool: asyncpg.Pool, period: str, chat_id: int):
    labels, joins, leaves = await _get_stats_series(pool, period)
    period_label = CHART_PERIOD_LABELS.get(period, period)
    if not labels:
        await context.bot.send_message(chat_id=chat_id, text=f"📈 Нет данных {period_label}.")
        return

    image_bytes = _render_stats_chart(labels, joins, leaves, period_label)
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(io.BytesIO(image_bytes), filename=f"stats_{period}.png"),
        caption=f"📈 График статистики {period_label}",
        reply_markup=_chart_keyboard(),
    )


# ---------------------------------------------------------------------------
# Экспорт Excel/CSV (v2.5)
# ---------------------------------------------------------------------------


async def _fetch_all_events(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, chat_id, user_id, username, full_name, event_type, ts FROM events ORDER BY ts"
        )


async def _fetch_all_logs(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, actor_id, action, target_id, details, ts FROM admin_logs ORDER BY ts"
        )


async def _get_export_dataset(pool: asyncpg.Pool, dataset: str):
    if dataset == "events":
        rows = await _fetch_all_events(pool)
        headers = ["id", "chat_id", "user_id", "username", "full_name", "event_type", "ts"]
        data = [
            [r["id"], r["chat_id"], r["user_id"], r["username"] or "", r["full_name"] or "", r["event_type"], _fmt_dt(r["ts"])]
            for r in rows
        ]
        return headers, data, "события"

    if dataset == "logs":
        rows = await _fetch_all_logs(pool)
        headers = ["id", "actor_id", "action", "target_id", "details", "ts"]
        data = [
            [r["id"], r["actor_id"], r["action"], r["target_id"], r["details"] or "", _fmt_dt(r["ts"])]
            for r in rows
        ]
        return headers, data, "логи"

    if dataset == "admins":
        rows = await _list_admins(pool)
        headers = ["user_id", "username", "added_by", "added_at"]
        data = [
            [r["user_id"], r["username"] or "", r["added_by"], _fmt_dt(r["added_at"])]
            for r in rows
        ]
        return headers, data, "администраторы"

    return [], [], dataset


def _rows_to_csv_bytes(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    # utf-8-sig, чтобы Excel корректно показывал кириллицу
    return buf.getvalue().encode("utf-8-sig")


def _rows_to_xlsx_bytes(headers: list[str], rows: list[list], title: str = "Data") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = (title or "Data")[:31]
    ws.append(headers)
    for row in rows:
        ws.append(row)
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(width + 2, 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def _export_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("События CSV", callback_data="export:events:csv"),
                InlineKeyboardButton("События Excel", callback_data="export:events:xlsx"),
            ],
            [
                InlineKeyboardButton("Логи CSV", callback_data="export:logs:csv"),
                InlineKeyboardButton("Логи Excel", callback_data="export:logs:xlsx"),
            ],
            [
                InlineKeyboardButton("Админы CSV", callback_data="export:admins:csv"),
                InlineKeyboardButton("Админы Excel", callback_data="export:admins:xlsx"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Резервные копии и восстановление (v2.5)
# ---------------------------------------------------------------------------

_pending_restore: dict[int, dict] = {}


async def _backup_data(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        events = await conn.fetch(
            "SELECT chat_id, user_id, username, full_name, event_type, ts FROM events ORDER BY ts"
        )
        admins = await conn.fetch("SELECT user_id, username, added_by, added_at FROM admins ORDER BY added_at")
        settings = await conn.fetch("SELECT key, value FROM settings")
        logs = await conn.fetch(
            "SELECT actor_id, action, target_id, details, ts FROM admin_logs ORDER BY ts"
        )

    def iso(v):
        return v.isoformat() if v else None

    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "events": [
            {
                "chat_id": r["chat_id"],
                "user_id": r["user_id"],
                "username": r["username"],
                "full_name": r["full_name"],
                "event_type": r["event_type"],
                "ts": iso(r["ts"]),
            }
            for r in events
        ],
        "admins": [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "added_by": r["added_by"],
                "added_at": iso(r["added_at"]),
            }
            for r in admins
        ],
        "settings": {r["key"]: r["value"] for r in settings},
        "admin_logs": [
            {
                "actor_id": r["actor_id"],
                "action": r["action"],
                "target_id": r["target_id"],
                "details": r["details"],
                "ts": iso(r["ts"]),
            }
            for r in logs
        ],
    }


async def _restore_data(pool: asyncpg.Pool, data: dict):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM events")
            await conn.execute("DELETE FROM admins")
            await conn.execute("DELETE FROM settings")
            await conn.execute("DELETE FROM admin_logs")

            for e in data.get("events", []):
                await conn.execute(
                    "INSERT INTO events (chat_id, user_id, username, full_name, event_type, ts) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    e["chat_id"],
                    e["user_id"],
                    e.get("username"),
                    e.get("full_name"),
                    e["event_type"],
                    datetime.fromisoformat(e["ts"]) if e.get("ts") else datetime.now(timezone.utc),
                )

            for a in data.get("admins", []):
                await conn.execute(
                    "INSERT INTO admins (user_id, username, added_by, added_at) VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    a["user_id"],
                    a.get("username"),
                    a["added_by"],
                    datetime.fromisoformat(a["added_at"]) if a.get("added_at") else datetime.now(timezone.utc),
                )

            for key, value in data.get("settings", {}).items():
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    key,
                    value,
                )

            for entry in data.get("admin_logs", []):
                await conn.execute(
                    "INSERT INTO admin_logs (actor_id, action, target_id, details, ts) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    entry["actor_id"],
                    entry["action"],
                    entry.get("target_id"),
                    entry.get("details"),
                    datetime.fromisoformat(entry["ts"]) if entry.get("ts") else datetime.now(timezone.utc),
                )

            # Владелец бота должен остаться администратором в любом случае
            await conn.execute(
                "INSERT INTO admins (user_id, username, added_by, added_at) "
                "VALUES ($1, NULL, $1, $2) ON CONFLICT (user_id) DO NOTHING",
                ADMIN_ID,
                datetime.now(timezone.utc),
            )


class _AwaitingRestoreFileFilter(filters.MessageFilter):
    def filter(self, message):
        user = message.from_user
        return bool(user and _admin_input_mode.get(user.id) == "restore" and message.document)


# ---------------------------------------------------------------------------
# Подсчёт подписчиков (v2.5)
# ---------------------------------------------------------------------------


async def _get_known_chat_ids(pool: asyncpg.Pool) -> list[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT chat_id FROM events ORDER BY chat_id")
    return [r["chat_id"] for r in rows]


# ---------------------------------------------------------------------------
# Notifications and logs
# ---------------------------------------------------------------------------


async def _log_admin_action(
    pool: asyncpg.Pool,
    actor_id: int,
    action: str,
    target_id: int | None = None,
    details: str | None = None,
):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO admin_logs (actor_id, action, target_id, details, ts) VALUES ($1, $2, $3, $4, $5)",
            actor_id,
            action,
            target_id,
            details,
            datetime.now(timezone.utc),
        )


async def _refresh_admin_cache(context: ContextTypes.DEFAULT_TYPE):
    pool = context.bot_data["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM admins")
    context.bot_data["admin_ids"] = {r["user_id"] for r in rows} | {ADMIN_ID}


async def _add_admin(pool: asyncpg.Pool, user_id: int, username: str | None, added_by: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO admins (user_id, username, added_by, added_at) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username",
            user_id,
            username,
            added_by,
            datetime.now(timezone.utc),
        )


async def _remove_admin(pool: asyncpg.Pool, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id = $1", user_id)


async def _list_admins(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, username, added_by, added_at FROM admins ORDER BY added_at")


async def _admins_card_text(pool: asyncpg.Pool) -> str:
    rows = await _list_admins(pool)
    if not rows:
        return "👑 Администраторов пока нет."
    lines = ["👑 Текущие администраторы:\n"]
    for r in rows:
        uname = _safe_username(r["username"])
        mark = " — владелец" if r["user_id"] == ADMIN_ID else ""
        lines.append(f"• ID: {r['user_id']} {uname}{mark}")
    return "\n".join(lines)


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, exclude: set[int] | None = None):
    exclude = exclude or set()
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    for admin_id in admin_ids:
        if admin_id in exclude:
            continue
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception as exc:
            logger.info("Не удалось отправить уведомление админу %s: %s", admin_id, exc)


async def _notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception as exc:
        logger.info("Не удалось отправить уведомление пользователю %s: %s", user_id, exc)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


async def _get_stats(pool: asyncpg.Pool, period: str):
    now = datetime.now(timezone.utc)
    if period == "day":
        since = now - timedelta(days=1)
        label = "за сегодня (24ч)"
    elif period == "week":
        since = now - timedelta(days=7)
        label = "за неделю"
    else:
        since = now - timedelta(days=30)
        label = "за месяц"

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, COUNT(*) AS cnt FROM events WHERE ts >= $1 GROUP BY event_type",
            since,
        )
    counts = {r["event_type"]: r["cnt"] for r in rows}
    joins = counts.get("join", 0)
    leaves = counts.get("leave", 0)
    return label, joins, leaves


async def _stats_text(pool: asyncpg.Pool, period: str) -> str:
    label, joins, leaves = await _get_stats(pool, period)
    return (
        f"📊 Статистика {label}\n\n"
        f"➕ Вступлений: {joins}\n"
        f"➖ Выходов: {leaves}\n"
        f"Δ Итог: {joins - leaves:+d}"
    )


def _stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("День", callback_data="stats_day"),
            InlineKeyboardButton("Неделя", callback_data="stats_week"),
            InlineKeyboardButton("Месяц", callback_data="stats_month"),
        ]]
    )


# ---------------------------------------------------------------------------
# Settings UI
# ---------------------------------------------------------------------------


def _settings_text(settings: dict[str, bool]) -> str:
    lines = ["⚙️ Настройки бота\n"]
    for key, title in SETTING_TITLES.items():
        lines.append(f"{_yes_no_icon(settings.get(key, False))} {title}")
    lines.append("")
    lines.append("Используйте кнопки ниже, чтобы включать и выключать нужные режимы.")
    return "\n".join(lines)


def _settings_keyboard(settings: dict[str, bool]) -> InlineKeyboardMarkup:
    def btn(key: str) -> InlineKeyboardButton:
        state = "ON" if settings.get(key, False) else "OFF"
        return InlineKeyboardButton(
            f"{SETTING_TITLES[key]}: {state}",
            callback_data=f"set:toggle:{key}",
        )

    return InlineKeyboardMarkup(
        [
            [btn("auto_approve_join_requests")],
            [btn("notify_admin_join_requests")],
            [btn("notify_admin_joins"), btn("notify_admin_leaves")],
            [btn("notify_admin_admin_changes")],
            [btn("notify_user_admin_changes")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="set:back")],
        ]
    )


async def _render_settings_message(pool: asyncpg.Pool) -> tuple[str, InlineKeyboardMarkup]:
    settings = await _get_settings_map(pool)
    return _settings_text(settings), _settings_keyboard(settings)


# ---------------------------------------------------------------------------
# User search
# ---------------------------------------------------------------------------


async def _get_user_summary(pool: asyncpg.Pool, user_id: int):
    async with pool.acquire() as conn:
        admin_row = await conn.fetchrow(
            "SELECT user_id, username, added_by, added_at FROM admins WHERE user_id = $1",
            user_id,
        )
        event_row = await conn.fetchrow(
            """
            SELECT
                MIN(ts) AS first_seen,
                MAX(ts) AS last_seen,
                COUNT(*) FILTER (WHERE event_type = 'join') AS joins,
                COUNT(*) FILTER (WHERE event_type = 'leave') AS leaves,
                MAX(username) AS username,
                MAX(full_name) AS full_name,
                COUNT(*) AS total_events
            FROM events
            WHERE user_id = $1
            """,
            user_id,
        )

    if not admin_row and not event_row:
        return None

    joins = int(event_row["joins"] or 0) if event_row else 0
    leaves = int(event_row["leaves"] or 0) if event_row else 0
    total_events = int(event_row["total_events"] or 0) if event_row else 0
    username = None
    full_name = None
    first_seen = None
    last_seen = None
    if event_row:
        username = event_row["username"]
        full_name = event_row["full_name"]
        first_seen = event_row["first_seen"]
        last_seen = event_row["last_seen"]

    if admin_row and not username:
        username = admin_row["username"]

    return {
        "user_id": user_id,
        "username": username,
        "full_name": full_name,
        "is_admin": bool(admin_row),
        "added_by": admin_row["added_by"] if admin_row else None,
        "added_at": admin_row["added_at"] if admin_row else None,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "joins": joins,
        "leaves": leaves,
        "total_events": total_events,
    }


async def _search_user_ids(pool: asyncpg.Pool, query: str) -> list[int]:
    raw = query.strip()
    if not raw:
        return []

    if raw.lstrip("-").isdigit():
        return [int(raw)]

    q = raw.lstrip("@").lower()
    pattern = f"%{q}%"

    async with pool.acquire() as conn:
        admin_rows = await conn.fetch(
            """
            SELECT DISTINCT user_id
            FROM admins
            WHERE username IS NOT NULL AND (lower(username) = $1 OR lower(username) LIKE $2)
            ORDER BY user_id
            """,
            q,
            pattern,
        )
        event_rows = await conn.fetch(
            """
            SELECT DISTINCT user_id
            FROM events
            WHERE (username IS NOT NULL AND (lower(username) = $1 OR lower(username) LIKE $2))
               OR (full_name IS NOT NULL AND lower(full_name) LIKE $2)
            ORDER BY user_id
            """,
            q,
            pattern,
        )

    ids: list[int] = []
    seen: set[int] = set()
    for row in list(admin_rows) + list(event_rows):
        uid = row["user_id"]
        if uid not in seen:
            seen.add(uid)
            ids.append(uid)
    return ids[:10]


async def _format_user_search_result(pool: asyncpg.Pool, user_id: int) -> str | None:
    info = await _get_user_summary(pool, user_id)
    if not info:
        return None

    role = "👑 Администратор" if info["is_admin"] else "👤 Пользователь"
    lines = [
        f"{role}",
        f"ID: {info['user_id']}",
        f"Username: {_safe_username(info['username'])}",
        f"Имя: {info['full_name'] or '—'}",
        f"Вступлений: {info['joins']}",
        f"Выходов: {info['leaves']}",
        f"Событий всего: {info['total_events']}",
        f"Первое событие: {_fmt_dt(info['first_seen'])}",
        f"Последнее событие: {_fmt_dt(info['last_seen'])}",
    ]
    if info["is_admin"]:
        lines.append(f"Назначен: {_fmt_dt(info['added_at'])}")
        lines.append(f"Назначил: {info['added_by']}")
    return "\n".join(lines)


async def _format_user_search_results(pool: asyncpg.Pool, query: str) -> str:
    ids = await _search_user_ids(pool, query)
    if not ids:
        return "🔎 Ничего не найдено."

    if len(ids) == 1:
        result = await _format_user_search_result(pool, ids[0])
        return result or "🔎 Ничего не найдено."

    lines = [f"🔎 Найдено несколько пользователей: {len(ids)}\n"]
    for uid in ids:
        info = await _get_user_summary(pool, uid)
        if not info:
            continue
        role = "админ" if info["is_admin"] else "пользователь"
        uname = _safe_username(info["username"])
        display = info["full_name"] or "—"
        lines.append(f"• {uid} | {uname} | {display} | {role}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Admin logs
# ---------------------------------------------------------------------------


ACTION_TITLES = {
    "add_admin": "назначил администратором",
    "remove_admin": "снял с админки",
    "reset_stats": "сбросил статистику",
    "toggle_setting": "изменил настройку",
    "toggle_autojoin": "изменил автопринятие",
    "export": "выполнил экспорт данных",
    "backup": "создал резервную копию",
    "restore": "восстановил данные из копии",
}


async def _fetch_recent_admin_logs(pool: asyncpg.Pool, limit: int = 10):
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_id, action, target_id, details, ts FROM admin_logs ORDER BY ts DESC LIMIT $1",
            limit,
        )


def _format_admin_log_row(row) -> str:
    action = ACTION_TITLES.get(row["action"], row["action"])
    parts = [
        f"{_fmt_dt(row['ts'])}",
        f"actor={row['actor_id']}",
        action,
    ]
    if row["target_id"] is not None:
        parts.append(f"target={row['target_id']}")
    if row["details"]:
        parts.append(str(row["details"]))
    return " • ".join(parts)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})

    if _is_admin(user.id, admin_ids):
        text = (
            "Бот запущен.\n\n"
            "Команды для администратора:\n"
            "/stats — статистика вступлений/выходов\n"
            "/reset_stats — сброс статистики с подтверждением\n"
            "/settings — настройки уведомлений\n"
            "/autojoin — включить/выключить автопринятие заявок\n"
            "/finduser — поиск пользователя по ID/username\n"
            "/logs — логи действий администраторов\n"
            "/chart — графики статистики\n"
            "/export — экспорт в CSV/Excel\n"
            "/backup — резервная копия данных\n"
            "/restore — восстановление из копии\n"
            "/subscribers — число подписчиков\n"
            "/admin — панель управления"
        )
    else:
        text = "Бот запущен."

    await update.message.reply_text(text)


async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if result.new_chat_member.status in ("member", "administrator"):
        chat_id = result.chat.id
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔕",
                reply_markup=ReplyKeyboardRemove(),
            )
            logger.info("Клавиатура сброшена в чате %s при добавлении бота", chat_id)
        except Exception as exc:
            logger.warning("Не удалось сбросить клавиатуру в чате %s: %s", chat_id, exc)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    chat_id = request.chat.id
    user = request.from_user
    pool = context.bot_data["db_pool"]

    auto_approve = await _get_setting(pool, "auto_approve_join_requests")
    username = _safe_username(user.username)
    full_name = user.full_name or "Без имени"
    chat_title = request.chat.title or str(chat_id)

    if auto_approve:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user.id)
            logger.info("Заявка на вступление одобрена: user_id=%s chat_id=%s", user.id, chat_id)
            await _log_event(pool, chat_id, user.id, user.username or "", full_name, "join")

            if await _get_setting(pool, "notify_admin_join_requests") or await _get_setting(pool, "notify_admin_joins"):
                text = (
                    "✅ Заявка одобрена\n\n"
                    f"Чат: {chat_title}\n"
                    f"Пользователь: {full_name} {username}\n"
                    f"ID: {user.id}"
                )
                await _notify_admins(context, text)

        except Exception as exc:
            logger.warning("Не удалось одобрить заявку user_id=%s chat_id=%s: %s", user.id, chat_id, exc)
            await _notify_admins(
                context,
                "⚠️ Не удалось одобрить заявку\n\n"
                f"user_id: {user.id}\n"
                f"chat_id: {chat_id}\n"
                f"Ошибка: {exc}",
            )
    else:
        if await _get_setting(pool, "notify_admin_join_requests"):
            await _notify_admins(
                context,
                "⏳ Новая заявка на вступление\n\n"
                f"Чат: {chat_title}\n"
                f"Пользователь: {full_name} {username}\n"
                f"ID: {user.id}\n\n"
                "Автопринятие сейчас выключено.",
            )


async def on_member_status_changed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    user = result.new_chat_member.user
    chat_id = result.chat.id

    was_in = old_status in ("member", "administrator", "creator", "restricted")
    is_out = new_status in ("left", "kicked")

    if was_in and is_out:
        pool = context.bot_data["db_pool"]
        full_name = user.full_name or "Без имени"
        await _log_event(pool, chat_id, user.id, user.username or "", full_name, "leave")

        if await _get_setting(pool, "notify_admin_leaves"):
            username = _safe_username(user.username)
            await _notify_admins(
                context,
                "❌ Пользователь покинул канал/чат\n\n"
                f"Пользователь: {full_name}\n"
                f"Юзернейм: {username}\n"
                f"ID: {user.id}",
            )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    pool = context.bot_data["db_pool"]
    text = await _stats_text(pool, "day")
    await update.message.reply_text(text, reply_markup=_stats_keyboard())


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        await query.answer()
        return

    period_map = {
        "stats_day": "day",
        "stats_week": "week",
        "stats_month": "month",
    }
    period = period_map.get(query.data, "day")
    pool = context.bot_data["db_pool"]
    text = await _stats_text(pool, period)
    await query.edit_message_text(text, reply_markup=_stats_keyboard())
    await query.answer()


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    _admin_input_mode.pop(user.id, None)
    await update.message.reply_text(
        "🛠 Панель администратора\n\nВыберите действие кнопками ниже.",
        reply_markup=_admin_panel_keyboard(),
    )


async def on_add_admin_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    pool = context.bot_data["db_pool"]
    card = await _admins_card_text(pool)
    _admin_input_mode[user.id] = "add"
    await update.message.reply_text(
        f"{card}\n\nОтправьте ID пользователя, которого нужно назначить администратором.\n"
        f"Отмена — нажмите /admin ещё раз или кнопку закрытия панели.")


async def on_remove_admin_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    pool = context.bot_data["db_pool"]
    rows = await _list_admins(pool)
    removable = [r for r in rows if r["user_id"] != ADMIN_ID]
    if not removable:
        await update.message.reply_text("Некого убирать (кроме владельца).", reply_markup=_admin_panel_keyboard())
        return

    buttons = []
    for r in removable:
        label = _safe_username(r["username"])
        buttons.append([InlineKeyboardButton(f"❌ {label} ({r['user_id']})", callback_data=f"deladmin_{r['user_id']}")])

    await update.message.reply_text(
        f"{await _admins_card_text(pool)}\n\nВыберите, кого убрать из администраторов:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_settings_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    pool = context.bot_data["db_pool"]
    text, keyboard = await _render_settings_message(pool)
    await update.message.reply_text(text, reply_markup=keyboard)


async def on_autojoin_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return

    pool = context.bot_data["db_pool"]
    current = await _get_setting(pool, "auto_approve_join_requests")
    new_value = not current
    await _set_setting(pool, "auto_approve_join_requests", new_value)
    await _log_admin_action(
        pool,
        user.id,
        "toggle_autojoin",
        details=f"auto_approve_join_requests={int(new_value)}",
    )

    status = "включено" if new_value else "выключено"
    await update.message.reply_text(f"🚦 Автопринятие заявок: {status}")


async def on_find_user_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    _admin_input_mode[user.id] = "find"
    await update.message.reply_text(
        "Отправьте ID пользователя или username (например: 123456789 или @nickname)."
    )


async def on_logs_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return

    pool = context.bot_data["db_pool"]
    rows = await _fetch_recent_admin_logs(pool, 10)
    if not rows:
        await update.message.reply_text("Логов пока нет.")
        return

    lines = ["🧾 Последние действия администраторов:\n"]
    for row in rows:
        lines.append("• " + _format_admin_log_row(row))
    await update.message.reply_text("\n".join(lines))


async def on_reset_stats_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Да, сбросить", callback_data="resetstats_yes"),
            InlineKeyboardButton("❌ Нет", callback_data="resetstats_no"),
        ]]
    )
    await update.message.reply_text(
        "⚠️ Вы уверены, что хотите полностью сбросить статистику?\n\n"
        "Это удалит все записи о вступлениях и выходах.",
        reply_markup=keyboard,
    )


async def on_chart_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    pool = context.bot_data["db_pool"]
    await _send_stats_chart(context, pool, "week", update.effective_chat.id)


async def on_export_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    await update.message.reply_text(
        "📊 Выберите, что и в каком формате экспортировать:",
        reply_markup=_export_keyboard(),
    )


async def on_backup_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return

    pool = context.bot_data["db_pool"]
    data = await _backup_data(pool)
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"backup_{datetime.now(LOCAL_TZ).strftime('%Y%m%d_%H%M%S')}.json"

    await update.message.reply_document(
        document=InputFile(io.BytesIO(content), filename=filename),
        caption=(
            "💾 Резервная копия создана.\n\n"
            f"Событий: {len(data['events'])}\n"
            f"Администраторов: {len(data['admins'])}\n"
            f"Записей лога: {len(data['admin_logs'])}\n\n"
            "Для восстановления используйте /restore и отправьте этот файл."
        ),
    )
    await _log_admin_action(pool, user.id, "backup", details=f"events={len(data['events'])}")


async def on_subscribers_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return

    pool = context.bot_data["db_pool"]
    chat_ids = await _get_known_chat_ids(pool)
    if not chat_ids:
        await update.message.reply_text("👥 Пока нет данных ни по одному чату/каналу.")
        return

    lines = ["👥 Подписчики:\n"]
    total = 0
    ok_count = 0
    for chat_id in chat_ids:
        try:
            chat = await context.bot.get_chat(chat_id)
            count = await context.bot.get_chat_member_count(chat_id)
            title = chat.title or chat.first_name or str(chat_id)
            lines.append(f"• {title}: {count}")
            total += count
            ok_count += 1
        except Exception as exc:
            logger.info("Не удалось получить число подписчиков chat_id=%s: %s", chat_id, exc)
            lines.append(f"• {chat_id}: недоступно ({exc.__class__.__name__})")

    if ok_count > 1:
        lines.append(f"\nΣ Всего подписчиков: {total}")

    await update.message.reply_text("\n".join(lines))


async def on_restore_file_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return

    document = update.message.document
    file = await document.get_file()
    raw = await file.download_as_bytearray()

    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Не удалось прочитать файл резервной копии: {exc}")
        return

    if not isinstance(data, dict) or "events" not in data or "admins" not in data:
        await update.message.reply_text("⚠️ Файл не похож на резервную копию этого бота.")
        return

    _admin_input_mode.pop(user.id, None)
    _pending_restore[user.id] = data

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Да, восстановить", callback_data="restore_yes"),
            InlineKeyboardButton("❌ Отмена", callback_data="restore_no"),
        ]]
    )
    await update.message.reply_text(
        "⚠️ Подтвердите восстановление из резервной копии.\n\n"
        f"Событий: {len(data.get('events', []))}\n"
        f"Администраторов: {len(data.get('admins', []))}\n"
        f"Создана: {data.get('created_at', '—')}\n\n"
        "Это ПОЛНОСТЬЮ заменит текущие данные бота. Продолжить?",
        reply_markup=keyboard,
    )


async def on_close_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    _admin_input_mode.pop(user.id, None)
    await update.message.reply_text("Панель закрыта.", reply_markup=ReplyKeyboardRemove())


async def on_awaiting_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    mode = _admin_input_mode.get(user.id)
    if not mode:
        return

    pool = context.bot_data["db_pool"]
    text = (update.message.text or "").strip()

    if mode == "add":
        if not text.lstrip("-").isdigit():
            await update.message.reply_text(
                "ID должен быть числом. Пришлите числовой ID пользователя или нажмите /admin для отмены."
            )
            return

        target_id = int(text)
        username = None
        try:
            chat = await context.bot.get_chat(target_id)
            username = chat.username
        except Exception as exc:
            logger.info("Не удалось получить username для %s: %s", target_id, exc)

        await _add_admin(pool, target_id, username, added_by=user.id)
        await _refresh_admin_cache(context)
        _admin_input_mode.pop(user.id, None)

        await _log_admin_action(
            pool,
            user.id,
            "add_admin",
            target_id=target_id,
            details=f"username={username or ''}",
        )

        if await _get_setting(pool, "notify_admin_admin_changes"):
            await _notify_admins(
                context,
                f"👑 Назначен новый администратор\n\nID: {target_id}\nUsername: {_safe_username(username)}\nНазначил: {user.id}",
            )

        if await _get_setting(pool, "notify_user_admin_changes"):
            await _notify_user(
                context,
                target_id,
                "Вас назначили администратором.",
            )

        card = await _admins_card_text(pool)
        await update.message.reply_text(f"✅ Пользователь {target_id} назначен администратором.\n\n{card}")
        return

    if mode == "find":
        _admin_input_mode.pop(user.id, None)
        result = await _format_user_search_results(pool, text)
        await update.message.reply_text(result)
        return


async def on_admin_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_awaiting_admin_text(update, context)


async def add_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        await query.answer()
        return

    target_id = int(query.data.split("_", 1)[1])
    if target_id == ADMIN_ID:
        await query.answer("Владельца нельзя убрать.", show_alert=True)
        return

    pool = context.bot_data["db_pool"]
    async with pool.acquire() as conn:
        target_row = await conn.fetchrow("SELECT username FROM admins WHERE user_id = $1", target_id)

    await _remove_admin(pool, target_id)
    await _refresh_admin_cache(context)

    await _log_admin_action(
        pool,
        user.id,
        "remove_admin",
        target_id=target_id,
        details=f"username={target_row['username'] if target_row else ''}",
    )

    if await _get_setting(pool, "notify_admin_admin_changes"):
        await _notify_admins(
            context,
            f"👑 Администратор снят\n\nID: {target_id}\nКем снят: {user.id}",
        )

    if await _get_setting(pool, "notify_user_admin_changes"):
        await _notify_user(
            context,
            target_id,
            "Вас сняли с админки.",
        )

    rows = await _list_admins(pool)
    remaining = [r for r in rows if r["user_id"] != ADMIN_ID]
    if remaining:
        buttons = []
        for r in remaining:
            label = _safe_username(r["username"])
            buttons.append([InlineKeyboardButton(f"❌ {label} ({r['user_id']})", callback_data=f"deladmin_{r['user_id']}")])
        await query.edit_message_text(
            f"{await _admins_card_text(pool)}\n\n✅ Администратор {target_id} удалён.\n\nВыберите, кого ещё убрать:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await query.edit_message_text(f"{await _admins_card_text(pool)}\n\n✅ Администратор {target_id} удалён.")
    await query.answer()


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        await query.answer()
        return

    pool = context.bot_data["db_pool"]
    data = query.data.split(":", 2)
    action = data[1] if len(data) > 1 else ""

    if action == "back":
        await query.edit_message_text("🛠 Панель администратора")
        await query.answer()
        return

    if action == "toggle" and len(data) == 3:
        key = data[2]
        current = await _get_setting(pool, key)
        new_value = not current
        await _set_setting(pool, key, new_value)
        await _log_admin_action(
            pool,
            user.id,
            "toggle_setting",
            details=f"{key}={int(new_value)}",
        )

        text, keyboard = await _render_settings_message(pool)
        await query.edit_message_text(text, reply_markup=keyboard)
        await query.answer("Настройка обновлена")
        return

    await query.answer()


async def reset_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        await query.answer()
        return

    pool = context.bot_data["db_pool"]
    if query.data == "resetstats_no":
        await query.edit_message_text("Сброс статистики отменён.")
        await query.answer()
        return

    if query.data == "resetstats_yes":
        await _reset_stats(pool)
        await _log_admin_action(pool, user.id, "reset_stats", details="all events deleted")
        await query.edit_message_text("✅ Статистика полностью сброшена.")
        await query.answer()
        return

    await query.answer()


async def chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        await query.answer()
        return

    period_map = {"chart_day": "day", "chart_week": "week", "chart_month": "month"}
    period = period_map.get(query.data, "week")
    pool = context.bot_data["db_pool"]
    await query.answer("Строю график…")
    await _send_stats_chart(context, pool, period, query.message.chat.id)


async def export_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        await query.answer()
        return

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer()
        return
    _, dataset, fmt = parts

    pool = context.bot_data["db_pool"]
    headers, data, title = await _get_export_dataset(pool, dataset)

    if not data:
        await query.answer()
        await context.bot.send_message(chat_id=query.message.chat.id, text=f"Нет данных для экспорта: {title}.")
        return

    await query.answer("Готовлю файл…")

    ts_suffix = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
    if fmt == "csv":
        content = _rows_to_csv_bytes(headers, data)
        filename = f"{dataset}_{ts_suffix}.csv"
    else:
        content = _rows_to_xlsx_bytes(headers, data, title=title)
        filename = f"{dataset}_{ts_suffix}.xlsx"

    await context.bot.send_document(
        chat_id=query.message.chat.id,
        document=InputFile(io.BytesIO(content), filename=filename),
        caption=f"📊 Экспорт: {title} ({len(data)} записей)",
    )
    await _log_admin_action(pool, user.id, "export", details=f"{dataset}:{fmt}, rows={len(data)}")


async def restore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        await query.answer()
        return

    if query.data == "restore_no":
        _pending_restore.pop(user.id, None)
        await query.edit_message_text("Восстановление отменено.")
        await query.answer()
        return

    if query.data == "restore_yes":
        data = _pending_restore.pop(user.id, None)
        if not data:
            await query.edit_message_text(
                "⚠️ Данные для восстановления не найдены, отправьте файл заново через /restore."
            )
            await query.answer()
            return

        pool = context.bot_data["db_pool"]
        try:
            await _restore_data(pool, data)
        except Exception as exc:
            logger.exception("Ошибка восстановления из резервной копии")
            await query.edit_message_text(f"⚠️ Ошибка восстановления: {exc}")
            await query.answer()
            return

        await _refresh_admin_cache(context)
        await _log_admin_action(pool, user.id, "restore", details="restored from backup file")
        await query.edit_message_text("✅ Данные успешно восстановлены из резервной копии.")
        await query.answer()
        return

    await query.answer()


async def reset_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_reset_stats_pressed(update, context)


async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_chart_pressed(update, context)


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_export_pressed(update, context)


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_backup_pressed(update, context)


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    _admin_input_mode[user.id] = "restore"
    await update.message.reply_text(
        "♻️ Отправьте файл резервной копии (.json), созданный командой /backup.\n\n"
        "⚠️ Восстановление полностью заменит текущие данные бота."
    )


async def subscribers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_subscribers_pressed(update, context)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_settings_pressed(update, context)


async def autojoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_autojoin_pressed(update, context)


async def finduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return

    pool = context.bot_data["db_pool"]
    if context.args:
        query = " ".join(context.args).strip()
        result = await _format_user_search_results(pool, query)
        await update.message.reply_text(result)
    else:
        _admin_input_mode[user.id] = "find"
        await update.message.reply_text(
            "Отправьте ID пользователя или username (например: 123456789 или @nickname)."
        )


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_logs_pressed(update, context)


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_panel(update, context)


# ---------------------------------------------------------------------------
# Admin add/remove helper from message buttons
# ---------------------------------------------------------------------------


async def on_add_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_add_admin_pressed(update, context)


async def on_remove_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_remove_admin_pressed(update, context)


async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_settings_pressed(update, context)


async def on_autojoin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_autojoin_pressed(update, context)


async def on_find_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_find_user_pressed(update, context)


async def on_logs_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_logs_pressed(update, context)


async def on_reset_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_reset_stats_pressed(update, context)


async def on_chart_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_chart_pressed(update, context)


async def on_export_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_export_pressed(update, context)


async def on_backup_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_backup_pressed(update, context)


async def on_subscribers_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_subscribers_pressed(update, context)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Необработанное исключение при обработке update=%s", update, exc_info=context.error)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Ошибка в боте:\n{type(context.error).__name__}: {context.error}",
        )
    except Exception:
        pass


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_init_db)
        .post_shutdown(_close_db)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("reset_stats", reset_stats_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("autojoin", autojoin_command))
    application.add_handler(CommandHandler("finduser", finduser_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("chart", chart_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("restore", restore_command))
    application.add_handler(CommandHandler("subscribers", subscribers_command))
    application.add_handler(CommandHandler("admin", admin_panel_command))

    application.add_handler(ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(on_member_status_changed, ChatMemberHandler.CHAT_MEMBER))

    application.add_handler(MessageHandler(_AwaitingRestoreFileFilter() & filters.Document.ALL, on_restore_file_received))
    application.add_handler(MessageHandler(_AwaitingAdminInputFilter() & filters.TEXT & ~filters.COMMAND, on_admin_id_input))
    application.add_handler(MessageHandler(filters.Text([ADD_ADMIN_BTN]), on_add_admin_button))
    application.add_handler(MessageHandler(filters.Text([REMOVE_ADMIN_BTN]), on_remove_admin_button))
    application.add_handler(MessageHandler(filters.Text([SETTINGS_BTN]), on_settings_button))
    application.add_handler(MessageHandler(filters.Text([AUTOJOIN_BTN]), on_autojoin_button))
    application.add_handler(MessageHandler(filters.Text([FIND_USER_BTN]), on_find_user_button))
    application.add_handler(MessageHandler(filters.Text([LOGS_BTN]), on_logs_button))
    application.add_handler(MessageHandler(filters.Text([RESET_STATS_BTN]), on_reset_stats_button))
    application.add_handler(MessageHandler(filters.Text([CHART_BTN]), on_chart_button))
    application.add_handler(MessageHandler(filters.Text([EXPORT_BTN]), on_export_button))
    application.add_handler(MessageHandler(filters.Text([BACKUP_BTN]), on_backup_button))
    application.add_handler(MessageHandler(filters.Text([SUBSCRIBERS_BTN]), on_subscribers_button))
    application.add_handler(MessageHandler(filters.Text([CLOSE_PANEL_BTN]), on_close_admin_panel))

    application.add_handler(ChatJoinRequestHandler(on_join_request))

    application.add_handler(CallbackQueryHandler(stats_callback, pattern="^stats_"))
    application.add_handler(CallbackQueryHandler(reset_stats_callback, pattern="^resetstats_"))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern="^set:"))
    application.add_handler(CallbackQueryHandler(add_admin_callback, pattern="^deladmin_"))
    application.add_handler(CallbackQueryHandler(chart_callback, pattern="^chart_"))
    application.add_handler(CallbackQueryHandler(export_callback, pattern="^export:"))
    application.add_handler(CallbackQueryHandler(restore_callback, pattern="^restore_"))

    application.add_error_handler(_on_error)

    if WEBHOOK_URL:
        logger.info("Запуск в режиме webhook: %s", WEBHOOK_URL)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("WEBHOOK_URL не задан — запуск в режиме polling")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
