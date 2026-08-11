import os
import json
import random
import logging
import asyncio
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.error import ChatMigrated
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
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
_required_vars = ["BOT_TOKEN", "GEMINI_API_KEY", "CHANNEL_ID"]
_missing = [v for v in _required_vars if v not in os.environ]
if _missing:
    # Диагностика: показываем, какие переменные реально видит процесс
    # (только ИМЕНА, без значений — чтобы не светить токены/ключи в логах),
    # чтобы сразу было понятно, опечатка ли это, не тот сервис/группа и т.п.
    all_keys = sorted(os.environ.keys())
    logger.error(
        "Не найдены обязательные переменные окружения: %s. "
        "Всего переменных видно процессу: %d. "
        "Похожие по названию: %s. "
        "Первые 30 имён переменных, которые реально видит процесс: %s",
        _missing,
        len(all_keys),
        [k for k in all_keys if "CHAT" in k.upper() or "TOKEN" in k.upper() or "GEMINI" in k.upper()],
        all_keys[:30],
    )
    raise SystemExit(
        f"Отсутствуют переменные окружения: {_missing}. Смотрите лог выше — "
        "там список того, что реально видит процесс."
    )

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CHAT_ID = os.environ["CHANNEL_ID"]  # id группы, например -1001234567890. Имя переменной на Render: CHANNEL_ID

# Именинница зашита в код намертво. Менять её через /admin больше нельзя —
# соответствующий пункт меню и диалог назначения именинника удалены.
TARGET_NAME = "Вика"

# ID для кликабельного пинга в чате (необязательно). Если не задан — бот
# просто пишет имя без ссылки. Задаётся только переменной окружения,
# в рантайме через бота изменить нельзя.
_target_id_raw = os.environ.get("BIRTHDAY_ID", "").strip()
TARGET_ID = int(_target_id_raw) if _target_id_raw.lstrip("-").isdigit() else None


def decline_accusative(name: str) -> str:
    """Грубое склонение русского имени в винительный падеж — нужно для кнопки
    'Поздравить <Имя> с Днём Рождения!' (поздравить кого? — Вику, Машу...).
    Имя в состоянии бота (get_target_name) всегда хранится и используется в
    именительном падеже (кто? — Вика) — так оно правильно подставляется в
    текст поздравления ('С Днём Рождения, Вика!'). Эта функция только для
    кнопки, отдельного поля не заводим.
    Покрывает самые частые окончания; не претендует на полный морфоанализ —
    если паттерн не распознан (мужское имя на согласную/мягкий знак и т.п.),
    имя возвращается без изменений."""
    if not name:
        return name
    lower = name.lower()
    if lower.endswith("ия"):
        return name[:-1] + "ю"  # Мария -> Марию
    if lower.endswith("я"):
        return name[:-1] + "ю"  # Соня -> Соню, Наталья -> Наталью
    if lower.endswith("а"):
        return name[:-1] + "у"  # Вика -> Вику, Анна -> Анну
    return name
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", 3 * 60 * 60))  # 3 часа
FIRST_RUN_DELAY = int(os.environ.get("FIRST_RUN_DELAY", 30))

# Пользователи, которым доступно админ-меню назначения именинника.
# Несколько ID можно указать через запятую в переменной окружения ADMIN_ID,
# например: ADMIN_ID=6788511742,6024223246
_admin_ids_raw = os.environ.get("ADMIN_ID", "6788511742,6024223246")
ADMIN_IDS = {int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()}

TOGGLE_BOT_BUTTON_TEXT = "🔌 Вкл/выкл бота"
TOGGLE_VISIBILITY_BUTTON_TEXT = "🙈 Скрыть/показать кнопку"
BACK_TO_MAIN_BUTTON_TEXT = "◀️ Главное меню"

# Render (free Web Service) требует, чтобы сервис слушал HTTP-порт.
# Этот сервер также используется как health-check для UptimeRobot,
# чтобы сервис не засыпал.
PORT = int(os.environ.get("PORT", 10000))

genai.configure(api_key=GEMINI_API_KEY)

# Высокая temperature/top_p — чтобы модель реально генерировала разные варианты,
# а не скатывалась в один и тот же наиболее вероятный текст при одинаковом промпте.
GENERATION_CONFIG = genai.types.GenerationConfig(
    temperature=1.4,
    top_p=0.97,
    top_k=64,
)

model = genai.GenerativeModel(GEMINI_MODEL, generation_config=GENERATION_CONFIG)

PROMPT_TEMPLATE = (
    "Придумай короткое (3-5 предложений) тёплое и весёлое поздравление с Днём Рождения "
    "для человека по имени {name} от лица всей компании друзей ('от нас всех'). "
    "Можно с лёгким юмором и уместными эмодзи, но без пошлости и без клише вроде "
    "'желаю счастья здоровья' — постарайся быть оригинальным и живым. "
    "Стиль этого конкретного поздравления: {style}. "
    "{avoid_repeats}"
    "Выведи только сам текст поздравления, без вступлений и подписи."
)

# Разные "углы подачи" — чтобы каждый раз получался заметно другой текст,
# а не вариации одного и того же шаблона.
GREETING_STYLES = [
    "лёгкая ирония и дружеские подколки",
    "тёплое и трогательное, почти как небольшой тост",
    "в стиле короткой смешной истории или анекдота про именинника",
    "энергичное и залихватское, будто кричат хором на вечеринке",
    "поэтичное, с необычными сравнениями и метафорами",
    "в виде шутливых 'пожеланий от лица компании' с перечислением",
    "простое и искреннее, без вычурности, но с одной неожиданной шуткой",
    "в стиле кино-трейлера или спортивного комментатора",
]

# Храним несколько последних поздравлений, чтобы явно просить модель не повторяться.
_recent_greetings: deque[str] = deque(maxlen=5)

# Момент старта приложения. Нужен, чтобы отличать "бота реально только что
# добавили в чат" от "Telegram досрочно доставил старое my_chat_member
# событие после рестарта/пробуждения сервиса" — во втором случае приветствие
# слать не нужно, иначе оно дублирует сообщение "Бот перезапущен".
STARTUP_TIME = datetime.now(timezone.utc)

# ---------- Хранение текущего "именинника" (имя + telegram id для пинга) ----------
# Файл лежит рядом со скриптом. На бесплатном плане Render диск не гарантированно
# сохраняется между редеплоями (только между обычными рестартами одного и того же
# деплоя) — если после редеплоя настройки слетели, просто заново пройдите /admin.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "target_state.json")


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                # bot_active=False -> бот "выключен" для всех, кроме админов
                "bot_active": data.get("bot_active", True),
                # button_visible=False -> кнопка поздравления скрыта у всех
                "button_visible": data.get("button_visible", True),
            }
    except Exception:
        return {
            "bot_active": True,
            "button_visible": True,
        }


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
    except Exception:
        logger.exception("Не удалось сохранить target_state.json")


_state = load_state()


def get_target_name() -> str:
    # Зашито намертво — см. константу TARGET_NAME выше.
    return TARGET_NAME


def build_button_text() -> str:
    return f"Поздравить {decline_accusative(get_target_name())} с Днём Рождения!"


def get_target_id():
    # Зашито намертво — см. константу TARGET_ID выше.
    return TARGET_ID


def get_bot_active() -> bool:
    return _state.get("bot_active", True)


def set_bot_active(value: bool):
    _state["bot_active"] = value
    save_state()


def get_button_visible() -> bool:
    return _state.get("button_visible", True)


def set_button_visible(value: bool):
    _state["button_visible"] = value
    save_state()


def mention_or_name() -> str:
    """HTML-фрагмент с именем именинника. Если известен ID — это кликабельный
    пинг (tg://user?id=...), уведомляющий человека, даже если у него нет username."""
    name = get_target_name()
    target_id = get_target_id()
    if target_id:
        return f'<a href="tg://user?id={target_id}">{name}</a>'
    return name


FALLBACK_GREETINGS = [
    "🎉 С Днём Рождения, {name}! Желаем счастья, вдохновения и много поводов "
    "для улыбки. Обнимаем — от нас всех! 🎂",
    "🥳 {name}, с праздником! Пусть этот год принесёт побольше классных "
    "историй, которые потом захочется рассказывать. Мы рядом — от всей компании!",
    "🎂 Ура, у {name} День Рождения! Желаем, чтобы всё задуманное сбывалось "
    "легко и с огоньком. Обнимаем крепко — от нас всех!",
    "✨ С Днём Рождения, {name}! Пусть будет много смеха, вкусного торта и "
    "приятных сюрпризов. Ты классный — от всей нашей команды!",
]


def _build_prompt(name: str) -> str:
    style = random.choice(GREETING_STYLES)
    if _recent_greetings:
        recent_list = "\n".join(f"- {g}" for g in _recent_greetings)
        avoid_repeats = (
            "Вот несколько поздравлений, которые уже использовались недавно — "
            "не повторяй их формулировки, шутки и структуру, придумай действительно "
            f"новый текст:\n{recent_list}\n"
        )
    else:
        avoid_repeats = ""
    return PROMPT_TEMPLATE.format(name=name, style=style, avoid_repeats=avoid_repeats)


async def generate_greeting(name: str) -> str:
    prompt = _build_prompt(name)
    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        text = (response.text or "").strip()
        if not text:
            raise ValueError("empty response")
        _recent_greetings.append(text)
        return text
    except Exception:
        logger.exception("Gemini generation failed, using fallback greeting")
        template = random.choice(FALLBACK_GREETINGS)
        return template.format(name=name)


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


def build_keyboard():
    """Публичная клавиатура (видна всем участникам чата/ЛС).
    Если админ скрыл кнопку через панель управления — убираем клавиатуру
    у всех, вместо кнопки поздравления."""
    if not get_button_visible():
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(
        [[KeyboardButton(build_button_text())]],
        resize_keyboard=False,
        is_persistent=True,
    )


class _ButtonTextFilter(filters.MessageFilter):
    """Кнопка теперь содержит имя (склоняется автоматически), а значит её
    текст меняется вместе со сменой именинника. filters.Text([...]) со
    статичным списком тут не подходит, поэтому сверяем текст сообщения с
    текущим build_button_text() динамически, на каждое входящее сообщение."""

    def filter(self, message):
        return message.text == build_button_text()


button_filter = _ButtonTextFilter()


def build_admin_keyboard() -> ReplyKeyboardMarkup:
    """Панель управления — видна только админам (selective=True + фильтр
    на хендлерах ниже)."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(TOGGLE_BOT_BUTTON_TEXT)],
            [KeyboardButton(TOGGLE_VISIBILITY_BUTTON_TEXT)],
            [KeyboardButton(BACK_TO_MAIN_BUTTON_TEXT)],
        ],
        resize_keyboard=True,
        selective=True,
    )


async def safe_send_to_chat(context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """Отправка в основной групповой чат с обработкой миграции в супергруппу.
    Если Telegram сообщает новый chat_id (ChatMigrated), сразу пробуем отправить
    туда же, чтобы сообщение не потерялось. Постоянное решение — обновить
    переменную окружения CHANNEL_ID на новый id и перезапустить сервис."""
    global CHAT_ID
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=text, **kwargs)
    except ChatMigrated as e:
        new_id = e.new_chat_id
        logger.warning(
            "Группа мигрировала в супергруппу. Обновите CHANNEL_ID в Render на %s", new_id
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

    # Защита от "призрачных" событий: если Telegram доставил это my_chat_member
    # обновление с датой ДО момента старта приложения — значит, оно случилось,
    # пока бот был офлайн (перезапуск/сон Render), а не только что. Такие
    # события игнорируем, чтобы не слать "Привет!" повторно после рестарта.
    event_date = result.date
    if event_date is not None:
        if event_date.tzinfo is None:
            event_date = event_date.replace(tzinfo=timezone.utc)
        if event_date < STARTUP_TIME:
            return

    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    just_added = old_status in ("left", "kicked") and new_status in ("member", "administrator")
    if just_added:
        if get_bot_active():
            await send_welcome(context, result.chat.id)
        else:
            await context.bot.send_message(
                chat_id=result.chat.id,
                text="Бот работает, но временно отключён администратором.",
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открытый хендлер (без admin_filter на регистрации), потому что для
    не-админов в ЛС он должен отвечать простым "Бот работает". Вся логика
    ограничения — внутри функции."""
    user = update.effective_user
    is_admin = user is not None and user.id in ADMIN_IDS
    chat = update.effective_chat

    if is_admin:
        await send_welcome(context, CHAT_ID)
        if chat is not None and str(chat.id) != str(CHAT_ID):
            await update.message.reply_text("Кнопка отправлена в общий чат 👍")
        return

    # Не-админ.
    if chat is not None and chat.type == "private":
        # В ЛС бот всегда отвечает только на /start и только этим текстом,
        # никакой другой функциональности не-админам в ЛС не показываем.
        await update.message.reply_text("Бот работает")
    # В групповом чате не-админам бот вообще не отвечает — сообщение просто
    # игнорируется (см. также остальные хендлеры, они все admin_filter).
    return


async def on_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Хендлер уже зарегистрирован с admin_filter (см. main()), сюда попадают
    # только сообщения от админов. Дополнительно проверяем, что это тот самый
    # групповой чат — личные сообщения боту эту логику не запускают.
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
    bot_status = "включён 🟢" if get_bot_active() else "выключен 🔴"
    button_status = "видна 👁" if get_button_visible() else "скрыта 🙈"
    await update.message.reply_text(
        "Панель управления ботом.\n\n"
        f"Бот сейчас: {bot_status}\n"
        f"Кнопка поздравления сейчас: {button_status}\n\n"
        "🔌 — включить/выключить бота для всех, кроме админов\n"
        "🙈 — скрыть/показать кнопку поздравления для всех",
        reply_markup=build_admin_keyboard(),
    )


async def toggle_bot_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_value = not get_bot_active()
    set_bot_active(new_value)
    if new_value:
        status_text = "включён 🟢. Обычная работа восстановлена."
        chat_notice = "Бот снова включён и работает в обычном режиме."
        chat_markup = build_keyboard()
    else:
        status_text = (
            "выключен 🔴. Для всех, кроме админов, в ЛС бот отвечает только "
            "«Бот работает», а в этом чате вообще не реагирует на команды."
        )
        chat_notice = "Бот временно отключён администратором."
        chat_markup = ReplyKeyboardRemove()

    await update.message.reply_text(
        f"Готово ✅ Бот теперь {status_text}", reply_markup=build_admin_keyboard()
    )
    try:
        await safe_send_to_chat(context, chat_notice, reply_markup=chat_markup)
    except Exception:
        logger.exception("Не удалось разослать уведомление о смене статуса бота в чат")


async def toggle_button_visible(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_value = not get_button_visible()
    set_button_visible(new_value)
    if new_value:
        status_text = "показана 👁 для всех."
        chat_notice = "Кнопка снова доступна."
    else:
        status_text = "скрыта 🙈 для всех (и в ЛС, и в чате)."
        chat_notice = "Кнопка временно скрыта администратором."

    await update.message.reply_text(
        f"Готово ✅ Кнопка теперь {status_text}", reply_markup=build_admin_keyboard()
    )
    try:
        await safe_send_to_chat(context, chat_notice, reply_markup=build_keyboard())
    except Exception:
        logger.exception("Не удалось разослать уведомление о смене видимости кнопки в чат")


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню.", reply_markup=build_keyboard())


def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    admin_filter = filters.User(user_id=ADMIN_IDS)

    # /start открыт для всех на регистрации (иначе не-админы не получат даже
    # "Бот работает" в ЛС), но вся логика ограничений — внутри функции start().
    application.add_handler(CommandHandler("start", start))

    # Кнопка поздравления, admin-панель и все её действия — реагируют только
    # на администраторов бота. Остальные участники чата просто видят
    # автопоздравления, но не могут ничего вызвать.
    application.add_handler(
        MessageHandler(button_filter & admin_filter, on_button_pressed)
    )
    application.add_handler(
        ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(CommandHandler("admin", admin_menu, filters=admin_filter))
    application.add_handler(
        MessageHandler(filters.Text([TOGGLE_BOT_BUTTON_TEXT]) & admin_filter, toggle_bot_active)
    )
    application.add_handler(
        MessageHandler(
            filters.Text([TOGGLE_VISIBILITY_BUTTON_TEXT]) & admin_filter, toggle_button_visible
        )
    )
    application.add_handler(
        MessageHandler(filters.Text([BACK_TO_MAIN_BUTTON_TEXT]) & admin_filter, back_to_main)
    )

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
