import os
import logging
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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

# --- Настройки (переменные окружения на Render) ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6788511742"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", "10000"))

BUTTON_TEXT = "Привет от @UserTG18"


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def _restore_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BUTTON_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if _is_admin(user.id):
        text = (
            "Бот запущен.\n\n"
            "Команды для администратора:\n"
            "/remove_button — принудительно убрать клавиатуру с кнопкой в этом чате\n"
            "/restore_button — вернуть клавиатуру с кнопкой в этом чате"
        )
    else:
        text = "Бот запущен."

    await update.message.reply_text(text)


async def remove_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        return  # молча игнорируем для всех, кроме владельца

    # Пустое/техническое сообщение с ReplyKeyboardRemove — стирает клавиатуру у всех,
    # кто откроет чат после этого сообщения.
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔕",
        reply_markup=ReplyKeyboardRemove(),
    )


async def restore_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        return

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🎉",
        reply_markup=_restore_keyboard(),
    )



def big_keyboard():
    rows=[]
    for _ in range(15):
        row=[]
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
    try:
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user.id)
        logger.info("Заявка на вступление одобрена: user_id=%s chat_id=%s", user.id, chat_id)

        username = f"@{user.username}" if user.username else "(без username)"
        full_name = user.full_name or "Без имени"
        chat_title = request.chat.title or str(chat_id)

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


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("remove_button", remove_button))
    application.add_handler(CommandHandler("restore_button", restore_button))
    application.add_handler(
        ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(
        MessageHandler(filters.Text([BUTTON_TEXT]), on_congratulate_pressed)
    )
    application.add_handler(ChatJoinRequestHandler(on_join_request))

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
