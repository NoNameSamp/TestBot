"""
Бот для доски объявлений с мини-приложением.

Как это работает:
1. Пользователь жмёт /start и открывает мини-приложение (кнопка "Опубликовать объявление").
2. В мини-приложении пишет текст, жмёт "Опубликовать" -> текст прилетает боту как web_app_data.
3. Если в мини-приложении был выбран флажок "есть фото" — бот просит прислать фото обычным
   сообщением. Если фото не нужно — объявление публикуется сразу.
4. Готовое объявление (текст + фото, если есть) публикуется в канал/группу (CHANNEL_ID)
   или, если канал не настроен, просто подтверждается пользователю в личке (режим демо).

Переменные окружения:
  BOT_TOKEN    - токен бота от @BotFather (обязательно)
  WEBAPP_URL   - https-ссылка на захостенный index.html (обязательно)
  CHANNEL_ID   - id канала/группы для публикации, например -1001234567890 (опционально)
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # можно не задавать на этапе тестов


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "📌 Опубликовать объявление",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )]]
    )
    await update.message.reply_text(
        "Привет! Здесь можно опубликовать объявление.\n\n"
        "Нажмите кнопку ниже, напишите текст и при желании прикрепите фото.",
        reply_markup=keyboard,
    )


async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем текст объявления из мини-приложения."""
    try:
        data = json.loads(update.effective_message.web_app_data.data)
    except (json.JSONDecodeError, AttributeError):
        await update.message.reply_text("Не удалось прочитать данные из формы, попробуйте ещё раз.")
        return

    text = (data.get("text") or "").strip()
    has_photo = bool(data.get("hasPhoto"))

    if not text:
        await update.message.reply_text("Текст объявления пустой, попробуйте снова.")
        return

    if has_photo:
        context.user_data["pending_ad_text"] = text
        await update.message.reply_text(
            "Текст принят ✅\nТеперь пришлите фото к объявлению (или отправьте /skip, чтобы опубликовать без фото)."
        )
    else:
        await publish_ad(update, context, text=text, photo_file_id=None)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем фото, если бот ждёт его после текста объявления."""
    pending_text = context.user_data.get("pending_ad_text")
    if not pending_text:
        return  # фото пришло не в рамках публикации объявления — игнорируем

    photo_file_id = update.message.photo[-1].file_id
    context.user_data.pop("pending_ad_text", None)
    await publish_ad(update, context, text=pending_text, photo_file_id=photo_file_id)


async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_text = context.user_data.get("pending_ad_text")
    if not pending_text:
        await update.message.reply_text("Нет объявления, ожидающего фото.")
        return
    context.user_data.pop("pending_ad_text", None)
    await publish_ad(update, context, text=pending_text, photo_file_id=None)


async def publish_ad(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, photo_file_id: str | None):
    author = update.effective_user
    caption = f"{text}\n\n— {author.full_name}"

    target_chat = CHANNEL_ID or update.effective_chat.id  # без канала публикуем прямо в чат с пользователем (демо-режим)

    if photo_file_id:
        await context.bot.send_photo(chat_id=target_chat, photo=photo_file_id, caption=caption)
    else:
        await context.bot.send_message(chat_id=target_chat, text=caption)

    if CHANNEL_ID:
        await update.message.reply_text("Готово, объявление опубликовано в канале ✅")
    else:
        await update.message.reply_text(
            "Готово! (CHANNEL_ID не задан, поэтому объявление показано здесь, а не в канале — см. README)"
        )


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # не засоряем логи health-check запросами


def start_health_server():
    """Render (и подобные хостинги) держат сервис живым, пока он отвечает на HTTP.
    Бот сам по себе HTTP не слушает (работает через polling), поэтому поднимаем
    отдельный мини-сервер, который просто отвечает 200 OK на любой запрос."""
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"Health-check сервер запущен на порту {port}")


def main():
    start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("skip", skip_photo))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    log.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
