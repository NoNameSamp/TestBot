import os
import json
import logging
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.error import ChatMigrated
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
import google.generativeai as genai

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Настройки из переменных окружения ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CHAT_ID = os.environ["CHAT_ID"]  # id группы, например -1001234567890

DEFAULT_BIRTHDAY_NAME = os.environ.get("BIRTHDAY_NAME", "Вику")
# Текст кнопки теперь не завязан на имя — имя именинника можно менять "на лету"
# через админ-меню, не пересоздавая клавиатуру у всех участников заново.
BUTTON_TEXT = os.environ.get("BUTTON_TEXT", "🎉 Поздравить с Днём Рождения")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", 3 * 60 * 60))  # 3 часа
FIRST_RUN_DELAY = int(os.environ.get("FIRST_RUN_DELAY", 30))

# Единственный пользователь, которому доступно админ-меню назначения именинника.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6788511742"))

ADMIN_BUTTON_TEXT = "⚙️ Назначить именинника"
ASK_ID, ASK_NAME = range(2)

# Render (free Web Service) требует, чтобы сервис слушал HTTP-порт.
# Этот сервер также используется как health-check для UptimeRobot,
# чтобы сервис не засыпал.
PORT = int(os.environ.get("PORT", 10000))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL)

PROMPT_TEMPLATE = (
    "Придумай короткое (3-5 предложений) тёплое и весёлое поздравление с Днём Рождения "
    "для человека по имени {name} от лица всей компании друзей ('от нас всех'). "
    "Можно с лёгким юмором и уместными эмодзи, но без пошлости и без клише вроде "
    "'желаю счастья здоровья' — постарайся быть оригинальным и живым. "
    "Каждый раз придумывай новый текст, не повторяйся. "
    "Выведи только сам текст поздравления, без вступлений и подписи."
)

# ---------- Хранение текущего "именинника" (имя + telegram id для пинга) ----------
# Файл лежит рядом со скриптом. На бесплатном плане Render диск не гарантированно
# сохраняется между редеплоями (только между обычными рестартами одного и того же
# деплоя) — если после редеплоя настройки слетели, просто заново пройдите /admin.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "target_state.json")


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {"name": data.get("name", DEFAULT_BIRTHDAY_NAME), "id": data.get("id")}
    except Exception:
        return {"name": DEFAULT_BIRTHDAY_NAME, "id": None}


def save_state(name, user_id):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"name": name, "id": user_id}, f, ensure_ascii=False)
    except Exception:
        logger.exception("Не удалось сохранить target_state.json")


_state = load_state()


def get_target_name() -> str:
    return _state["name"]


def get_target_id():
    return _state["id"]


def set_target(name, user_id):
    _state["name"] = name
    _state["id"] = user_id
    save_state(name, user_id)


def mention_or_name() -> str:
    """HTML-фрагмент с именем именинника. Если известен ID — это кликабельный
    пинг (tg://user?id=...), уведомляющий человека, даже если у него нет username."""
    name = get_target_name()
    target_id = get_target_id()
    if target_id:
        return f'<a href="tg://user?id={target_id}">{name}</a>'
    return name


async def generate_greeting(name: str) -> str:
    prompt = PROMPT_TEMPLATE.format(name=name)
    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        text = (response.text or "").strip()
        if not text:
            raise ValueError("empty response")
        return text
    except Exception:
        logger.exception("Gemini generation failed, using fallback greeting")
        return (
            f"🎉 С Днём Рождения, {name}! Желаем счастья, вдохновения и много поводов "
            f"для улыбки. Обнимаем — от нас всех! 🎂"
        )


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("OK, bot is alive".encode("utf-8"))

    def log_message(self, format, *args):
        # не засоряем логи health-check запросами от UptimeRobot
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
    logger.info(f"Health-check сервер слушает порт {PORT}")
    server.serve_forever()


def build_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def safe_send_to_chat(context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """Отправка в основной групповой чат с обработкой миграции в супергруппу.
    Если Telegram сообщает новый chat_id (ChatMigrated), сразу пробуем отправить
    туда же, чтобы сообщение не потерялось. Постоянное решение — обновить
    переменную окружения CHAT_ID на новый id и перезапустить сервис."""
    global CHAT_ID
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=text, **kwargs)
    except ChatMigrated as e:
        new_id = e.new_chat_id
        logger.warning(
            "Группа мигрировала в супергруппу. Обновите CHAT_ID в Render на %s", new_id
        )
        CHAT_ID = str(new_id)
        await context.bot.send_message(chat_id=CHAT_ID, text=text, **kwargs)


async def send_welcome(context: ContextTypes.DEFAULT_TYPE, chat_id):
    await context.bot.send_message(
        chat_id=chat_id,
        text="Привет! Я бот-поздравлятор 🎉\n"
        "Нажимайте на кнопку ниже, чтобы поздравить именинника, "
        "а ещё я сам буду присылать поздравления каждые пару часов.",
        reply_markup=build_keyboard(),
    )


async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает, когда бота добавляют в группу (или меняют его права там).
    Отправляет полноценное приветствие с кнопкой сразу, без ручного /start."""
    result = update.my_chat_member
    if result is None:
        return
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    just_added = old_status in ("left", "kicked") and new_status in ("member", "administrator")
    if just_added:
        await send_welcome(context, result.chat.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome(context, CHAT_ID)
    if update.effective_chat is not None and str(update.effective_chat.id) != str(CHAT_ID):
        await update.message.reply_text("Кнопка отправлена в общий чат 👍")


async def on_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Реагируем на нажатие кнопки только в самом групповом чате —
    # личные сообщения боту эту логику не запускают.
    if update.effective_chat is None or str(update.effective_chat.id) != str(CHAT_ID):
        return
    greeting = await generate_greeting(get_target_name())
    text = f"{mention_or_name()}\n\n{greeting}"
    await update.message.reply_text(
        text, reply_markup=build_keyboard(), parse_mode=ParseMode.HTML
    )


async def scheduled_greeting(context: ContextTypes.DEFAULT_TYPE):
    greeting = await generate_greeting(get_target_name())
    text = f"{mention_or_name()}\n\n{greeting}"
    # reply_markup здесь тоже нужен: так клавиатура с кнопкой регулярно
    # "напоминает о себе" и остаётся видна всем участникам чата, даже тем,
    # кто присоединился позже или у кого клавиатура случайно закрылась.
    await safe_send_to_chat(
        context, text, reply_markup=build_keyboard(), parse_mode=ParseMode.HTML
    )


async def post_init(application: Application):
    # Отправляем клавиатуру в группу сразу при старте бота, чтобы кнопка
    # появилась у всех участников чата без необходимости писать /start.
    try:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text="Бот перезапущен. Нажимайте на кнопку ниже, чтобы поздравить именинника 🎉",
            reply_markup=build_keyboard(),
        )
    except Exception:
        logger.exception("Не удалось отправить стартовое сообщение с кнопкой в чат")


# ---------------- Админ-меню: назначить именинника ----------------

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # selective=True + reply на это сообщение -> клавиатуру видит только админ.
    # Но главная защита — фильтр по ADMIN_ID на самом хендлере ниже.
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(ADMIN_BUTTON_TEXT)]],
        resize_keyboard=True,
        selective=True,
    )
    await update.message.reply_text(
        "Админ-меню. Нажмите кнопку, чтобы назначить, кого поздравлять.",
        reply_markup=keyboard,
    )


async def admin_flow_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Отправьте Telegram ID пользователя, которого нужно поздравлять "
        "(число). Узнать ID можно, например, через бота @userinfobot.\n"
        "Отменить — /cancel.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_ID


async def admin_flow_got_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit():
        await update.message.reply_text(
            "Это не похоже на ID (нужны только цифры). Попробуйте ещё раз или /cancel."
        )
        return ASK_ID
    context.user_data["new_target_id"] = int(raw)
    await update.message.reply_text(
        "Принято. Теперь напишите, как боту обращаться к этому человеку "
        f"(например: {DEFAULT_BIRTHDAY_NAME}).\n"
        "Если хотите оставить текущее имя без изменений — отправьте /skip."
    )
    return ASK_NAME


async def admin_flow_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    target_id = context.user_data.get("new_target_id")
    set_target(name, target_id)
    await update.message.reply_text(
        f"Готово ✅ Теперь буду поздравлять «{name}» (ID: {target_id}) и пинговать его в чате.",
        reply_markup=build_keyboard(),
    )
    context.user_data.pop("new_target_id", None)
    return ConversationHandler.END


async def admin_flow_skip_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_id = context.user_data.get("new_target_id")
    name = get_target_name()
    set_target(name, target_id)
    await update.message.reply_text(
        f"Готово ✅ Имя оставил прежним («{name}»), обновил только ID: {target_id}.",
        reply_markup=build_keyboard(),
    )
    context.user_data.pop("new_target_id", None)
    return ConversationHandler.END


async def admin_flow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_target_id", None)
    await update.message.reply_text("Отменено.", reply_markup=build_keyboard())
    return ConversationHandler.END


def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    admin_filter = filters.User(user_id=ADMIN_ID)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.Text([BUTTON_TEXT]), on_button_pressed)
    )
    application.add_handler(
        ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(CommandHandler("admin", admin_menu, filters=admin_filter))

    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Text([ADMIN_BUTTON_TEXT]) & admin_filter, admin_flow_entry
            )
        ],
        states={
            ASK_ID: [
                CommandHandler("cancel", admin_flow_cancel, filters=admin_filter),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter, admin_flow_got_id
                ),
            ],
            ASK_NAME: [
                CommandHandler("cancel", admin_flow_cancel, filters=admin_filter),
                CommandHandler("skip", admin_flow_skip_name, filters=admin_filter),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter, admin_flow_got_name
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_flow_cancel, filters=admin_filter)],
    )
    application.add_handler(admin_conv)

    if application.job_queue is not None:
        application.job_queue.run_repeating(
            scheduled_greeting, interval=INTERVAL_SECONDS, first=FIRST_RUN_DELAY
        )
    else:
        logger.warning(
            "JobQueue недоступен — установите 'python-telegram-bot[job-queue]'"
        )

    logger.info("Бот запущен, поллинг...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
