import os
import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Настройки (переменные окружения на Render) ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6788511742"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))

# Render Postgres: во вкладке базы данных — "Internal Database URL" (если бот
# на том же Render-аккаунте) или "External Database URL". Формат:
# postgresql://user:password@host:5432/dbname
DATABASE_URL = os.environ["DATABASE_URL"]
# Render внешним подключениям обычно требует SSL. Если используете Internal URL
# в том же регионе — можно поставить DB_SSL=disable в переменных окружения.
DB_SSL = os.environ.get("DB_SSL", "require")

BUTTON_TEXT = "Привет от @UserTG18"

ADD_ADMIN_BTN = "➕ Добавить администратора"
REMOVE_ADMIN_BTN = "➖ Убрать администратора"
CLOSE_PANEL_BTN = "🔙 Закрыть панель"

# user_id -> "add"  (в памяти процесса; сбрасывается при рестарте бота — это ок,
# просто нужно будет заново нажать кнопку "Добавить администратора")
_awaiting_admin_input: dict[int, str] = {}


def _is_admin(user_id: int, admin_ids: set[int] | None = None) -> bool:
    if admin_ids is None:
        admin_ids = {ADMIN_ID}
    return user_id == ADMIN_ID or user_id in admin_ids


class _AwaitingAdminIdFilter(filters.MessageFilter):
    """Пропускает сообщение, только если от этого пользователя ждём ввод ID администратора."""

    def filter(self, message):
        user = message.from_user
        return bool(user and user.id in _awaiting_admin_input)


def _restore_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BUTTON_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


# ---------------------------------------------------------------------------
# База данных (Render PostgreSQL, через asyncpg)
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
                event_type TEXT NOT NULL,   -- 'join' или 'leave'
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
        # Владелец (ADMIN_ID из переменных окружения) всегда в списке админов
        await conn.execute(
            "INSERT INTO admins (user_id, username, added_by, added_at) "
            "VALUES ($1, NULL, $1, $2) ON CONFLICT (user_id) DO NOTHING",
            ADMIN_ID, datetime.now(timezone.utc),
        )
        rows = await conn.fetch("SELECT user_id FROM admins")

    application.bot_data["admin_ids"] = {r["user_id"] for r in rows} | {ADMIN_ID}
    logger.info("Подключение к PostgreSQL установлено, таблицы events/admins готовы")


async def _close_db(application: Application):
    pool = application.bot_data.get("db_pool")
    if pool:
        await pool.close()
        logger.info("Пул подключений к PostgreSQL закрыт")


async def _log_event(pool: asyncpg.Pool, chat_id: int, user_id: int, username: str, full_name: str, event_type: str):
    async with pool.acquire() as conn:
        if event_type=="join":
            ex=await conn.fetchval("SELECT 1 FROM events WHERE chat_id=$1 AND user_id=$2 AND event_type='join' LIMIT 1",chat_id,user_id)
            if ex: return

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO events (chat_id, user_id, username, full_name, event_type, ts) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            chat_id, user_id, username, full_name, event_type, datetime.now(timezone.utc),
        )


async def _get_stats(pool: asyncpg.Pool, period: str):
    """period: 'day' | 'week' | 'month'"""
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


def _stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("День", callback_data="stats_day"),
                InlineKeyboardButton("Неделя", callback_data="stats_week"),
                InlineKeyboardButton("Месяц", callback_data="stats_month"),
            ]
        ]
    )


async def _stats_text(pool: asyncpg.Pool, period: str) -> str:
    label, joins, leaves = await _get_stats(pool, period)
    return (
        f"📊 Статистика {label}\n\n"
        f"➕ Вступлений: {joins}\n"
        f"➖ Выходов: {leaves}\n"
        f"Δ Итог: {joins - leaves:+d}"
    )


# ---------------------------------------------------------------------------
# Управление администраторами
# ---------------------------------------------------------------------------

async def _list_admins(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT user_id, username, added_by, added_at FROM admins ORDER BY added_at"
        )


async def _add_admin(pool: asyncpg.Pool, user_id: int, username: str | None, added_by: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO admins (user_id, username, added_by, added_at) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username",
            user_id, username, added_by, datetime.now(timezone.utc),
        )


async def _remove_admin(pool: asyncpg.Pool, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM admins WHERE user_id = $1", user_id)


async def _refresh_admin_cache(context: ContextTypes.DEFAULT_TYPE):
    pool = context.bot_data["db_pool"]
    rows = await _list_admins(pool)
    context.bot_data["admin_ids"] = {r["user_id"] for r in rows} | {ADMIN_ID}


async def _admins_card_text(pool: asyncpg.Pool) -> str:
    rows = await _list_admins(pool)
    if not rows:
        return "👑 Администраторов пока нет."
    lines = ["👑 Текущие администраторы:\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "(без username)"
        mark = " — владелец" if r["user_id"] == ADMIN_ID else ""
        lines.append(f"• ID: {r['user_id']} {uname}{mark}")
    return "\n".join(lines)


def _admin_panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(ADD_ADMIN_BTN), KeyboardButton(REMOVE_ADMIN_BTN)],
            [KeyboardButton(CLOSE_PANEL_BTN)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _remove_admin_inline_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        if r["user_id"] == ADMIN_ID:
            continue  # владельца снять нельзя
        label = f"@{r['username']}" if r["username"] else str(r["user_id"])
        buttons.append(
            [InlineKeyboardButton(f"❌ {label} ({r['user_id']})", callback_data=f"deladmin_{r['user_id']}")]
        )
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})

    if _is_admin(user.id, admin_ids):
        text = (
            "Бот запущен.\n\n"
            "Команды для администратора:\n"
            "/remove_button — принудительно убрать клавиатуру с кнопкой в этом чате\n"
            "/restore_button — вернуть клавиатуру с кнопкой в этом чате\n"
            "/stats — статистика вступлений/выходов (день/неделя/месяц)\n"
            "/admin — панель управления администраторами"
        )
    else:
        text = "Бот запущен."

    await update.message.reply_text(text)


async def remove_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return  # молча игнорируем для всех, кроме админов

    # Пустое/техническое сообщение с ReplyKeyboardRemove — стирает клавиатуру у всех,
    # кто откроет чат после этого сообщения.
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔕",
        reply_markup=ReplyKeyboardRemove(),
    )


async def restore_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🎉",
        reply_markup=_restore_keyboard(),
    )


def big_keyboard():
    rows = []
    for _ in range(15):
        row = []
        for _ in range(5):
            row.append(KeyboardButton(BUTTON_TEXT))
        rows.append(row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Как только бота добавляют в группу — сразу принудительно чистим старую клавиатуру."""
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
        except Exception as e:
            logger.warning("Не удалось сбросить клавиатуру в чате %s: %s", chat_id, e)


async def on_congratulate_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит нажатие восстановленной кнопки (она шлёт обычное текстовое сообщение)."""
    user = update.effective_user
    await update.message.reply_text(
        f"🎂 {user.first_name} поздравил(а) Татьяну с Днём Рождения!"
    )


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автоматически одобряет все заявки на вступление в канал/чат."""
    request = update.chat_join_request
    chat_id = request.chat.id
    user = request.from_user
    pool = context.bot_data["db_pool"]

    try:
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user.id)
        logger.info("Заявка на вступление одобрена: user_id=%s chat_id=%s", user.id, chat_id)

        username = f"@{user.username}" if user.username else "(без username)"
        full_name = user.full_name or "Без имени"
        chat_title = request.chat.title or str(chat_id)

        await _log_event(pool, chat_id, user.id, user.username or "", full_name, "join")

        notify_text = (
            "✅ Принята заявка на вступление\n\n"
            f"Канал/чат: {chat_title}\n"
            f"Пользователь: {full_name} {username}\n"
            f"ID: {user.id}"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=notify_text)
        except Exception as notify_err:
            logger.warning("Не удалось отправить уведомление админу: %s", notify_err)

    except Exception as e:
        logger.warning(
            "Не удалось одобрить заявку user_id=%s chat_id=%s: %s", user.id, chat_id, e
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "⚠️ Не удалось одобрить заявку\n\n"
                    f"user_id: {user.id}\n"
                    f"chat_id: {chat_id}\n"
                    f"Ошибка: {e}"
                ),
            )
        except Exception:
            pass


async def on_member_status_changed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отслеживает выходы/исключения участников (для статистики)."""
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

        username = f"@{user.username}" if user.username else "(без username)"
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "❌ Пользователь покинул канал/чат\n\n"
                    f"Пользователь: {full_name}\n"
                    f"Юзернейм: {username}\n"
                    f"ID: {user.id}"
                ),
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление о выходе: %s", e)


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
    await update.message.reply_text(
        "🛠 Панель администратора",
        reply_markup=_admin_panel_keyboard(),
    )


async def on_add_admin_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    pool = context.bot_data["db_pool"]
    card = await _admins_card_text(pool)
    _awaiting_admin_input[user.id] = "add"
    await update.message.reply_text(
        f"{card}\n\nОтправьте ID пользователя, которого нужно назначить администратором.\n"
        f"Отмена — нажмите /admin ещё раз."
    )


async def on_remove_admin_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    pool = context.bot_data["db_pool"]
    rows = await _list_admins(pool)
    removable_kb = _remove_admin_inline_keyboard(rows)
    card = await _admins_card_text(pool)
    if not removable_kb.inline_keyboard:
        await update.message.reply_text(f"{card}\n\nНекого убирать (кроме владельца).")
        return
    await update.message.reply_text(
        f"{card}\n\nВыберите, кого убрать из администраторов:",
        reply_markup=removable_kb,
    )


async def on_close_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = context.bot_data.get("admin_ids", {ADMIN_ID})
    if not _is_admin(user.id, admin_ids):
        return
    _awaiting_admin_input.pop(user.id, None)
    await update.message.reply_text("Панель закрыта.", reply_markup=_restore_keyboard())


async def on_admin_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if _awaiting_admin_input.get(user.id) != "add":
        return

    text = (update.message.text or "").strip()
    if not text.lstrip("-").isdigit():
        await update.message.reply_text(
            "ID должен быть числом. Пришлите числовой ID пользователя, либо нажмите /admin для отмены."
        )
        return

    target_id = int(text)
    pool = context.bot_data["db_pool"]

    username = None
    try:
        chat = await context.bot.get_chat(target_id)
        username = chat.username
    except Exception as e:
        logger.info("Не удалось получить username для %s: %s", target_id, e)

    await _add_admin(pool, target_id, username, added_by=user.id)
    await _refresh_admin_cache(context)
    _awaiting_admin_input.pop(user.id, None)

    card = await _admins_card_text(pool)
    await update.message.reply_text(f"✅ Пользователь {target_id} назначен администратором.\n\n{card}")


async def remove_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await _remove_admin(pool, target_id)
    await _refresh_admin_cache(context)

    rows = await _list_admins(pool)
    card = await _admins_card_text(pool)
    removable_kb = _remove_admin_inline_keyboard(rows)

    if removable_kb.inline_keyboard:
        await query.edit_message_text(
            f"{card}\n\n✅ Администратор {target_id} удалён.\n\nВыберите, кого ещё убрать:",
            reply_markup=removable_kb,
        )
    else:
        await query.edit_message_text(f"{card}\n\n✅ Администратор {target_id} удалён.")
    await query.answer()


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_init_db)
        .post_shutdown(_close_db)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("remove_button", remove_button))
    application.add_handler(CommandHandler("restore_button", restore_button))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(
        ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(
        ChatMemberHandler(on_member_status_changed, ChatMemberHandler.CHAT_MEMBER)
    )
    # Ввод ID при добавлении админа — должен проверяться раньше остальных текстовых
    # хендлеров, но матчится только у того, кто реально ждёт ввода (см. _AwaitingAdminIdFilter).
    application.add_handler(
        MessageHandler(_AwaitingAdminIdFilter() & filters.TEXT & ~filters.COMMAND, on_admin_id_input)
    )
    application.add_handler(MessageHandler(filters.Text([ADD_ADMIN_BTN]), on_add_admin_pressed))
    application.add_handler(MessageHandler(filters.Text([REMOVE_ADMIN_BTN]), on_remove_admin_pressed))
    application.add_handler(MessageHandler(filters.Text([CLOSE_PANEL_BTN]), on_close_admin_panel))
    application.add_handler(
        MessageHandler(filters.Text([BUTTON_TEXT]), on_congratulate_pressed)
    )
    application.add_handler(ChatJoinRequestHandler(on_join_request))
    application.add_handler(CallbackQueryHandler(stats_callback, pattern="^stats_"))
    application.add_handler(CallbackQueryHandler(remove_admin_callback, pattern="^deladmin_"))

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
