import asyncio
import sqlite3
import os
import logging
import json
import re
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from telethon import TelegramClient
from telethon.tl.functions.photos import GetUserPhotosRequest
from telethon.tl.functions.users import GetFullUserRequest
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

def escape_markdown(text):
    """Экранирует специальные символы для Markdown"""
    if not text:
        return ""
    text = str(text)
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

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
            last_seen TIMESTAMP,
            last_report TIMESTAMP
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
    cur.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            report_text TEXT,
            created_at TIMESTAMP
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
        (user_id, username, first_name, last_name, phone, bio, is_bot, added_at, last_seen, last_report)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, username, first_name, last_name, phone, bio, is_bot, datetime.now(), datetime.now(), datetime.now()))
    conn.commit()
    conn.close()

def get_targets():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, username, first_name, last_name, photo_hash, bio, last_seen, last_report FROM targets ORDER BY added_at DESC')
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
    cur.execute('DELETE FROM reports WHERE user_id = ?', (user_id,))
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

def update_last_report(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('UPDATE targets SET last_report = ? WHERE user_id = ?', (datetime.now(), user_id))
    conn.commit()
    conn.close()

def save_report(user_id, report_text):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO reports (user_id, report_text, created_at)
        VALUES (?, ?, ?)
    ''', (user_id, report_text, datetime.now()))
    conn.commit()
    conn.close()

def get_last_report(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT report_text, created_at FROM reports
        WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
    ''', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

async def download_photo(user_id):
    """Скачивает фото профиля и сохраняет в файл"""
    try:
        # Получаем фото пользователя
        photos = await telethon_client(GetUserPhotosRequest(user_id, offset=0, max_id=0, limit=1))
        if photos and len(photos.photos) > 0:
            photo = photos.photos[0]
            
            # Создаём папку если её нет
            os.makedirs('screenshots', exist_ok=True)
            
            # Формируем путь к файлу
            file_path = f"screenshots/{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            
            # Скачиваем фото
            await telethon_client.download_media(photo, file=file_path)
            
            # Проверяем, что файл создался
            if os.path.exists(file_path):
                add_screenshot(user_id, file_path)
                logger.info(f"✅ Фото сохранено: {file_path}")
                return file_path
            else:
                logger.error(f"❌ Файл не создался: {file_path}")
        else:
            logger.info(f"ℹ️ Нет фото для пользователя {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка при скачивании фото для {user_id}: {e}")
    return None
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
        "🌐 Веб-панель для просмотра истории.\n"
        "📊 Автоматические отчёты каждые 24 часа.\n\n"
        "<b>Команды:</b>\n"
        "/add @username — добавить цель\n"
        "/list — список целей\n"
        "/remove @username — удалить цель\n"
        "/history @username — история изменений\n"
        "/stats @username — полная информация\n"
        "/screenshots @username — скриншоты профиля\n"
        "/clear_history @username — очистить историю\n"
        "/export @username — экспорт в JSON\n"
        "/report @username — получить последний отчёт\n"
        "/report_all — получить отчёты по всем целям",
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
        "4. Каждые 24 часа отправляется подробный отчёт\n"
        "5. История доступна в веб-панели\n"
        "6. Данные хранятся 30 дней\n\n"
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
    
    identifier = args[1].replace('@', '')
    try:
        try:
            user_id = int(identifier)
            entity = await telethon_client.get_entity(user_id)
        except ValueError:
            entity = await telethon_client.get_entity(identifier)
        
        if not isinstance(entity, User):
            await message.answer("❌ Это не пользователь, а группа/канал.")
            return
        
        try:
            full_user = await telethon_client(GetFullUserRequest(entity.id))
            bio = full_user.full_user.about or ""
        except:
            bio = ""
        
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
        
        photo_path = await download_photo(entity.id)
        
        report = await generate_report(entity.id)
        if report:
            save_report(entity.id, report)
            await bot.send_message(OWNER_ID, report)
        
        await message.answer(
            f"✅ **Пользователь добавлен** в список отслеживания.\n"
            f"🆔 ID: `{entity.id}`\n"
            f"👤 Имя: {escape_markdown(entity.first_name or 'Без имени')}\n"
            f"🔖 Юзернейм: @{entity.username or 'нет'}\n"
            f"📝 Био: {escape_markdown(bio[:50]) or 'нет'}\n"
            f"📸 Сделан скриншот профиля\n"
            f"📊 Первый отчёт отправлен владельцу",
            parse_mode="Markdown"
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
    for user_id, username, first_name, last_name, photo_hash, bio, last_seen, last_report in targets:
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
        
        last_report_dt = datetime.fromisoformat(last_report) if last_report else None
        report_status = "✅" if last_report_dt and (datetime.now() - last_report_dt).days < 1 else "⏳"
        
        text += f"{status} **@{escape_markdown(username or 'нет')}** — {escape_markdown(first_name or 'без имени')}\n"
        text += f"   🆔 `{user_id}` | 📅 {time_ago} | 📊 {report_status}\n\n"
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
            old_val_escaped = escape_markdown(str(old_val)[:100])
            new_val_escaped = escape_markdown(str(new_val)[:100])
            text += f"🔹 **{field}**: `{old_val_escaped}` → `{new_val_escaped}`\n   📅 {dt}\n\n"
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
        
        try:
            full_user = await telethon_client(GetFullUserRequest(entity.id))
            bio = full_user.full_user.about or ""
        except:
            bio = "Недоступно"
        
        is_online = await check_online(entity.id)
        
        text = f"📊 **Статистика @{username}:**\n\n"
        text += f"🆔 ID: `{entity.id}`\n"
        text += f"👤 Имя: {escape_markdown(entity.first_name or '—')}\n"
        text += f"📛 Фамилия: {escape_markdown(entity.last_name or '—')}\n"
        text += f"🔖 Юзернейм: @{escape_markdown(entity.username or '—')}\n"
        text += f"📱 Телефон: {escape_markdown(getattr(entity, 'phone', '—'))}\n"
        text += f"🤖 Бот: {'Да' if entity.bot else 'Нет'}\n"
        text += f"📝 Био: {escape_markdown(bio[:200]) or '—'}\n"
        text += f"🖼️ Фото: {'Есть' if entity.photo else 'Нет'}\n"
        text += f"🟢 Онлайн: {'Да' if is_online else 'Нет'}\n"
        
        history = get_history(entity.id)
        text += f"📜 Изменений: {len(history)}\n"
        
        last_report = get_last_report(entity.id)
        if last_report:
            text += f"\n📊 Последний отчёт: {escape_markdown(last_report[0][:50])}..."
        
        await message.answer(text[:4000], parse_mode="Markdown")
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("report"))
async def report_cmd(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажи юзернейм: `/report @username`", parse_mode="Markdown")
        return
    username = args[1].replace('@', '')
    try:
        entity = await telethon_client.get_entity(username)
        last_report = get_last_report(entity.id)
        if not last_report:
            report = await generate_report(entity.id)
            if report:
                save_report(entity.id, report)
                await message.answer(report, parse_mode="Markdown")
            else:
                await message.answer("❌ Не удалось сгенерировать отчёт.")
        else:
            await message.answer(
                f"📊 **Последний отчёт для @{username}:**\n\n{last_report[0]}",
                parse_mode="Markdown"
            )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("report_all"))
async def report_all_cmd(message: Message):
    targets = get_targets()
    if not targets:
        await message.answer("📭 Нет целей для отчётов.")
        return
    
    await message.answer("📊 **Генерация отчётов по всем целям...**")
    
    for user_id, username, first_name, last_name, photo_hash, bio, last_seen, last_report in targets:
        try:
            report = await generate_report(user_id)
            if report:
                save_report(user_id, report)
                await bot.send_message(
                    OWNER_ID,
                    f"📊 **Отчёт для @{username or first_name}:**\n\n{report}",
                    parse_mode="Markdown"
                )
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка при генерации отчёта для {user_id}: {e}")
    
    await message.answer("✅ Отчёты сгенерированы и отправлены!")

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
        sent_count = 0
        for url, captured_at in screenshots[:5]:
            dt = datetime.fromisoformat(captured_at).strftime("%d.%m %H:%M")
            if url.startswith('screenshots/') and os.path.exists(url):
                try:
                    with open(url, 'rb') as photo_file:
                        await message.answer_photo(
                            types.BufferedInputFile(photo_file.read(), filename=os.path.basename(url)),
                            caption=f"📅 {dt}"
                        )
                        sent_count += 1
                except Exception as e:
                    logger.error(f"Ошибка при отправке фото {url}: {e}")
                    text += f"📅 {dt}: [Файл не найден]\n"
            else:
                text += f"📅 {dt}: [Фото]({url})\n"
        
        if text and not sent_count:
            await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
        elif not sent_count:
            await message.answer("❌ Нет доступных скриншотов для отображения.")
        
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
        cur.execute('DELETE FROM reports WHERE user_id = ?', (entity.id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ История, скриншоты и отчёты @{username} очищены.")
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
        reports = []
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT report_text, created_at FROM reports WHERE user_id = ? ORDER BY created_at DESC', (entity.id,))
        reports = cur.fetchall()
        conn.close()
        
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
            ],
            "reports": [
                {"text": r[0], "time": r[1]}
                for r in reports
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
async def generate_report(user_id):
    """Генерирует подробный отчёт о пользователе"""
    try:
        entity = await telethon_client.get_entity(user_id)
        target = get_target(user_id)
        
        if not target:
            return None
        
        try:
            full_user = await telethon_client(GetFullUserRequest(user_id))
            bio = full_user.full_user.about or ""
        except:
            bio = "Недоступно"
        
        is_online = await check_online(user_id)
        history = get_history(user_id, limit=50)
        screenshots = get_screenshots(user_id, limit=3)
        
        day_ago = datetime.now() - timedelta(days=1)
        recent_changes = 0
        for h in history:
            try:
                h_time = datetime.fromisoformat(h[3])
                if h_time > day_ago:
                    recent_changes += 1
            except:
                pass
        
        report = f"📊 **Отчёт о пользователе**\n"
        report += f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        report += f"👤 **{escape_markdown(entity.first_name or 'Без имени')}**\n"
        report += f"🔖 @{escape_markdown(entity.username or 'нет')}\n"
        report += f"🆔 `{entity.id}`\n\n"
        
        report += f"**Статус:** {'🟢 Онлайн' if is_online else '⚪ Офлайн'}\n"
        report += f"**Био:** {escape_markdown(bio[:200]) or '—'}\n"
        report += f"**Фото:** {'✅ Есть' if entity.photo else '❌ Нет'}\n\n"
        
        report += f"**📊 Статистика:**\n"
        report += f"• Всего изменений: {len(history)}\n"
        report += f"• За последние 24ч: {recent_changes}\n"
        report += f"• Скриншотов: {len(screenshots)}\n"
        
        if history:
            report += f"\n**🔹 Последние изменения:**\n"
            for field, old_val, new_val, changed_at in history[:3]:
                dt = datetime.fromisoformat(changed_at).strftime("%d.%m %H:%M")
                old_escaped = escape_markdown(str(old_val)[:50])
                new_escaped = escape_markdown(str(new_val)[:50])
                report += f"• {field}: `{old_escaped}` → `{new_escaped}` ({dt})\n"
        
        return report
        
    except Exception as e:
        logger.error(f"Ошибка при генерации отчёта для {user_id}: {e}")
        return None

async def check_online(user_id):
    try:
        entity = await telethon_client.get_entity(user_id)
        full = await telethon_client.get_full_user(entity)
        
        if full and full.status:
            status_type = type(full.status).__name__
            logger.info(f"Статус пользователя {user_id}: {status_type}")
            
            if status_type == 'UserStatusOnline':
                return True
            elif status_type == 'UserStatusRecently':
                return True
            elif status_type == 'UserStatusLastWeek':
                return False
            elif status_type == 'UserStatusLastMonth':
                return False
            else:
                return False
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки онлайн для {user_id}: {e}")
        return False

async def report_loop():
    """Цикл отправки отчётов каждые 24 часа"""
    while True:
        try:
            targets = get_targets()
            logger.info(f"📊 Генерация ежедневных отчётов для {len(targets)} целей...")
            
            for user_id, username, first_name, last_name, photo_hash, bio, last_seen, last_report in targets:
                if last_report:
                    last_report_dt = datetime.fromisoformat(last_report)
                    if (datetime.now() - last_report_dt).days < 1:
                        continue
                
                try:
                    report = await generate_report(user_id)
                    if report:
                        save_report(user_id, report)
                        await bot.send_message(
                            OWNER_ID,
                            f"📊 **Ежедневный отчёт для @{username or first_name}:**\n\n{report}",
                            parse_mode="Markdown"
                        )
                        update_last_report(user_id)
                        logger.info(f"✅ Отчёт отправлен для {user_id}")
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Ошибка при отправке отчёта для {user_id}: {e}")
            
            logger.info("✅ Ежедневные отчёты отправлены")
            
        except Exception as e:
            logger.error(f"Ошибка в цикле отчётов: {e}")
        
        await asyncio.sleep(21600)

async def stalker_loop():
    while True:
        try:
            targets = get_targets()
            logger.info(f"🔍 Проверка {len(targets)} целей...")
            
            for user_id, username, first_name, last_name, saved_photo_hash, saved_bio, _, _ in targets:
                try:
                    # Получаем актуальные данные
                    entity = await telethon_client.get_entity(user_id)
                    
                    # Получаем био
                    try:
                        full_user = await telethon_client(GetFullUserRequest(user_id))
                        new_bio = full_user.full_user.about or ""
                    except Exception as e:
                        logger.error(f"Ошибка получения био для {user_id}: {e}")
                        new_bio = saved_bio  # оставляем старое
                    
                    # Проверяем изменения
                    changes = []
                    photo_changed = False
                    
                    # Проверка юзернейма
                    new_username = entity.username or ""
                    if new_username != username:
                        changes.append(('username', username, new_username))
                        log_change(user_id, 'username', username, new_username)
                        await bot.send_message(OWNER_ID, f"🔄 @{username} сменил юзернейм на @{new_username}")
                        photo_changed = True
                    
                    # Проверка имени
                    new_first = entity.first_name or ""
                    if new_first != first_name:
                        changes.append(('first_name', first_name, new_first))
                        log_change(user_id, 'first_name', first_name, new_first)
                        await bot.send_message(OWNER_ID, f"🔄 {first_name} сменил имя на {new_first}")
                        photo_changed = True
                    
                    # Проверка био - ВАЖНО!
                    if new_bio != saved_bio:
                        changes.append(('bio', saved_bio, new_bio))
                        log_change(user_id, 'bio', saved_bio, new_bio)
                        await bot.send_message(OWNER_ID, f"📝 {first_name} изменил био: {new_bio[:50]}...")
                        photo_changed = True
                    
                    # Проверка фото
                    photo_hash = str(entity.photo) if entity.photo else ""
                    if photo_hash != saved_photo_hash:
                        changes.append(('photo', saved_photo_hash, photo_hash))
                        log_change(user_id, 'photo', saved_photo_hash, photo_hash)
                        await bot.send_message(OWNER_ID, f"🖼️ {first_name} сменил аватарку")
                        photo_changed = True
                        
                        # Скачиваем новое фото
                        await download_photo(user_id)
                    
                    # Если есть изменения — обновляем БД
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
                        logger.info(f"✅ Обновлены данные для {user_id}: {len(changes)} изменений")
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка при проверке {user_id}: {e}")
                    
        except Exception as e:
            logger.error(f"❌ Ошибка в stalker-цикле: {e}")
            
        await asyncio.sleep(30)  # Проверка каждые 30 секунд
async def web_index(request):
    """Главная страница веб-панели"""
    targets = get_targets()
    
    html = """
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
            margin-bottom: 20px;
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
        .user-avatar {
            width: 48px;
            height: 48px;
            border-radius: 50%;
            object-fit: cover;
            background: #2a2a2a;
        }
        .status-online { color: #4ade80; }
        .status-offline { color: #6b7280; }
        .text-gray { color: #888; }
        .text-sm { font-size: 0.9em; }
        .flex { display: flex; }
        .items-center { align-items: center; }
        .gap-3 { gap: 12px; }
        .mt-2 { margin-top: 8px; }
        .truncate { 
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .badge {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 20px;
            font-size: 0.7em;
        }
        .badge-online { background: #4ade80; color: #000; }
        .badge-offline { background: #6b7280; color: #fff; }
    </style>
</head>
<body>
    <div class="container">
        <h1>👁️ Stalker Bot</h1>
        <p class="subtitle">Панель отслеживания пользователей</p>
        <div class="grid">
"""
    
    if not targets:
        html += """
            <div class="card" style="grid-column: 1/-1; text-align: center; padding: 40px;">
                <p style="color: #666;">📭 Нет целей для отслеживания</p>
                <p style="color: #444; font-size: 0.9em; margin-top: 8px;">
                    Добавьте пользователя через команду /add в Telegram
                </p>
            </div>
"""
    else:
        for user_id, username, first_name, last_name, photo_hash, bio, last_seen, last_report in targets:
            is_online = False
            try:
                is_online = await check_online(user_id)
            except:
                pass
            
            status_class = "badge-online" if is_online else "badge-offline"
            status_text = "🟢 Онлайн" if is_online else "⚪ Офлайн"
            
            # Ищем аватарку
            avatar = "/static/default-avatar.png"
            if os.path.exists('screenshots'):
                for file in os.listdir('screenshots'):
                    if file.startswith(str(user_id)):
                        avatar = f"/static/{file}"
                        break
            
            html += f"""
            <div class="card">
                <div class="flex items-center gap-3">
                    <img src="{avatar}" alt="avatar" class="user-avatar" onerror="this.src='/static/default-avatar.png'">
                    <div style="flex: 1; min-width: 0;">
                        <div style="font-weight: bold; truncate;">{first_name or 'Без имени'}</div>
                        <div class="text-gray text-sm truncate">@{username or 'нет'}</div>
                    </div>
                    <span class="badge {status_class}">{status_text}</span>
                </div>
                <div class="text-sm text-gray mt-2">
                    <div class="truncate">📝 {bio[:80] or 'Нет био'}</div>
                    <div style="font-size: 0.8em; margin-top: 4px; color: #555;">
                        🆔 {user_id} | 📅 {last_seen or 'никогда'}
                    </div>
                </div>
            </div>
"""
    
    html += """
        </div>
    </div>
</body>
</html>
"""
    return web.Response(text=html, content_type='text/html')

async def web_api_targets(request):
    """API для получения списка целей"""
    conn = get_db_connection()
    targets = conn.execute('''
        SELECT user_id, username, first_name, last_name, photo_hash, bio, last_seen, added_at
        FROM targets ORDER BY added_at DESC
    ''').fetchall()
    conn.close()
    
    result = []
    online_count = 0
    
    for target in targets:
        # Проверяем онлайн
        is_online = False
        try:
            # Вызываем функцию проверки из bot.py
            from bot import check_online
            is_online = await check_online(target['user_id'])
            if is_online:
                online_count += 1
        except:
            pass
        
        # Ищем аватарку
        avatar = None
        if os.path.exists('screenshots'):
            # Ищем самый свежий скриншот для этого пользователя
            screenshots_dir = 'screenshots'
            user_files = [f for f in os.listdir(screenshots_dir) if f.startswith(str(target['user_id']))]
            if user_files:
                # Сортируем по времени создания (берём самый новый)
                user_files.sort(reverse=True)
                avatar = f"/static/{user_files[0]}"
        
        result.append({
            'user_id': target['user_id'],
            'username': target['username'],
            'first_name': target['first_name'],
            'last_name': target['last_name'],
            'bio': target['bio'] or '',
            'avatar': avatar,
            'online': is_online,
            'last_seen': target['last_seen'],
            'added_at': target['added_at']
        })
    
    conn = get_db_connection()
    total_changes = conn.execute('SELECT COUNT(*) FROM history').fetchone()[0]
    total_screenshots = conn.execute('SELECT COUNT(*) FROM screenshots').fetchone()[0]
    conn.close()
    
    return web.json_response({
        'targets': result,
        'stats': {
            'total_targets': len(result),
            'online_targets': online_count,
            'total_changes': total_changes,
            'total_screenshots': total_screenshots
        }
    })

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
    asyncio.create_task(report_loop())
    
    logger.info("🤖 Бот запущен")
    await bot.delete_webhook()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
