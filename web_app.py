# web_app.py
import os
import sqlite3
import json
from datetime import datetime
from aiohttp import web
from telethon import TelegramClient
from telethon.tl.functions.photos import GetUserPhotosRequest
from telethon.tl.functions.users import GetFullUserRequest

DB_PATH = "stalker.db"
telethon_client = None

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

async def web_dashboard(request):
    """Главная страница с панелью управления"""
    conn = get_db_connection()
    targets = conn.execute('''
        SELECT user_id, username, first_name, last_name, photo_hash, bio, last_seen, added_at
        FROM targets ORDER BY added_at DESC
    ''').fetchall()
    conn.close()
    
    html_content = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stalker Bot - Панель управления</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0a;
            color: #fff;
        }
        .sidebar {
            position: fixed;
            top: 0;
            left: 0;
            width: 250px;
            height: 100vh;
            background: #111;
            border-right: 1px solid #222;
            padding: 20px;
            overflow-y: auto;
            z-index: 100;
        }
        .main-content {
            margin-left: 250px;
            padding: 20px;
            min-height: 100vh;
        }
        .sidebar-logo {
            font-size: 1.5em;
            font-weight: bold;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            padding: 10px 0 20px 0;
            border-bottom: 1px solid #222;
            margin-bottom: 20px;
        }
        .nav-item {
            display: flex;
            align-items: center;
            padding: 12px 16px;
            color: #888;
            text-decoration: none;
            border-radius: 8px;
            margin-bottom: 4px;
            transition: all 0.3s;
            cursor: pointer;
        }
        .nav-item:hover {
            background: #1a1a1a;
            color: #fff;
        }
        .nav-item.active {
            background: #1a1a1a;
            color: #667eea;
        }
        .nav-item i {
            width: 24px;
            margin-right: 12px;
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
        }
        .user-card {
            background: #1a1a1a;
            border-radius: 12px;
            padding: 16px;
            border: 1px solid #2a2a2a;
            transition: all 0.3s;
            cursor: pointer;
        }
        .user-card:hover {
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
        .status-badge {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 20px;
            font-size: 0.7em;
        }
        .status-online { background: #4ade80; color: #000; }
        .status-offline { background: #6b7280; color: #fff; }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.active {
            display: flex;
        }
        .modal-content {
            background: #1a1a1a;
            border-radius: 16px;
            padding: 30px;
            max-width: 800px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
            border: 1px solid #2a2a2a;
        }
        .modal-close {
            float: right;
            background: none;
            border: none;
            color: #888;
            font-size: 1.5em;
            cursor: pointer;
        }
        .modal-close:hover { color: #fff; }
        .history-item {
            padding: 10px 0;
            border-bottom: 1px solid #2a2a2a;
        }
        .history-item:last-child { border-bottom: none; }
        .screenshot-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 12px;
        }
        .screenshot-grid img {
            width: 100%;
            height: 150px;
            object-fit: cover;
            border-radius: 8px;
            border: 1px solid #2a2a2a;
        }
        .report-text {
            background: #0a0a0a;
            padding: 16px;
            border-radius: 8px;
            white-space: pre-wrap;
            font-family: monospace;
            font-size: 0.9em;
        }
        .btn {
            padding: 8px 16px;
            border-radius: 8px;
            border: none;
            cursor: pointer;
            font-size: 0.9em;
            transition: all 0.3s;
        }
        .btn-primary { background: #667eea; color: #fff; }
        .btn-primary:hover { background: #5a6fd6; }
        .btn-danger { background: #ef4444; color: #fff; }
        .btn-danger:hover { background: #dc2626; }
        .btn-success { background: #22c55e; color: #fff; }
        .btn-success:hover { background: #16a34a; }
        .btn-sm { padding: 4px 12px; font-size: 0.8em; }
        .input-field {
            width: 100%;
            padding: 10px 14px;
            background: #0a0a0a;
            border: 1px solid #2a2a2a;
            border-radius: 8px;
            color: #fff;
            font-size: 1em;
        }
        .input-field:focus {
            outline: none;
            border-color: #667eea;
        }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media (max-width: 768px) {
            .sidebar { display: none; }
            .main-content { margin-left: 0; }
            .grid-2 { grid-template-columns: 1fr; }
        }
        .mobile-menu-btn {
            display: none;
            position: fixed;
            top: 10px;
            left: 10px;
            z-index: 101;
            background: #1a1a1a;
            border: 1px solid #2a2a2a;
            color: #fff;
            padding: 10px 14px;
            border-radius: 8px;
            cursor: pointer;
        }
        @media (max-width: 768px) {
            .mobile-menu-btn { display: block; }
            .sidebar.active { display: block; }
        }
        .loading {
            text-align: center;
            padding: 40px;
            color: #888;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: #1a1a1a;
            padding: 16px;
            border-radius: 12px;
            border: 1px solid #2a2a2a;
            text-align: center;
        }
        .stat-card .number {
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
        }
        .stat-card .label {
            color: #888;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <button class="mobile-menu-btn" onclick="toggleSidebar()">
        <i class="fas fa-bars"></i>
    </button>

    <!-- Sidebar -->
    <div class="sidebar" id="sidebar">
        <div class="sidebar-logo">
            <i class="fas fa-eye"></i> Stalker Bot
        </div>
        <nav>
            <a href="/" class="nav-item active">
                <i class="fas fa-users"></i> Цели
            </a>
            <a href="/reports" class="nav-item" onclick="loadPage('/reports')">
                <i class="fas fa-file-alt"></i> Отчёты
            </a>
            <a href="/add" class="nav-item" onclick="loadPage('/add')">
                <i class="fas fa-plus-circle"></i> Добавить цель
            </a>
            <a href="/stats" class="nav-item" onclick="loadPage('/stats')">
                <i class="fas fa-chart-bar"></i> Статистика
            </a>
            <a href="/settings" class="nav-item" onclick="loadPage('/settings')">
                <i class="fas fa-cog"></i> Настройки
            </a>
        </nav>
    </div>

    <!-- Main Content -->
    <div class="main-content" id="mainContent">
        <div class="flex justify-between items-center mb-6">
            <h1 class="text-3xl font-bold bg-gradient-to-r from-purple-400 to-blue-400 bg-clip-text text-transparent">
                <i class="fas fa-users"></i> Список целей
            </h1>
            <div>
                <span class="text-gray-400 mr-4" id="targetCount">0 целей</span>
                <button class="btn btn-primary" onclick="refreshData()">
                    <i class="fas fa-sync"></i>
                </button>
            </div>
        </div>

        <!-- Статистика -->
        <div class="stats-grid" id="statsGrid">
            <div class="stat-card">
                <div class="number" id="totalTargets">0</div>
                <div class="label">Всего целей</div>
            </div>
            <div class="stat-card">
                <div class="number" id="onlineTargets">0</div>
                <div class="label">В онлайне</div>
            </div>
            <div class="stat-card">
                <div class="number" id="totalChanges">0</div>
                <div class="label">Всего изменений</div>
            </div>
            <div class="stat-card">
                <div class="number" id="totalScreenshots">0</div>
                <div class="label">Скриншотов</div>
            </div>
        </div>

        <!-- Список целей -->
        <div id="targetsList">
            <div class="loading"><i class="fas fa-spinner fa-spin"></i> Загрузка...</div>
        </div>
    </div>

    <!-- Модальное окно -->
    <div class="modal" id="userModal">
        <div class="modal-content">
            <button class="modal-close" onclick="closeModal()">&times;</button>
            <div id="modalContent">
                <div class="loading"><i class="fas fa-spinner fa-spin"></i> Загрузка...</div>
            </div>
        </div>
    </div>

    <script>
        let allTargets = [];
        let currentPage = 'targets';

        async function loadData() {
            try {
                const response = await fetch('/api/targets');
                const data = await response.json();
                allTargets = data.targets || [];
                renderTargets(allTargets);
                updateStats(data.stats || {});
            } catch (error) {
                console.error('Error loading data:', error);
                document.getElementById('targetsList').innerHTML = `
                    <div class="text-center py-10 text-red-500">
                        <i class="fas fa-exclamation-circle text-4xl"></i>
                        <p>Ошибка загрузки данных</p>
                    </div>
                `;
            }
        }

        function updateStats(stats) {
            document.getElementById('totalTargets').textContent = stats.total_targets || 0;
            document.getElementById('onlineTargets').textContent = stats.online_targets || 0;
            document.getElementById('totalChanges').textContent = stats.total_changes || 0;
            document.getElementById('totalScreenshots').textContent = stats.total_screenshots || 0;
            document.getElementById('targetCount').textContent = `${stats.total_targets || 0} целей`;
        }

        function renderTargets(targets) {
            if (!targets || targets.length === 0) {
                document.getElementById('targetsList').innerHTML = `
                    <div class="text-center py-20 text-gray-500">
                        <i class="fas fa-inbox text-6xl mb-4"></i>
                        <p class="text-xl">Нет целей для отслеживания</p>
                        <p class="text-sm">Добавьте пользователя через Telegram или нажмите "Добавить цель"</p>
                    </div>
                `;
                return;
            }

            let html = '<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">';
            targets.forEach(user => {
                const statusClass = user.online ? 'status-online' : 'status-offline';
                const statusText = user.online ? 'Онлайн' : 'Офлайн';
                const avatarUrl = user.avatar || '/static/default-avatar.png';
                const lastSeen = user.last_seen ? new Date(user.last_seen).toLocaleString() : 'Никогда';
                
                html += `
                    <div class="user-card" onclick="showUser('${user.user_id}')">
                        <div class="flex items-center gap-3 mb-3">
                            <img src="${avatarUrl}" alt="avatar" class="user-avatar" 
                                 onerror="this.src='/static/default-avatar.png'">
                            <div class="flex-1 min-w-0">
                                <div class="font-semibold truncate">${user.first_name || 'Без имени'}</div>
                                <div class="text-sm text-gray-400 truncate">@${user.username || 'нет'}</div>
                            </div>
                            <span class="status-badge ${statusClass}">${statusText}</span>
                        </div>
                        <div class="text-sm text-gray-400">
                            <div class="truncate">${user.bio ? user.bio.substring(0, 80) : 'Нет био'}</div>
                            <div class="text-xs mt-1">🆔 ${user.user_id}</div>
                            <div class="text-xs text-gray-500">📅 ${lastSeen}</div>
                        </div>
                    </div>
                `;
            });
            html += '</div>';
            document.getElementById('targetsList').innerHTML = html;
        }

        async function showUser(userId) {
            const modal = document.getElementById('userModal');
            const content = document.getElementById('modalContent');
            modal.classList.add('active');
            content.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> Загрузка...</div>';

            try {
                const response = await fetch(`/api/user/${userId}`);
                const data = await response.json();
                renderUserDetails(data);
            } catch (error) {
                content.innerHTML = '<div class="text-red-500">Ошибка загрузки данных пользователя</div>';
            }
        }

        function renderUserDetails(data) {
            const user = data.user;
            const history = data.history || [];
            const screenshots = data.screenshots || [];
            const reports = data.reports || [];

            let html = `
                <div class="flex items-center gap-4 mb-6">
                    <img src="${user.avatar || '/static/default-avatar.png'}" 
                         alt="avatar" class="w-20 h-20 rounded-full object-cover border-2 border-purple-500"
                         onerror="this.src='/static/default-avatar.png'">
                    <div>
                        <h2 class="text-2xl font-bold">${user.first_name || 'Без имени'} ${user.last_name || ''}</h2>
                        <div class="text-gray-400">@${user.username || 'нет'}</div>
                        <div class="text-sm text-gray-500">🆔 ${user.user_id}</div>
                    </div>
                    <div class="ml-auto flex gap-2">
                        <button class="btn btn-danger btn-sm" onclick="removeTarget(${user.user_id})">
                            <i class="fas fa-trash"></i> Удалить
                        </button>
                        <button class="btn btn-primary btn-sm" onclick="generateReport(${user.user_id})">
                            <i class="fas fa-file-alt"></i> Отчёт
                        </button>
                    </div>
                </div>
            `;

            // Информация
            html += `
                <div class="grid grid-cols-2 gap-4 mb-6">
                    <div class="bg-black p-4 rounded-lg">
                        <div class="text-gray-400 text-sm">Био</div>
                        <div>${user.bio || 'Нет био'}</div>
                    </div>
                    <div class="bg-black p-4 rounded-lg">
                        <div class="text-gray-400 text-sm">Статус</div>
                        <div>${user.online ? '🟢 Онлайн' : '⚪ Офлайн'}</div>
                    </div>
                    <div class="bg-black p-4 rounded-lg">
                        <div class="text-gray-400 text-sm">Добавлен</div>
                        <div>${user.added_at ? new Date(user.added_at).toLocaleString() : 'Неизвестно'}</div>
                    </div>
                    <div class="bg-black p-4 rounded-lg">
                        <div class="text-gray-400 text-sm">Последний раз</div>
                        <div>${user.last_seen ? new Date(user.last_seen).toLocaleString() : 'Никогда'}</div>
                    </div>
                </div>
            `;

            // История
            html += `<h3 class="text-lg font-semibold mb-3">📜 История изменений (${history.length})</h3>`;
            if (history.length > 0) {
                html += `<div class="max-h-60 overflow-y-auto">`;
                history.forEach(item => {
                    const time = new Date(item.changed_at).toLocaleString();
                    html += `
                        <div class="history-item">
                            <div class="flex justify-between">
                                <span class="font-medium">${item.field}</span>
                                <span class="text-gray-500 text-sm">${time}</span>
                            </div>
                            <div class="text-sm text-gray-400">
                                "${item.old_value}" → "${item.new_value}"
                            </div>
                        </div>
                    `;
                });
                html += `</div>`;
            } else {
                html += `<div class="text-gray-500 text-sm">Нет истории изменений</div>`;
            }

            // Скриншоты
            html += `<h3 class="text-lg font-semibold mt-6 mb-3">📸 Скриншоты профиля (${screenshots.length})</h3>`;
            if (screenshots.length > 0) {
                html += `<div class="screenshot-grid">`;
                screenshots.forEach(item => {
                    html += `
                        <div>
                            <img src="${item.url}" alt="screenshot" 
                                 onerror="this.style.display='none'">
                            <div class="text-xs text-gray-500 mt-1">${new Date(item.captured_at).toLocaleString()}</div>
                        </div>
                    `;
                });
                html += `</div>`;
            } else {
                html += `<div class="text-gray-500 text-sm">Нет скриншотов</div>`;
            }

             // Отчёты
            html += `<h3 class="text-lg font-semibold mt-6 mb-3">📊 Отчёты (${reports.length})</h3>`;
            if (reports.length > 0) {
                reports.forEach(item => {
                    html += `
                        <div class="bg-black p-4 rounded-lg mb-3">
                            <div class="text-gray-400 text-sm">${new Date(item.created_at).toLocaleString()}</div>
                            <div class="report-text">${item.report_text}</div>
                        </div>
                    `;
                });
            } else {
                html += `<div class="text-gray-500 text-sm">Нет отчётов</div>`;
            }

            document.getElementById('modalContent').innerHTML = html;
        }

        function closeModal() {
            document.getElementById('userModal').classList.remove('active');
        }

        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('active');
        }

        function refreshData() {
            loadData();
        }

        async function removeTarget(userId) {
            if (!confirm('Удалить эту цель?')) return;
            try {
                const response = await fetch(`/api/remove/${userId}`, { method: 'POST' });
                if (response.ok) {
                    closeModal();
                    loadData();
                }
            } catch (error) {
                alert('Ошибка удаления');
            }
        }

        async function generateReport(userId) {
            try {
                const response = await fetch(`/api/report/${userId}`, { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    alert('Отчёт сгенерирован!');
                    showUser(userId);
                }
            } catch (error) {
                alert('Ошибка генерации отчёта');
            }
        }

        function loadPage(page) {
            alert(`Страница "${page}" в разработке`);
        }

        // Закрытие модального окна по клику вне
        document.getElementById('userModal').addEventListener('click', function(e) {
            if (e.target === this) closeModal();
        });

        // Загрузка данных
        loadData();
        // Обновление каждые 30 секунд
        setInterval(loadData, 30000);
    </script>
</body>
</html>
    """
    return web.Response(text=html_content, content_type='text/html')

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
        is_online = False
        
        avatar = None
        if os.path.exists('screenshots'):
            for file in os.listdir('screenshots'):
                if file.startswith(str(target['user_id'])):
                    avatar = f"/static/{file}"
                    break
        
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

async def web_api_user(request):
    """API для получения данных о конкретном пользователе"""
    user_id = int(request.match_info['user_id'])
    conn = get_db_connection()
    
    user = conn.execute('SELECT * FROM targets WHERE user_id = ?', (user_id,)).fetchone()
    if not user:
        conn.close()
        return web.json_response({'error': 'User not found'}, status=404)
    
    history = conn.execute('''
        SELECT field, old_value, new_value, changed_at 
        FROM history WHERE user_id = ? ORDER BY changed_at DESC LIMIT 100
    ''', (user_id,)).fetchall()
    
    screenshots = conn.execute('''
        SELECT photo_url, captured_at FROM screenshots 
        WHERE user_id = ? ORDER BY captured_at DESC LIMIT 20
    ''', (user_id,)).fetchall()
    
    reports = conn.execute('''
        SELECT report_text, created_at FROM reports 
        WHERE user_id = ? ORDER BY created_at DESC LIMIT 10
    ''', (user_id,)).fetchall()
    
    conn.close()
    
    avatar = None
    if os.path.exists('screenshots'):
        for file in os.listdir('screenshots'):
            if file.startswith(str(user_id)):
                avatar = f"/static/{file}"
                break
    
    return web.json_response({
        'user': {
            'user_id': user['user_id'],
            'username': user['username'],
            'first_name': user['first_name'],
            'last_name': user['last_name'],
            'bio': user['bio'] or '',
            'avatar': avatar,
            'online': False,
            'last_seen': user['last_seen'],
            'added_at': user['added_at']
        },
        'history': [dict(h) for h in history],
        'screenshots': [dict(s) for s in screenshots],
        'reports': [dict(r) for r in reports]
    })

async def web_api_remove(request):
    """API для удаления цели"""
    user_id = int(request.match_info['user_id'])
    conn = get_db_connection()
    conn.execute('DELETE FROM targets WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM history WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM screenshots WHERE user_id = ?', (user_id,))
    conn.execute('DELETE FROM reports WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()
    return web.json_response({'success': True})

async def web_api_report(request):
    """API для генерации отчёта"""
    user_id = int(request.match_info['user_id'])
    return web.json_response({'success': True, 'report': 'Отчёт сгенерирован'})

async def web_api_add(request):
    """API для добавления цели"""
    data = await request.json()
    username = data.get('username', '').replace('@', '')
    return web.json_response({'success': True, 'message': f'Цель @{username} добавлена'})

def setup_web_app(telethon_client_instance=None):
    """Настройка веб-приложения"""
    global telethon_client
    if telethon_client_instance:
        telethon_client = telethon_client_instance
    
    app = web.Application()
    
    # Статические файлы
    if os.path.exists('screenshots'):
        app.router.add_static('/static', 'screenshots')
    
    # Страницы
    app.router.add_get('/', web_dashboard)
    
    # API
    app.router.add_get('/api/targets', web_api_targets)
    app.router.add_get('/api/user/{user_id}', web_api_user)
    app.router.add_post('/api/remove/{user_id}', web_api_remove)
    app.router.add_post('/api/report/{user_id}', web_api_report)
    app.router.add_post('/api/add', web_api_add)
    
    return app