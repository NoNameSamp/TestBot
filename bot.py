import asyncio
import sqlite3
import os
import logging
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from telethon import TelegramClient
from telethon.tl.functions.photos import GetUserPhotosRequest
from telethon.tl.types import User
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
WEB_PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN or not API_ID or not API_HASH or not OWNER_ID:
    logger.error("❌ Ошибка: не все переменные окружения заданы!")
    exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
telethon_client = TelegramClient('stalker_session', API_ID, API_HASH)

DB_PATH = "stalker.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
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

@dp.message(Command("start"))
async def start_cmd(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Веб-панель", url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'localhost')}/")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")]
    ])
    await message.answer(
        "<b>👁️ Бот-сталкер активирован.</b>\n\n"
        "Отслеживает всё: имя, юзернейм, фото, био, онлайн.\n"
        "📸 Делает скриншоты профиля при каждом изменении.\n"
        "🌐 Веб-панель для просмотра истории.\n\n"
        "<b>Команды:</b>\n"
        "/add @username — добавить цель\n"
        "/list — список целей\n"
        "/remove @username — удалить цель\n"
        "/history @username — история изменений\n"
        "/stats @username — полная информация\n"
        "/screenshots @username — скриншоты профиля\n"
        "/clear_history @username — очистить историю\n"
        "/export @username — экспорт в JSON",
        parse_mode="HTML",
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
        await message.answer("❌ Укажи юзернейм или ID: `/add @username` или `/add 123456789`", parse_mode="Markdown")
        return
    
    identifier = args[1].replace('@', '')
    try:
        # Пытаемся преобразовать в число (если это ID)
        try:
            user_id = int(identifier)
            entity = await telethon_client.get_entity(user_id)
        except ValueError:
            # Если не число — ищем по юзернейму
            entity = await telethon_client.get_entity(identifier)
        
        if not isinstance(entity, User):
            await message.answer("❌ Это не пользователь, а группа/канал.")
            return
        
        # Получаем полную информацию
        full = await telethon_client.get_full_entity(entity)
        bio = full.about or ""
        
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
            f"✅ **Пользователь добавлен** в список отслеживания.\n"
            f"🆔 ID: `{entity.id}`\n"
            f"👤 Имя: {entity.first_name or 'Без имени'}\n"
            f"🔖 Юзернейм: @{entity.username or 'нет'}\n"
            f"📸 Сделан скриншот профиля",
            parse_mode="Markdown"
        )
        
        await bot.send_message(
            OWNER_ID,
            f"🎯 Новая цель: @{entity.username or entity.first_name} (ID: {entity.id})\nДобавил: @{message.from_user.username}"
        )
        
    except ValueError:
        await message.answer("❌ Неверный ID. Используй цифры без пробелов: `/add 123456789`", parse_mode="Markdown")
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
        is_online = await check_online(user_id)
        status = "🟢" if is_online else "⚪"
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
        json_str = json.dumps(data, indent=2, ensure_ascii=False)
        from io import BytesIO
        file = BytesIO(json_str.encode('utf-8'))
        file.name = f"stalker_{username}_{datetime.now().strftime('%Y%m%d')}.json"
        await message.answer_document(
            types.BufferedInputFile(file.getvalue(), filename=file.name),
            caption=f"📦 Экспорт данных @{username}"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

async def check_online(user_id):
    try:
        entity = await telethon_client.get_entity(user_id)
        full = await telethon_client.get_full_user(entity)
        if full.status:
            status_type = type(full.status).__name__
            if status_type == 'UserStatusOnline' or status_type == 'UserStatusRecently':
                return True
        return False
    except:
        return False

async def web_index(request):
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
            border-radius: 8px;
            cursor: pointer;
            margin-bottom: 20px;
            font-size: 1em;
        }
        .refresh-btn:hover { background: #5a6fd6; }
    </style>
</head>
<body>
    <div class="container">
        <h1>👁️ Stalker Bot</h1>
        <p class="subtitle">Панель отслеживания пользователей</p>
        <button class="refresh-btn" onclick="location.reload()">🔄 Обновить</button>
        <div class="grid">
"""
    if not targets:
        html_content += """
            <div class="empty" style="grid-column: 1/-1;">
                <h2>📭 Нет целей для отслеживания</h2>
                <p>Добавьте пользователя через команду /add в Telegram</p>
            </div>
"""
    else:
        for user_id, username, first_name, last_name, photo_hash, bio, last_seen in targets:
            is_online = await check_online(user_id)
            status_class = "online" if is_online else "offline"
            status_text = "🟢 Онлайн" if is_online else "⚪ Офлайн"
            html_content += f"""
        <div class="card">
            <div class="card-header">
                <div>
                    <div class="username">@{username or 'нет'}</div>
                    <div class="name">{first_name or 'Без имени'} {last_name or ''}</div>
                </div>
                <div>
                    <span class="status {status_class}"></span>
                    <span class="badge">{status_text}</span>
                </div>
            </div>
            <div class="info">
                <div>🆔 {user_id}</div>
                <div>📝 {bio[:100] or 'Нет био'}</div>
                <div style="margin-top: 5px; color: #555;">
                    📅 Обновлён: {last_seen or 'никогда'}
                </div>
            </div>
        </div>
"""
    html_content += """
        </div>
    </div>
</body>
</html>
"""
    return web.Response(text=html_content, content_type='text/html')

async def web_api_stats(request):
    targets = get_targets()
    data = []
    for user_id, username, first_name, last_name, photo_hash, bio, last_seen in targets:
        data.append({
            "id": user_id,
            "username": username,
            "name": f"{first_name} {last_name}".strip(),
            "bio": bio,
            "last_seen": last_seen,
            "online": await check_online(user_id)
        })
    return web.json_response(data)

def setup_web():
    app = web.Application()
    app.router.add_get('/', web_index)
    app.router.add_get('/api/stats', web_api_stats)
    return app

async def stalker_loop():
    while True:
        try:
            targets = get_targets()
            logger.info(f"🔍 Проверка {len(targets)} целей...")
            for user_id, username, first_name, last_name, saved_photo_hash, saved_bio, _ in targets:
                try:
                    entity = await telethon_client.get_entity(user_id)
                    full = await telethon_client.get_full_user(entity)
                    changes = []
                    photo_changed = False
                    new_username = entity.username or ""
                    if new_username != username:
                        changes.append(('username', username, new_username))
                        log_change(user_id, 'username', username, new_username)
                        await bot.send_message(OWNER_ID, f"🔄 @{username} сменил юзернейм на @{new_username}")
                        photo_changed = True
                    new_first = entity.first_name or ""
                    if new_first != first_name:
                        changes.append(('first_name', first_name, new_first))
                        log_change(user_id, 'first_name', first_name, new_first)
                        await bot.send_message(OWNER_ID, f"🔄 {first_name} сменил имя на {new_first}")
                        photo_changed = True
                    new_bio = full.about or ""
                    if new_bio != saved_bio:
                        changes.append(('bio', saved_bio, new_bio))
                        log_change(user_id, 'bio', saved_bio, new_bio)
                        await bot.send_message(OWNER_ID, f"📝 {first_name} изменил био")
                        photo_changed = True
                    photo_hash = str(entity.photo) if entity.photo else ""
                    if photo_hash != saved_photo_hash:
                        changes.append(('photo', saved_photo_hash, photo_hash))
                        log_change(user_id, 'photo', saved_photo_hash, photo_hash)
                        await bot.send_message(OWNER_ID, f"🖼️ {first_name} сменил аватарку")
                        photo_changed = True
                        try:
                            photos = await telethon_client(GetUserPhotosRequest(user_id, offset=0, max_id=0, limit=1))
                            if photos.count > 0:
                                photo_obj = photos.photos[0]
                                photo_url = f"https://t.me/userpic/{user_id}/{photo_obj.id}.jpg"
                                add_screenshot(user_id, photo_url)
                        except:
                            pass
                    if changes or photo_changed:
                        conn = sqlite3.connect(DB_PATH)
                        cur = conn.cursor()
                        cur.execute('''
                            UPDATE targets 
                            SET username = ?, first_name = ?, last_name = ?, 
                                photo_hash = ?, bio = ?, last_seen = ?
                            WHERE user_id = ?
                        ''', (new_username, new_first, entity.last_name or "", 
                              photo_hash, new_bio, datetime.now(), user_id))
                        conn.commit()
                        conn.close()
                        update_last_seen(user_id)
                except Exception as e:
                    logger.error(f"Ошибка при проверке {user_id}: {e}")
        except Exception as e:
            logger.error(f"Ошибка в стalking-цикле: {e}")
        await asyncio.sleep(10)

async def main():
    init_db()
    await telethon_client.start(bot_token=BOT_TOKEN)
    logger.info("✅ Telethon подключен через бота")
    web_app = setup_web()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WEB_PORT)
    await site.start()
    logger.info(f"🌐 Веб-панель запущена на порту {WEB_PORT}")
    asyncio.create_task(stalker_loop())
    logger.info("🤖 Бот запущен")
    
    # Удаляем вебхук перед запуском polling
    await bot.delete_webhook()
    logger.info("✅ Webhook удалён")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
