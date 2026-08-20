"""
🤖 Telegram Stalker Bot - Полная версия
Отслеживает: имя, юзернейм, фото, био, онлайн, телефон (если доступен)
Версия: 2.0 с веб-панелью
"""

import asyncio
import sqlite3
import os
import logging
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from telethon import TelegramClient, events
from telethon.tl.types import User, UserProfilePhoto
from telethon.tl.functions.photos import GetUserPhotosRequest
import aiohttp
from aiohttp import web
import html

# === НАСТРОЙКА ЛОГОВ ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === ПЕРЕМЕННЫЕ ИЗ ОКРУЖЕНИЯ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
WEB_PORT = int(os.getenv("PORT", 8080))

# Проверка обязательных переменных
if not BOT_TOKEN or not API_ID or not API_HASH or not OWNER_ID:
    logger.error("❌ Ошибка: не все переменные окружения заданы!")
    logger.error("Нужны: BOT_TOKEN, API_ID, API_HASH, OWNER_ID")
    exit(1)

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
telethon_client = TelegramClient('stalker_session', API_ID, API_HASH)

# === БАЗА ДАННЫХ ===
DB_PATH = "stalker.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Таблица целей
    cur.execute('''
        CREATE TABLE IF NOT EXISTS targets (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            phone TEXT,
            photo_hash TEXT,
            bio TEXT,
            is_bot BOOLEAN,
            added_at TIMESTAMP,
            last_seen TIMESTAMP
        )
    ''')
    
    # Таблица истории
    cur.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            changed_at TIMESTAMP
        )
    ''')
    
    # Таблица скриншотов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS screenshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            photo_url TEXT,
            captured_at TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

# === РАБОТА С БАЗОЙ ===
def add_target(user_id, username, first_name, last_name, phone="", bio="", is_bot=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT OR REPLACE INTO targets 
        (user_id, username, first_name, last_name, phone, bio, is_bot, added_at, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, username, first_name, last_name, phone, bio, is_bot, datetime.now(), datetime.now()))
    conn.commit()
    conn.close()
    logger.info(f"✅ Добавлена цель: {user_id} (@{username})")

def get_targets():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, username, first_name, last_name, photo_hash, bio, last_seen FROM targets ORDER BY added_at DESC')
    rows = cur.fetchall()
    conn.close()
    return rows

def get_target(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT * FROM targets WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def remove_target(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('DELETE FROM targets WHERE user_id = ?', (user_id,))
    cur.execute('DELETE FROM history WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()
    logger.info(f"🗑️ Удалена цель: {user_id}")

def log_change(user_id, field, old_val, new_val):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO history (user_id, field, old_value, new_value, changed_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, field, str(old_val)[:500], str(new_val)[:500], datetime.now()))
    conn.commit()
    conn.close()

def get_history(user_id, limit=100):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT field, old_value, new_value, changed_at 
        FROM history 
        WHERE user_id = ? 
        ORDER BY changed_at DESC 
        LIMIT ?
    ''', (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def add_screenshot(user_id, photo_url):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO screenshots (user_id, photo_url, captured_at)
        VALUES (?, ?, ?)
    ''', (user_id, photo_url, datetime.now()))
    conn.commit()
    conn.close()

def get_screenshots(user_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT photo_url, captured_at FROM screenshots
        WHERE user_id = ? ORDER BY captured_at DESC LIMIT ?
    ''', (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def update_last_seen(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('UPDATE targets SET last_seen = ? WHERE user_id = ?', (datetime.now(), user_id))
    conn.commit()
    conn.close()

# === КОМАНДЫ БОТА ===
@dp.message(Command("start"))
async def start_cmd(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Веб-панель", url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'localhost')}/")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")]
    ])
    
    await message.answer(
        "👁️ **Бот-сталкер** активирован.\n\n"
        "Отслеживает всё: имя, юзернейм, фото, био, онлайн.\n"
        "📸 Делает скриншоты профиля при каждом изменении.\n"
        "🌐 Веб-панель для просмотра истории.\n\n"
        "**Команды:**\n"
        "/add @username — добавить цель\n"
        "/list — список целей\n"
        "/remove @username — удалить цель\n"
        "/history @username — история изменений\n"
        "/stats @username — полная информация\n"
        "/screenshots @username — скриншоты профиля\n"
        "/clear_history @username — очистить историю\n"
        "/export @username — экспорт в JSON",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

@dp.callback_query(lambda c: c.data == "help")
async def help_callback(callback):
    await callback.message.answer(
        "📖 **Инструкция:**\n\n"
        "1. Добавьте пользователя через /add\n"
        "2. Бот будет отслеживать все изменения\n"
        "3. При каждом изменении делается скриншот профиля\n"
        "4. История доступна в веб-панели\n"
        "5. Данные хранятся 30 дней\n\n"
        "🔒 Все данные приватны, только для владельца бота.",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(Command("add"))
async def add_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажи юзернейм: `/add @username`", parse_mode="Markdown")
        return
    
    username = args[1].replace('@', '')
    try:
        entity = await telethon_client.get_entity(username)
        if not isinstance(entity, User):
            await message.answer("❌ Это не пользователь, а группа/канал.")
            return
        
        # Получаем полную информацию
        full = await telethon_client.get_full_user(entity)
        bio = full.about or ""
        
        # Получаем фото
        photo_hash = ""
        if entity.photo:
            photo_hash = str(entity.photo.photo_id) if hasattr(entity.photo, 'photo_id') else ""
        
        add_target(
            entity.id,
            entity.username or "",
            entity.first_name or "",
            entity.last_name or "",
            getattr(entity, 'phone', ''),
            bio,
            entity.bot or False
        )
        
        # Делаем первый скриншот
        if entity.photo:
            try:
                photos = await telethon_client(GetUserPhotosRequest(entity.id, offset=0, max_id=0, limit=1))
                if photos.count > 0:
                    photo = photos.photos[0]
                    photo_url = f"https://t.me/userpic/{entity.id}/{photo.id}.jpg"
                    add_screenshot(entity.id, photo_url)
            except:
                pass
        
        await message.answer(
            f"✅ **@{username}** добавлен в список отслеживания.\n"
            f"🆔 ID: `{entity.id}`\n"
            f"📸 Сделан скриншот профиля",
            parse_mode="Markdown"
        )
        
        # Уведомление владельцу
        await bot.send_message(
            OWNER_ID,
            f"🎯 Новая цель: @{username} (ID: {entity.id})\nДобавил: @{message.from_user.username}"
        )
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("list"))
async def list_cmd(message: Message):
    targets = get_targets()
    
    if not targets:
        await message.answer("📭 Список целей пуст.")
        return
    
    text = "📋 **Список целей:**\n\n"
    for user_id, username, first_name, last_name, photo_hash, bio, last_seen in targets:
        # Проверяем онлайн
        is_online = await check_online(user_id)
        status = "🟢" if is_online else "⚪"
        
        # Время последнего изменения
        last_seen_dt = datetime.fromisoformat(last_seen) if last_seen else None
        time_ago = ""
        if last_seen_dt:
            diff = datetime.now() - last_seen_dt
            if diff.days > 0:
                time_ago = f"{diff.days}д назад"
            elif diff.seconds > 3600:
                time_ago = f"{diff.seconds // 3600}ч назад"
            else:
                time_ago = f"{diff.seconds // 60}м назад"
        
        text += f"{status} **@{username or 'нет'}** — {first_name or 'без имени'}\n"
        text += f"   🆔 `{user_id}` | 📅 {time_ago}\n\n"
    
    await message.answer(text[:4000], parse_mode="Markdown")

@dp.message(Command("remove"))
async def remove_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажи юзернейм: `/remove @username`", parse_mode="Markdown")
        return
    
    username = args[1].replace('@', '')
    try:
        entity = await telethon_client.get_entity(username)
        remove_target(entity.id)
        await message.answer(f"✅ @{username} удалён из списка.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("history"))
async def history_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажи юзернейм: `/history @username`", parse_mode="Markdown")
        return
    
    username = args[1].replace('@', '')
    try:
        entity = await telethon_client.get_entity(username)
        history = get_history(entity.id)
        
        if not history:
            await message.answer(f"📭 Нет истории для @{username}.")
            return
        
        text = f"📜 **История @{username}:**\n\n"
        for field, old_val, new_val, changed_at in history[:20]:
            dt = datetime.fromisoformat(changed_at).strftime("%d.%m %H:%M")
            text += f"🔹 **{field}**: `{old_val}` → `{new_val}`\n   📅 {dt}\n\n"
        
        await message.answer(text[:4000], parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажи юзернейм: `/stats @username`", parse_mode="Markdown")
        return
    
    username = args[1].replace('@', '')
    try:
        entity = await telethon_client.get_entity(username)
        full = await telethon_client.get_full_user(entity)
        
        text = f"📊 **Статистика @{username}:**\n\n"
        text += f"🆔 ID: `{entity.id}`\n"
        text += f"👤 Имя: {entity.first_name or '—'}\n"
        text += f"📛 Фамилия: {entity.last_name or '—'}\n"
        text += f"🔖 Юзернейм: @{entity.username or '—'}\n"
        text += f"📱 Телефон: {getattr(entity, 'phone', '—')}\n"
        text += f"🤖 Бот: {'Да' if entity.bot else 'Нет'}\n"
        text += f"📝 Био: {full.about or '—'}\n"
        text += f"🖼️ Фото: {'Есть' if entity.photo else 'Нет'}\n"
        text += f"🟢 Онлайн: {'Да' if await check_online(entity.id) else 'Нет'}\n"
        
        # Количество изменений
        history_count = len(get_history(entity.id))
        text += f"📜 Изменений: {history_count}\n"
        
        await message.answer(text[:4000], parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("screenshots"))
async def screenshots_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажи юзернейм: `/screenshots @username`", parse_mode="Markdown")
        return
    
    username = args[1].replace('@', '')
    try:
        entity = await telethon_client.get_entity(username)
        screenshots = get_screenshots(entity.id)
        
        if not screenshots:
            await message.answer(f"📸 Нет скриншотов для @{username}.")
            return
        
        text = f"📸 **Скриншоты @{username}:**\n\n"
        for url, captured_at in screenshots[:5]:
            dt = datetime.fromisoformat(captured_at).strftime("%d.%m %H:%M")
            text += f"📅 {dt}: [Фото]({url})\n"
        
        await message.answer(text, parse_mode="Markdown", disable_web_page_preview=False)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("clear_history"))
async def clear_history_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажи юзернейм: `/clear_history @username`", parse_mode="Markdown")
        return
    
    username = args[1].replace('@', '')
    try:
        entity = await telethon_client.get_entity(username)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('DELETE FROM history WHERE user_id = ?', (entity.id,))
        cur.execute('DELETE FROM screenshots WHERE user_id = ?', (entity.id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ История и скриншоты @{username} очищены.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("export"))
async def export_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажи юзернейм: `/export @username`", parse_mode="Markdown")
        return
    
    username = args[1].replace('@', '')
    try:
        entity = await telethon_client.get_entity(username)
        
        # Собираем данные
        target = get_target(entity.id)
        history = get_history(entity.id, limit=1000)
        screenshots = get_screenshots(entity.id)
        
        data = {
            "user": {
                "id": entity.id,
                "username": entity.username,
                "first_name": entity.first_name,
                "last_name": entity.last_name,
                "bio": target[6] if target else "",
                "added_at": target[8] if target else "",
            },
            "history": [
                {"field": h[0], "old": h[1], "new": h[2], "time": h[3]}
                for h in history
            ],
            "screenshots": [
                {"url": s[0], "time": s[1]}
                for s in screenshots
            ]
        }
        
        # Отправляем JSON файлом
        json_str = json.dumps(data, indent=2, ensure_ascii=False)
        
        # Создаём файл в памяти и отправляем
        from io import BytesIO
        file = BytesIO(json_str.encode('utf-8'))
        file.name = f"stalker_{username}_{datetime.now().strftime('%Y%m%d')}.json"
        
        await message.answer_document(
            types.BufferedInputFile(file.getvalue(), filename=file.name),
            caption=f"📦 Экспорт данных @{username}"
        )
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# === ФУНКЦИЯ ПРОВЕРКИ ОНЛАЙН ===
async def check_online(user_id):
    try:
        entity = await telethon_client.get_entity(user_id)
        full = await telethon_client.get_full_user(entity)
        # Проверяем статус
        if full.status:
            status_type = type(full.status).__name__
            if status_type == 'UserStatusOnline':
                return True
            elif status_type == 'UserStatusRecently':
                return True
        return False
    except:
        return False

# === ВЕБ-ПАНЕЛЬ ===
async def web_index(request):
    """Главная страница веб-панели"""
    targets = get_targets()
    
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Stalker Bot - Панель управления</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #0a0a0a;
                color: #fff;
                padding: 20px;
            }
            .container { max-width: 1200px; margin: 0 auto; }
            h1 {
                font-size: 2.5em;
                margin-bottom: 10px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }
            .subtitle { color: #888; margin-bottom: 30px; }
            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
                gap: 20px;
            }
            .card {
                background: #1a1a1a;
                border-radius: 12px;
                padding: 20px;
                border: 1px solid #2a2a2a;
                transition: all 0.3s;
            }
            .card:hover {
                border-color: #667eea;
                transform: translateY(-2px);
            }
            .card-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 10px;
            }
            .username {
                font-size: 1.2em;
                font-weight: bold;
                color: #667eea;
            }
            .name { color: #ccc; font-size: 0.9em; }
            .status {
                display: inline-block;
                width: 10px;
                height: 10px;
                border-radius: 50%;
                margin-right: 8px;
            }
            .status.online { background: #4ade80; }
            .status.offline { background: #6b7280; }
            .info {
                color: #888;
                font-size: 0.8em;
                margin-top: 10px;
                padding-top: 10px;
                border-top: 1px solid #2a2a2a;
            }
            .badge {
                background: #2a2a2a;
                padding: 2px 10px;
                border-radius: 20px;
                font-size: 0.7em;
                color: #888;
            }
            .empty {
                text-align: center;
                padding: 60px 20px;
                color: #666;
            }
            .empty h2 { color: #888; margin-bottom: 10px; }
            .refresh-btn {
                background: #667eea;
                border: none;
                color: #fff;
                padding: 10px 20px;
               