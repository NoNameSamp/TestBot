import os
import logging
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
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

BIRTHDAY_NAME = os.environ.get("BIRTHDAY_NAME", "Вику")
BUTTON_TEXT = os.environ.get(
    "BUTTON_TEXT", f"🎉 Поздравить {BIRTHDAY_NAME} с Днём Рождения"
)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", 3 * 60 * 60))  # 3 часа
FIRST_RUN_DELAY = int(os.environ.get("FIRST_RUN_DELAY", 30))

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Кнопка должна уходить всем участникам группового чата, а не в личку.
    # Поэтому независимо от того, откуда пришёл /start, клавиатуру отправляем
    # именно в групповой чат (CHAT_ID) — там reply-клавиатура видна всем.
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text="Привет! Нажимайте на кнопку ниже, чтобы поздравить именинницу 🎉\n"
        "А ещё я сам буду присылать поздравления каждые пару часов.",
        reply_markup=build_keyboard(),
    )
    # Если /start пришёл не из самой группы (например, кто-то написал боту лично),
    # вежливо объясняем, что кнопка теперь в группе, а не в личке.
    if update.effective_chat is not None and str(update.effective_chat.id) != str(CHAT_ID):
        await update.message.reply_text("Кнопка отправлена в общий чат 👍")


async def on_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Реагируем на нажатие кнопки только в самом групповом чате —
    # личные сообщения боту эту логику не запускают.
    if update.effective_chat is None or str(update.effective_chat.id) != str(CHAT_ID):
        return
    greeting = await generate_greeting(BIRTHDAY_NAME)
    await update.message.reply_text(greeting, reply_markup=build_keyboard())


async def scheduled_greeting(context: ContextTypes.DEFAULT_TYPE):
    greeting = await generate_greeting(BIRTHDAY_NAME)
    # reply_markup здесь тоже нужен: так клавиатура с кнопкой регулярно
    # "напоминает о себе" и остаётся видна всем участникам чата, даже тем,
    # кто присоединился позже или у кого клавиатура случайно закрылась.
    await context.bot.send_message(
        chat_id=CHAT_ID, text=greeting, reply_markup=build_keyboard()
    )


async def post_init(application: Application):
    # Отправляем клавиатуру в группу сразу при старте бота, чтобы кнопка
    # появилась у всех участников чата без необходимости писать /start.
    try:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text="Бот перезапущен. Нажимайте на кнопку ниже, чтобы поздравить именинницу 🎉",
            reply_markup=build_keyboard(),
        )
    except Exception:
        logger.exception("Не удалось отправить стартовое сообщение с кнопкой в чат")


def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Text([BUTTON_TEXT]), on_button_pressed))

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
