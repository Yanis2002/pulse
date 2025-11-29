import os
import threading
import time
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta
from contextlib import contextmanager
import signal
import subprocess
import shutil

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    print("WARNING: requests module not found. Telegram features will not work.")
    REQUESTS_AVAILABLE = False
    requests = None


ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "local-admin")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8574583723:AAHGnyANIA7z_7yPftV1q_HBoYWH4XkMVnI")

# Admin telegram_id for migration notifications
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "463639949")

# List of game nicknames that have admin rights
# Admin system based on game_nickname (not Telegram username)
ADMIN_GAME_NICKNAMES = os.environ.get("ADMIN_GAME_NICKNAMES", "emmpti,47").split(",")
ADMIN_GAME_NICKNAMES = [n.strip().lower() for n in ADMIN_GAME_NICKNAMES if n.strip()]

BASE_LEVELS = [
    {"sb": 100, "bb": 200, "minutes": 12, "breakMinutes": 0},
    {"sb": 200, "bb": 400, "minutes": 12, "breakMinutes": 0},
    {"sb": 300, "bb": 600, "minutes": 12, "breakMinutes": 0},
    {"sb": 400, "bb": 800, "minutes": 12, "breakMinutes": 7},
    {"sb": 500, "bb": 1000, "minutes": 12, "breakMinutes": 0},
    {"sb": 600, "bb": 1200, "minutes": 12, "breakMinutes": 0},
    {"sb": 800, "bb": 1500, "minutes": 12, "breakMinutes": 0},
    {"sb": 1000, "bb": 2000, "minutes": 12, "breakMinutes": 5},
    {"sb": 1200, "bb": 2500, "minutes": 12, "breakMinutes": 0},
    {"sb": 1500, "bb": 3000, "minutes": 12, "breakMinutes": 10},
    {"sb": 2000, "bb": 4000, "minutes": 10, "breakMinutes": 0},
    {"sb": 3000, "bb": 6000, "minutes": 10, "breakMinutes": 0},
    {"sb": 4000, "bb": 8000, "minutes": 10, "breakMinutes": 0},
    {"sb": 6000, "bb": 12000, "minutes": 10, "breakMinutes": 0},
    {"sb": 8000, "bb": 16000, "minutes": 10, "breakMinutes": 5},
    {"sb": 10000, "bb": 20000, "minutes": 10, "breakMinutes": 0},
    {"sb": 15000, "bb": 30000, "minutes": 10, "breakMinutes": 0},
    {"sb": 20000, "bb": 40000, "minutes": 10, "breakMinutes": 0},
    {"sb": 30000, "bb": 60000, "minutes": 10, "breakMinutes": 0},
    {"sb": 50000, "bb": 100000, "minutes": 10, "breakMinutes": 0},
    {"sb": 100000, "bb": 200000, "minutes": 10, "breakMinutes": 5},
    {"sb": 200000, "bb": 400000, "minutes": 10, "breakMinutes": 0},
    {"sb": 400000, "bb": 800000, "minutes": 10, "breakMinutes": 0},
    {"sb": 1000000, "bb": 2000000, "minutes": 10, "breakMinutes": 0},
    {"sb": 2000000, "bb": 4000000, "minutes": 10, "breakMinutes": 0},
    {"sb": 4000000, "bb": 8000000, "minutes": 10, "breakMinutes": 0},
]

LEVELS = deepcopy(BASE_LEVELS)

level_config = {"preMinutes": 12, "postMinutes": 10, "lateLevels": 10}

state_lock = threading.Lock()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "pulse-timer-secret")
# Use eventlet async_mode for production with gunicorn
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

# Add headers to allow Telegram widget to work
# This fixes "Bot domain invalid" error in Telegram Web
# Similar to removing SecurityMiddleware in Django
@app.after_request
def set_security_headers(response):
    # Remove X-Frame-Options to allow Telegram widget iframe
    # This is similar to removing SecurityMiddleware in Django
    response.headers.pop('X-Frame-Options', None)
    # Remove strict CSP that might block Telegram scripts
    response.headers.pop('Content-Security-Policy', None)
    return response

# Use persistent storage path if available, otherwise use local path
# For production: use /data directory (mounted persistent volume)
# For local: use current directory
DB_DIR = os.environ.get("DB_DIR", os.path.dirname(os.path.abspath(__file__)))
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "pulse_tournaments.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Initialize database with required tables."""
    with get_db() as db:
        # Tournaments table
        db.execute("""
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                month TEXT,
                year INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Players table - now uses telegram_id as primary identifier
        db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                telegram_id TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telegram_id)
            )
        """)
        
        # Tournament results table (like Google Sheets - each game is a column)
        db.execute("""
            CREATE TABLE IF NOT EXISTS tournament_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                game_number INTEGER NOT NULL,
                score INTEGER DEFAULT 0,
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
                FOREIGN KEY (player_id) REFERENCES players(id),
                UNIQUE(tournament_id, player_id, game_number)
            )
        """)
        
        # Bounty table
        db.execute("""
            CREATE TABLE IF NOT EXISTS player_bounties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                bounty INTEGER DEFAULT 0,
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
                FOREIGN KEY (player_id) REFERENCES players(id),
                UNIQUE(tournament_id, player_id)
            )
        """)
        
        # Events table
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                event_type TEXT NOT NULL,
                description TEXT,
                max_places INTEGER DEFAULT 20,
                price INTEGER DEFAULT 1000,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, time, event_type)
            )
        """)
        
        # Event registrations table
        db.execute("""
            CREATE TABLE IF NOT EXISTS event_registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                telegram_username TEXT,
                telegram_id TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
                UNIQUE(event_id, telegram_id)
            )
        """)
        
        # Tournament player states table (for poker tournaments)
        db.execute("""
            CREATE TABLE IF NOT EXISTS tournament_player_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                telegram_id TEXT,
                has_rent BOOLEAN DEFAULT 0,
                is_eliminated BOOLEAN DEFAULT 0,
                reentry_count INTEGER DEFAULT 0,
                addon_count INTEGER DEFAULT 0,
                final_place INTEGER,
                bonus_points INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
                UNIQUE(event_id, player_name)
            )
        """)
        
        # Telegram users table (for collecting bot users for mailing)
        db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT NOT NULL UNIQUE,
                first_name TEXT NOT NULL,
                last_name TEXT,
                username TEXT,
                language_code TEXT,
                is_bot BOOLEAN DEFAULT 0,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                registration_source TEXT DEFAULT 'telegram_widget'
            )
        """)
        
        # Migrate telegram_users table - add new columns if they don't exist
        try:
            db.execute("ALTER TABLE telegram_users ADD COLUMN offer_accepted BOOLEAN DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            db.execute("ALTER TABLE telegram_users ADD COLUMN offer_accepted_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            db.execute("ALTER TABLE telegram_users ADD COLUMN game_nickname TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Migrate events table - add new columns if they don't exist
        try:
            db.execute("ALTER TABLE events ADD COLUMN max_places INTEGER DEFAULT 20")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            db.execute("ALTER TABLE events ADD COLUMN price INTEGER DEFAULT 1000")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Migrate existing table if columns don't exist
        try:
            db.execute("ALTER TABLE event_registrations ADD COLUMN telegram_username TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        try:
            db.execute("ALTER TABLE event_registrations ADD COLUMN telegram_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Update unique constraint if needed
        try:
            # Drop old unique constraint if exists
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS event_registrations_unique ON event_registrations(event_id, telegram_id)")
        except sqlite3.OperationalError:
            pass
        
        # Telegram users table (for collecting bot users for mailing)
        db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT NOT NULL UNIQUE,
                first_name TEXT NOT NULL,
                last_name TEXT,
                username TEXT,
                language_code TEXT,
                is_bot BOOLEAN DEFAULT 0,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                registration_source TEXT DEFAULT 'telegram_widget',
                offer_accepted BOOLEAN DEFAULT 0,
                offer_accepted_at TIMESTAMP,
                game_nickname TEXT
            )
        """)
        
        # Create default tournaments for November and December if none exist
        result = db.execute("SELECT COUNT(*) as count FROM tournaments").fetchone()
        tournament_id = None
        if result["count"] == 0:
            now = datetime.now()
            year = now.year
            
            # Create November tournament
            nov_tournament = db.execute("""
                SELECT id FROM tournaments WHERE month = ? AND year = ?
            """, ("November", year)).fetchone()
            if not nov_tournament:
                cursor = db.execute("""
                    INSERT INTO tournaments (name, month, year)
                    VALUES (?, ?, ?)
                """, ("РЕЙТИНГ НОЯБРЯ", "November", year))
                tournament_id = cursor.lastrowid
            
            # Create December tournament
            dec_tournament = db.execute("""
                SELECT id FROM tournaments WHERE month = ? AND year = ?
            """, ("December", year)).fetchone()
            if not dec_tournament:
                cursor = db.execute("""
                    INSERT INTO tournaments (name, month, year)
                    VALUES (?, ?, ?)
                """, ("РЕЙТИНГ ДЕКАБРЯ", "December", year))
                if not tournament_id:
                    tournament_id = cursor.lastrowid
        else:
            tournament = db.execute("SELECT id FROM tournaments ORDER BY id DESC LIMIT 1").fetchone()
            tournament_id = tournament["id"] if tournament else None
        
        # Initialize default players if none exist
        player_count = db.execute("SELECT COUNT(*) as count FROM players").fetchone()
        if player_count["count"] == 0 and tournament_id:
            default_players = [
                "13 reason for", "ANDREYU", "Abrasha", "tolch__", "Artem",
                "Art", "Fish2005", "St05", "Винни", "Psychoanya",
                "kolyupaska", "apheristka", "Livinsl", "SergeyKoller",
                "TanyaKoller", "dombrovich"
            ]
            for name in default_players:
                try:
                    cursor = db.execute("INSERT INTO players (name) VALUES (?)", (name,))
                    player_id = cursor.lastrowid
                except sqlite3.IntegrityError:
                    # Player already exists, get ID
                    player = db.execute("SELECT id FROM players WHERE name = ?", (name,)).fetchone()
                    player_id = player["id"] if player else None


def minutes_for_level(index: int) -> int:
    idx = max(0, min(index, len(LEVELS) - 1))
    return level_config["preMinutes"] if idx < level_config["lateLevels"] else level_config["postMinutes"]


def stage_duration_seconds(index: int, is_break: bool) -> int:
    if is_break:
        minutes = LEVELS[index].get("breakMinutes", 0) or 0
        return max(1, minutes * 60)
    return max(1, minutes_for_level(index) * 60)


def build_state():
    return {
        "levelIndex": state["level_index"],
        "isInBreak": state["is_in_break"],
        "isRunning": state["is_running"],
        "timeLeft": state["time_left"],
        "ringTotal": state["ring_total"],
        "lastUpdate": state["last_update"],
        "version": state["version"],
        "breakMinutes": [lvl.get("breakMinutes", 0) for lvl in LEVELS],
        "levelConfig": level_config.copy(),
    }


state = {
    "level_index": 0,
    "is_in_break": False,
    "is_running": False,
    "time_left": stage_duration_seconds(0, False),
    "ring_total": stage_duration_seconds(0, False),
    "last_update": time.time(),
    "version": 1,
}


def reset_stage(keep_running: bool):
    """Reset timers for the current stage (level or break)."""
    idx = state["level_index"]
    total = stage_duration_seconds(idx, state["is_in_break"])
    state["ring_total"] = total
    state["time_left"] = total
    state["last_update"] = time.time()
    if not keep_running:
        state["is_running"] = False
    state["version"] += 1


def start_break_if_needed():
    idx = state["level_index"]
    break_minutes = LEVELS[idx].get("breakMinutes", 0) or 0
    if break_minutes > 0:
        state["is_in_break"] = True
        reset_stage(keep_running=True)
    else:
        advance_to_next_level()


def advance_to_next_level():
    if state["level_index"] < len(LEVELS) - 1:
        state["level_index"] += 1
    state["is_in_break"] = False
    reset_stage(keep_running=True)


def complete_current_stage():
    if state["is_in_break"]:
        state["is_in_break"] = False
        advance_to_next_level()
        return

    at_last_level = state["level_index"] >= len(LEVELS) - 1
    has_break = (LEVELS[state["level_index"]].get("breakMinutes", 0) or 0) > 0
    if at_last_level and not has_break:
        state["is_running"] = False
        state["time_left"] = 0
        state["last_update"] = time.time()
        state["version"] += 1
        return

    start_break_if_needed()


def emit_state(payload=None):
    socketio.emit("state", payload or build_state())


def timer_loop():
    while True:
        time.sleep(0.5)
        with state_lock:
            if not state["is_running"]:
                continue
            now = time.time()
            elapsed = now - state["last_update"]
            if elapsed <= 0:
                continue
            state["time_left"] = max(0.0, state["time_left"] - elapsed)
            state["last_update"] = now
            if state["time_left"] <= 0:
                complete_current_stage()
            state["version"] += 1
            payload = build_state()
        emit_state(payload)


def check_is_admin(game_nickname=None, telegram_id=None):
    """Check if user is admin based on game_nickname."""
    print(f"🔍 check_is_admin called with: game_nickname='{game_nickname}', telegram_id='{telegram_id}'")
    print(f"🔍 ADMIN_GAME_NICKNAMES: {ADMIN_GAME_NICKNAMES}")
    
    # If game_nickname not provided, try to get it from telegram_id
    if not game_nickname and telegram_id:
        try:
            with get_db() as db:
                user = db.execute("SELECT game_nickname FROM telegram_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
                if user and user["game_nickname"]:
                    game_nickname = user["game_nickname"]
                    print(f"🔍 Got game_nickname from telegram_id: '{game_nickname}'")
        except Exception as e:
            print(f"⚠️ Error fetching game_nickname: {e}")
    
    if not game_nickname:
        print("❌ No game_nickname provided")
        return False
    
    # Convert to lowercase
    nickname = game_nickname.strip().lower()
    print(f"🔍 Normalized nickname: '{nickname}'")
    
    is_admin = nickname in ADMIN_GAME_NICKNAMES
    print(f"🔍 Is admin: {is_admin}")
    
    return is_admin

@app.route("/")
def index():
    """Main dashboard page with splash screen."""
    # Pages are accessible to everyone, admin status is checked dynamically on frontend
    return render_template("dashboard.html", is_admin=False, admin_token=ADMIN_TOKEN)


@app.route("/timer")
def timer():
    """Timer page."""
    # Pages are accessible to everyone, admin status is checked dynamically on frontend
    return render_template("index.html", is_admin=False, admin_token=ADMIN_TOKEN)


@app.route("/rating")
def rating():
    # Pages are accessible to everyone, admin status is checked dynamically on frontend
    return render_template("rating.html", is_admin=False, admin_token=ADMIN_TOKEN)

@app.route("/contacts")
def contacts():
    return render_template("contacts.html")

@app.route("/debug/logs")
def debug_logs():
    """Debug page to view logs in real-time."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Debug Logs</title>
        <meta charset="UTF-8">
        <style>
            body {
                font-family: 'Courier New', monospace;
                background: #0a0a0a;
                color: #0f0;
                padding: 20px;
                margin: 0;
            }
            .container {
                max-width: 1200px;
                margin: 0 auto;
            }
            h1 {
                color: #d32f2f;
                text-shadow: 0 0 10px #d32f2f;
            }
            .section {
                margin: 20px 0;
                padding: 15px;
                background: #1a1a1a;
                border: 1px solid #333;
                border-radius: 5px;
            }
            .log-container {
                background: #000;
                padding: 15px;
                border-radius: 5px;
                max-height: 500px;
                overflow-y: auto;
                font-size: 12px;
                line-height: 1.6;
            }
            .log-entry {
                margin: 5px 0;
                padding: 5px;
                border-left: 3px solid #333;
                padding-left: 10px;
            }
            .log-success { border-left-color: #0f0; color: #0f0; }
            .log-error { border-left-color: #f00; color: #f00; }
            .log-info { border-left-color: #0ff; color: #0ff; }
            .log-warn { border-left-color: #ff0; color: #ff0; }
            button {
                background: #d32f2f;
                color: white;
                border: none;
                padding: 10px 20px;
                cursor: pointer;
                border-radius: 5px;
                margin: 5px;
                font-size: 14px;
            }
            button:hover { background: #b71c1c; }
            .instructions {
                background: #1a1a1a;
                padding: 15px;
                border-radius: 5px;
                margin-bottom: 20px;
                border: 1px solid #333;
            }
            .instructions h2 {
                color: #0ff;
                margin-top: 0;
            }
            .instructions ol {
                color: #ccc;
                line-height: 1.8;
            }
            .instructions code {
                background: #000;
                padding: 2px 6px;
                border-radius: 3px;
                color: #0f0;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🔍 Debug Logs Viewer</h1>
            
            <div class="instructions">
                <h2>📋 Как использовать:</h2>
                <ol>
                    <li>Откройте консоль браузера: <code>F12</code> (или <code>Cmd+Option+I</code> на Mac)</li>
                    <li>Перейдите на вкладку <code>Console</code></li>
                    <li>Откройте профиль (👤) на сайте</li>
                    <li>Смотрите логи в консоли браузера</li>
                    <li>Логи сервера смотрите в терминале, где запущен Flask</li>
                </ol>
            </div>
            
            <div class="section">
                <h2>📝 Логи браузера (симуляция)</h2>
                <button onclick="simulateLogs()">Симулировать логи</button>
                <button onclick="clearBrowserLogs()">Очистить</button>
                <div id="browser-logs" class="log-container"></div>
            </div>
            
            <div class="section">
                <h2>🔧 Проверка функций</h2>
                <button onclick="checkFunctions()">Проверить функции</button>
                <button onclick="testWidgetInit()">Тест инициализации виджета</button>
                <div id="function-check" class="log-container"></div>
            </div>
            
            <div class="section">
                <h2>💾 LocalStorage</h2>
                <button onclick="showLocalStorage()">Показать LocalStorage</button>
                <button onclick="clearLocalStorage()">Очистить LocalStorage</button>
                <div id="localstorage" class="log-container"></div>
            </div>
        </div>
        
        <script>
            function addLog(containerId, message, type = 'info') {
                const container = document.getElementById(containerId);
                const entry = document.createElement('div');
                entry.className = 'log-entry log-' + type;
                const time = new Date().toLocaleTimeString();
                entry.textContent = `[${time}] ${message}`;
                container.appendChild(entry);
                container.scrollTop = container.scrollHeight;
            }
            
            function clearBrowserLogs() {
                document.getElementById('browser-logs').innerHTML = '';
            }
            
            function simulateLogs() {
                const logs = [
                    { msg: '🔧 Defining window.onTelegramAuth function...', type: 'info' },
                    { msg: '👤 Profile modal opened, initializing Telegram widget...', type: 'info' },
                    { msg: '⏰ Timeout fired, calling initTelegramWidget...', type: 'info' },
                    { msg: '🔧 initTelegramWidget called', type: 'info' },
                    { msg: '✅ Container found: <div id="telegram-widget-container">', type: 'success' },
                    { msg: '✅ window.onTelegramAuth is defined', type: 'success' },
                    { msg: '🌐 Current domain: pulse-390031593512.europe-north1.run.app isLocalhost: false', type: 'info' },
                    { msg: '📝 Widget script attributes set: {data-telegram-login: "Pulse_Club_bot", ...}', type: 'info' },
                    { msg: '📤 Appending widget script to container', type: 'info' },
                    { msg: '✅ Widget script appended', type: 'success' },
                    { msg: '✅ Telegram widget script loaded successfully', type: 'success' },
                ];
                
                logs.forEach((log, index) => {
                    setTimeout(() => {
                        addLog('browser-logs', log.msg, log.type);
                    }, index * 200);
                });
            }
            
            function checkFunctions() {
                const container = document.getElementById('function-check');
                container.innerHTML = '';
                
                addLog('function-check', 'Проверка функций...', 'info');
                
                // Check onTelegramAuth
                if (typeof window.onTelegramAuth === 'function') {
                    addLog('function-check', '✅ window.onTelegramAuth is defined', 'success');
                } else {
                    addLog('function-check', '❌ window.onTelegramAuth is NOT defined', 'error');
                }
                
                // Check initTelegramWidget
                if (typeof initTelegramWidget === 'function') {
                    addLog('function-check', '✅ initTelegramWidget is defined', 'success');
                } else {
                    addLog('function-check', '❌ initTelegramWidget is NOT defined', 'error');
                }
                
                // Check container
                const widgetContainer = document.getElementById('telegram-widget-container');
                if (widgetContainer) {
                    addLog('function-check', '✅ telegram-widget-container found', 'success');
                } else {
                    addLog('function-check', '❌ telegram-widget-container NOT found', 'error');
                }
            }
            
            function testWidgetInit() {
                const container = document.getElementById('function-check');
                addLog('function-check', '🧪 Testing widget initialization...', 'info');
                
                if (typeof initTelegramWidget === 'function') {
                    try {
                        initTelegramWidget();
                        addLog('function-check', '✅ initTelegramWidget called successfully', 'success');
                    } catch (error) {
                        addLog('function-check', '❌ Error: ' + error.message, 'error');
                    }
                } else {
                    addLog('function-check', '❌ initTelegramWidget function not found', 'error');
                }
            }
            
            function showLocalStorage() {
                const container = document.getElementById('localstorage');
                container.innerHTML = '';
                
                const telegramUsername = localStorage.getItem('pulse_telegram_username');
                const telegramId = localStorage.getItem('pulse_telegram_id');
                const playerName = localStorage.getItem('pulse_player_name');
                
                addLog('localstorage', '=== LocalStorage Contents ===', 'info');
                addLog('localstorage', `pulse_telegram_username: ${telegramUsername || 'NOT SET'}`, telegramUsername ? 'success' : 'error');
                addLog('localstorage', `pulse_telegram_id: ${telegramId || 'NOT SET'}`, telegramId ? 'success' : 'error');
                addLog('localstorage', `pulse_player_name: ${playerName || 'NOT SET'}`, playerName ? 'success' : 'error');
            }
            
            function clearLocalStorage() {
                if (confirm('Очистить весь LocalStorage? Это удалит все сохраненные данные.')) {
                    localStorage.clear();
                    addLog('localstorage', '✅ LocalStorage cleared', 'success');
                }
            }
            
            // Auto-check on load
            window.onload = function() {
                checkFunctions();
                showLocalStorage();
            };
        </script>
    </body>
    </html>
    """
    
@app.route("/debug/admin")
def debug_admin():
    """Debug page to check admin status."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Debug</title>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: monospace;
                background: #0a0a0a;
                color: #fff;
                padding: 20px;
            }}
            .section {{
                margin: 20px 0;
                padding: 15px;
                background: #1a1a1a;
                border: 1px solid #333;
                border-radius: 5px;
            }}
            .success {{ color: #0f0; }}
            .error {{ color: #f00; }}
            .info {{ color: #0ff; }}
            button {{
                background: #d32f2f;
                color: white;
                border: none;
                padding: 10px 20px;
                cursor: pointer;
                border-radius: 5px;
                margin: 5px;
            }}
            button:hover {{ background: #b71c1c; }}
            #log {{
                background: #000;
                padding: 10px;
                border-radius: 5px;
                max-height: 400px;
                overflow-y: auto;
                font-size: 12px;
            }}
        </style>
    </head>
    <body>
        <h1>🔍 Admin Debug Page</h1>
        
        <div class="section">
            <h2>📋 Current Status</h2>
            <div id="status">Loading...</div>
        </div>
        
        <div class="section">
            <h2>🔧 Actions</h2>
            <button onclick="checkAdmin()">Check Admin Status</button>
            <button onclick="clearLog()">Clear Log</button>
            <button onclick="showLocalStorage()">Show LocalStorage</button>
        </div>
        
        <div class="section">
            <h2>📝 Log</h2>
            <div id="log"></div>
        </div>
        
        <script>
            function log(message, type = 'info') {{
                const logDiv = document.getElementById('log');
                const time = new Date().toLocaleTimeString();
                const color = type === 'error' ? 'error' : type === 'success' ? 'success' : 'info';
                logDiv.innerHTML += `<div class="${{color}}">[${{time}}] ${{message}}</div>`;
                logDiv.scrollTop = logDiv.scrollHeight;
            }}
            
            function clearLog() {{
                document.getElementById('log').innerHTML = '';
            }}
            
            function showLocalStorage() {{
                const telegramUsername = localStorage.getItem('pulse_telegram_username');
                const telegramId = localStorage.getItem('pulse_telegram_id');
                const playerName = localStorage.getItem('pulse_player_name');
                
                log('=== LocalStorage ===', 'info');
                log(`telegram_username: ${{telegramUsername || 'NOT SET'}}`, telegramUsername ? 'success' : 'error');
                log(`telegram_id: ${{telegramId || 'NOT SET'}}`, telegramId ? 'success' : 'error');
                log(`player_name: ${{playerName || 'NOT SET'}}`, playerName ? 'success' : 'error');
            }}
            
            async function checkAdmin() {{
                const telegramUsername = localStorage.getItem('pulse_telegram_username');
                
                if (!telegramUsername) {{
                    log('❌ No telegram_username in localStorage!', 'error');
                    log('💡 You need to login through Telegram widget first', 'info');
                    updateStatus('❌ Not logged in', 'error');
                    return;
                }}
                
                log(`🔍 Checking admin status for: ${{telegramUsername}}`, 'info');
                
                try {{
                    const response = await fetch('/api/telegram/check-admin?username=' + encodeURIComponent(telegramUsername));
                    const data = await response.json();
                    
                    log(`📡 Response: ${{JSON.stringify(data, null, 2)}}`, 'info');
                    
                    if (data.ok && data.is_admin) {{
                        log('✅ ADMIN ACCESS GRANTED!', 'success');
                        updateStatus('✅ You are ADMIN', 'success');
                    }} else {{
                        log('❌ NOT ADMIN', 'error');
                        log(`Admin list: ${{JSON.stringify(data.admin_list || [])}}`, 'info');
                        updateStatus('❌ You are NOT admin', 'error');
                    }}
                }} catch (error) {{
                    log(`❌ Error: ${{error.message}}`, 'error');
                    updateStatus('❌ Error checking status', 'error');
                }}
            }}
            
            function updateStatus(message, type) {{
                const statusDiv = document.getElementById('status');
                statusDiv.className = type;
                statusDiv.textContent = message;
            }}
            
            // Auto-check on load
            window.onload = function() {{
                log('🔍 Debug page loaded', 'info');
                showLocalStorage();
                checkAdmin();
            }};
        </script>
    </body>
    </html>
    """

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors by redirecting to main page."""
    return render_template("dashboard.html", is_admin=False, admin_token=ADMIN_TOKEN), 404


@app.route("/api/rating")
def api_rating():
    """Get current rating data. Optionally filter by date (only players registered for events on that date)."""
    date_filter = request.args.get("date")  # Format: YYYY-MM-DD
    
    if date_filter:
        # Filter to only players registered for events on this date
        with get_db() as db:
            # Get all players registered for events on this date
            registered_players = db.execute("""
                SELECT DISTINCT er.player_name, er.telegram_id
                FROM event_registrations er
                JOIN events e ON er.event_id = e.id
                WHERE e.date = ?
            """, (date_filter,)).fetchall()
            
            registered_names = {row["player_name"] for row in registered_players}
            registered_telegram_ids = {row["telegram_id"] for row in registered_players if row["telegram_id"]}
            
            # Get rating data and filter
            all_players = get_rating_data()
            filtered_players = [
                p for p in all_players 
                if p["name"] in registered_names or 
                   (p.get("telegram_id") and p["telegram_id"] in registered_telegram_ids)
            ]
            
            return jsonify({"ok": True, "players": filtered_players})
    else:
        return jsonify({"ok": True, "players": get_rating_data()})


@app.route("/api/tournament/<int:tournament_id>")
def api_get_tournament(tournament_id):
    """Get tournament data with all players and scores."""
    with get_db() as db:
        tournament = db.execute(
            "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
        ).fetchone()
        if not tournament:
            return jsonify({"ok": False, "error": "Tournament not found"}), 404
        
        # Calculate days in month
        month = tournament["month"]
        year = tournament["year"]
        if month == "November" or month == "ноября" or month == "November":
            days_in_month = 30
        elif month == "December" or month == "декабря" or month == "December":
            days_in_month = 31
        else:
            # Default to 30 if unknown
            days_in_month = 30
        
        # Get all players
        players = db.execute("SELECT * FROM players ORDER BY name").fetchall()
        players_dict = {p["id"]: {"id": p["id"], "name": p["name"]} for p in players}
        
        # Get all scores for this tournament
        scores = db.execute("""
            SELECT player_id, game_number, score
            FROM tournament_results
            WHERE tournament_id = ?
        """, (tournament_id,)).fetchall()
        
        # Get bounties
        bounties = db.execute("""
            SELECT player_id, bounty
            FROM player_bounties
            WHERE tournament_id = ?
        """, (tournament_id,)).fetchall()
        
        # Build result structure
        result = {
            "tournament": {
                "id": tournament["id"],
                "name": tournament["name"],
                "month": tournament["month"],
                "year": tournament["year"]
            },
            "players": [],
            "max_games": days_in_month  # Use days in month instead of calculated max
        }
        
        # Calculate totals and organize data
        for player in players:
            player_scores = {}
            total = 0
            for score_row in scores:
                if score_row["player_id"] == player["id"]:
                    game_num = score_row["game_number"]
                    score_val = score_row["score"]
                    player_scores[game_num] = score_val
                    total += score_val
            
            bounty = 0
            for bounty_row in bounties:
                if bounty_row["player_id"] == player["id"]:
                    bounty = bounty_row["bounty"]
                    break
            
            result["players"].append({
                "id": player["id"],
                "name": player["name"],
                "total": total,
                "bounty": bounty,
                "scores": player_scores
            })
        
        # Sort by total descending
        result["players"].sort(key=lambda x: -x["total"])
        
        return jsonify({"ok": True, "data": result})


@app.route("/api/tournaments")
def api_get_tournaments():
    """Get list of tournaments by month."""
    month = request.args.get("month")  # "November" or "December"
    year = request.args.get("year", type=int)
    
    if not month:
        return jsonify({"ok": False, "error": "month parameter required"}), 400
    
    try:
        with get_db() as db:
            query = "SELECT * FROM tournaments WHERE month = ?"
            params = [month]
            
            if year:
                query += " AND year = ?"
                params.append(year)
            
            query += " ORDER BY year DESC, id DESC"
            
            tournaments = db.execute(query, params).fetchall()
            
            result = []
            for t in tournaments:
                result.append({
                    "id": t["id"],
                    "name": t["name"],
                    "month": t["month"],
                    "year": t["year"]
                })
            
            return jsonify({"ok": True, "tournaments": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tournament/<int:tournament_id>/score", methods=["POST"])
def api_update_score(tournament_id):
    """Update a single score cell."""
    data = request.get_json() or {}
    try:
        require_admin(data)
    except PermissionError:
        return jsonify({"ok": False, "error": "invalid token or not admin"}), 403
    
    player_id = data.get("player_id")
    game_number = data.get("game_number")
    score = data.get("score", 0)
    
    if not player_id or not game_number:
        return jsonify({"ok": False, "error": "missing parameters"}), 400
    
    try:
        with get_db() as db:
            db.execute("""
                INSERT OR REPLACE INTO tournament_results
                (tournament_id, player_id, game_number, score)
                VALUES (?, ?, ?, ?)
            """, (tournament_id, player_id, game_number, int(score)))
        
        socketio.emit("tournament_update", {"tournament_id": tournament_id})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tournament/<int:tournament_id>/bounty", methods=["POST"])
def api_update_bounty(tournament_id):
    """Update player bounty."""
    data = request.get_json() or {}
    try:
        require_admin(data)
    except PermissionError:
        return jsonify({"ok": False, "error": "invalid token or not admin"}), 403
    
    player_id = data.get("player_id")
    bounty = data.get("bounty", 0)
    
    if not player_id:
        return jsonify({"ok": False, "error": "missing player_id"}), 400
    
    try:
        with get_db() as db:
            db.execute("""
                INSERT OR REPLACE INTO player_bounties
                (tournament_id, player_id, bounty)
                VALUES (?, ?, ?)
            """, (tournament_id, player_id, int(bounty)))
        
        socketio.emit("tournament_update", {"tournament_id": tournament_id})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/player", methods=["POST"])
def api_add_player():
    """Add a new player."""
    data = request.get_json() or {}
    try:
        require_admin(data)
    except PermissionError:
        return jsonify({"ok": False, "error": "invalid token or not admin"}), 403
    
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    
    try:
        with get_db() as db:
            cursor = db.execute("INSERT INTO players (name) VALUES (?)", (name,))
            player_id = cursor.lastrowid
        return jsonify({"ok": True, "player_id": player_id})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "player already exists"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/events", methods=["GET"])
def api_get_events():
    """Get events for a date range."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    telegram_id = request.args.get("telegram_id")  # Optional: to check if user is registered
    
    if not start_date or not end_date:
        return jsonify({"ok": False, "error": "start_date and end_date required"}), 400
    
    try:
        with get_db() as db:
            events = db.execute("""
                SELECT e.id, e.date, e.time, e.event_type, e.description, 
                       COALESCE(e.max_places, 20) as max_places,
                       COALESCE(e.price, 1000) as price,
                       COALESCE(GROUP_CONCAT(er.player_name, '|'), '') as registered_players,
                       COALESCE(GROUP_CONCAT(er.telegram_username, '|'), '') as telegram_usernames,
                       COUNT(er.id) as registration_count
                FROM events e
                LEFT JOIN event_registrations er ON e.id = er.event_id
                WHERE e.date >= ? AND e.date <= ?
                GROUP BY e.id, e.date, e.time, e.event_type, e.description, e.max_places, e.price
                ORDER BY e.date, e.time
            """, (start_date, end_date)).fetchall()
            
            result = []
            for event in events:
                registered = []
                telegram_users = []
                reg_players = event["registered_players"] or ""
                if reg_players:
                    registered = [p for p in reg_players.split("|") if p]
                tel_users = event["telegram_usernames"] or ""
                if tel_users:
                    telegram_users = [u for u in tel_users.split("|") if u]
                
                # Check if current user is registered
                is_user_registered = False
                if telegram_id:
                    try:
                        check = db.execute("""
                            SELECT id FROM event_registrations 
                            WHERE event_id = ? AND telegram_id = ?
                        """, (event["id"], telegram_id)).fetchone()
                        is_user_registered = check is not None
                    except Exception:
                        pass
                
                # Get max_places and price, defaulting to 20 and 1000 if None
                max_places = event.get("max_places")
                if max_places is None:
                    max_places = 20
                else:
                    try:
                        max_places = int(max_places)
                    except (ValueError, TypeError):
                        max_places = 20
                
                price = event.get("price")
                if price is None:
                    price = 1000
                else:
                    try:
                        price = int(price)
                    except (ValueError, TypeError):
                        price = 1000
                
                result.append({
                    "id": event["id"],
                    "date": event["date"],
                    "time": event["time"],
                    "event_type": event["event_type"],
                    "description": event["description"] or "",
                    "max_places": max_places,
                    "price": price,
                    "registered": registered,
                    "telegram_users": telegram_users,
                    "registration_count": event["registration_count"] or 0,
                    "is_registered": is_user_registered
                })
            
            return jsonify({"ok": True, "events": result})
    except Exception as e:
        import traceback
        import logging
        logging.error(f"Error in api_get_events: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/events", methods=["POST"])
def api_create_event():
    """Create a new event (admin only)."""
    data = request.get_json() or {}
    try:
        require_admin(data)
    except PermissionError:
        return jsonify({"ok": False, "error": "invalid token or not admin"}), 403
    
    date = data.get("date", "").strip()
    time = data.get("time", "").strip()
    event_type = data.get("event_type", "").strip()
    description = data.get("description", "").strip()
    max_places = data.get("max_places", 20)
    price = data.get("price", 1000)
    
    if not date or not time or not event_type:
        return jsonify({"ok": False, "error": "date, time and event_type required"}), 400
    
    if event_type not in ["Мафия", "Покер", "Свободная игра"]:
        return jsonify({"ok": False, "error": "invalid event_type"}), 400
    
    try:
        max_places = int(max_places) if max_places else 20
        price = int(price) if price else 1000
    except (ValueError, TypeError):
        max_places = 20
        price = 1000
    
    try:
        with get_db() as db:
            cursor = db.execute("""
                INSERT INTO events (date, time, event_type, description, max_places, price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (date, time, event_type, description, max_places, price))
            event_id = cursor.lastrowid
        socketio.emit("events_update", {"date": date})
        return jsonify({"ok": True, "event_id": event_id})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "event already exists"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/events/<int:event_id>/register", methods=["POST"])
def api_register_event(event_id):
    """Register for an event."""
    data = request.get_json() or {}
    player_name = data.get("player_name", "").strip()
    telegram_username = data.get("telegram_username", "").strip()
    telegram_id = data.get("telegram_id", "").strip()
    game_nickname = data.get("game_nickname", "").strip()
    
    # Registration requires 3 factors: telegram_id, offer_accepted, and game_nickname
    # Factor 1: telegram_id (authorization)
    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id required for authorization"}), 400
    
    # Check if user exists in database (authorization check)
    try:
        with get_db() as db:
            user = db.execute("SELECT telegram_id, offer_accepted, game_nickname FROM telegram_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if not user:
                return jsonify({"ok": False, "error": "User not authorized. Please register via Telegram bot (/start)"}), 401
            
            # Factor 2: offer_accepted
            if not user.get("offer_accepted"):
                return jsonify({"ok": False, "error": "offer_not_accepted", "message": "Необходимо принять публичную оферту для записи на события"}), 403
            
            # Factor 3: game_nickname
            if not user.get("game_nickname"):
                return jsonify({"ok": False, "error": "game_nickname_not_set", "message": "Необходимо указать игровой никнейм для записи на события"}), 403
    except Exception as e:
        print(f"Error checking user authorization: {e}")
        return jsonify({"ok": False, "error": "Authorization check failed"}), 500
    
    if not player_name:
        return jsonify({"ok": False, "error": "player_name required"}), 400
    
    if not game_nickname:
        return jsonify({"ok": False, "error": "game_nickname required"}), 400
    
    # Validate game_nickname: 2-20 chars, letters (lat/cyrillic), numbers, underscore, spaces
    import re
    # Allow letters (lat/cyrillic), numbers, underscore, and spaces
    # Trim spaces from start/end, but allow spaces in the middle
    game_nickname = game_nickname.strip()
    if len(game_nickname) < 2 or len(game_nickname) > 20:
        return jsonify({"ok": False, "error": "game_nickname должен содержать 2-20 символов"}), 400
    if not re.match(r'^[a-zA-Zа-яА-ЯёЁ0-9_\s]+$', game_nickname):
        return jsonify({"ok": False, "error": "game_nickname может содержать только буквы (лат/кирилл), цифры, пробелы и _"}), 400
    # Don't allow only spaces
    if not game_nickname.replace(' ', '').replace('_', ''):
        return jsonify({"ok": False, "error": "game_nickname не может состоять только из пробелов и подчеркиваний"}), 400
    
    try:
        with get_db() as db:
            # All 3 factors (telegram_id, offer_accepted, game_nickname) already checked above
            # Just verify event exists
            event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return jsonify({"ok": False, "error": "event not found"}), 404
            
            # Update game_nickname in telegram_users if provided
            if telegram_id:
                db.execute("""
                    UPDATE telegram_users 
                    SET game_nickname = ?, last_active = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                """, (game_nickname, telegram_id))
            elif telegram_username:
                db.execute("""
                    UPDATE telegram_users 
                    SET game_nickname = ?, last_active = CURRENT_TIMESTAMP
                    WHERE username = ?
                """, (game_nickname, telegram_username))
            
            # Register with game_nickname as player_name
            db.execute("""
                INSERT INTO event_registrations (event_id, player_name, telegram_username, telegram_id)
                VALUES (?, ?, ?, ?)
            """, (event_id, game_nickname, telegram_username or None, telegram_id or None))
        
        socketio.emit("events_update", {"date": event["date"]})
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "already registered"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/events/<int:event_id>/unregister", methods=["POST"])
def api_unregister_event(event_id):
    """Unregister from an event."""
    data = request.get_json() or {}
    telegram_id = data.get("telegram_id", "").strip()
    
    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id required"}), 400
    
    try:
        with get_db() as db:
            event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return jsonify({"ok": False, "error": "event not found"}), 404
            
            db.execute("""
                DELETE FROM event_registrations
                WHERE event_id = ? AND telegram_id = ?
            """, (event_id, telegram_id))
        
        socketio.emit("events_update", {"date": event["date"]})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/events/<int:event_id>/players")
def api_get_event_players(event_id):
    """Get list of registered players for an event."""
    try:
        with get_db() as db:
            event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return jsonify({"ok": False, "error": "event not found"}), 404
            
            registrations = db.execute("""
                SELECT player_name, telegram_username, telegram_id
                FROM event_registrations
                WHERE event_id = ?
                ORDER BY player_name
            """, (event_id,)).fetchall()
            
            players = []
            for reg in registrations:
                players.append({
                    "name": reg["player_name"],
                    "telegram_username": reg["telegram_username"] or reg["telegram_id"] or "",
                    "telegram_id": reg["telegram_id"] or ""
                })
            
            return jsonify({
                "ok": True,
                "event": {
                    "id": event["id"],
                    "date": event["date"],
                    "time": event["time"],
                    "event_type": event["event_type"],
                    "description": event["description"],
                    "max_places": event.get("max_places", 20),
                    "price": event.get("price", 1000)
                },
                "players": players
            })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def api_delete_event(event_id):
    """Delete an event (admin only)."""
    data = request.get_json() or {}
    try:
        require_admin(data)
    except PermissionError:
        return jsonify({"ok": False, "error": "invalid token or not admin"}), 403
    
    try:
        with get_db() as db:
            event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return jsonify({"ok": False, "error": "event not found"}), 404
            
            # Delete event (cascade will delete registrations)
            db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        
        socketio.emit("events_update", {"date": event["date"]})
        return jsonify({"ok": True, "message": "Event deleted"})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/poker-tournament/<date>")
def api_get_poker_tournament(date):
    """Get poker tournament players for a specific date (admin only)."""
    # Check admin token
    token = request.args.get("token") or (request.get_json() or {}).get("token", "")
    telegram_username = request.args.get("telegram_username") or (request.get_json() or {}).get("telegram_username", "")
    try:
        require_admin({"token": token, "telegram_username": telegram_username})
    except PermissionError:
        return jsonify({"ok": False, "error": "invalid token or not admin"}), 403
    
    try:
        with get_db() as db:
            # Get all poker events for this date
            events = db.execute("""
                SELECT id, time, description
                FROM events
                WHERE date = ? AND event_type = 'Покер'
                ORDER BY time
            """, (date,)).fetchall()
            
            if not events:
                return jsonify({"ok": True, "events": [], "players": []})
            
            # Get all registered players for these events
            event_ids = [e["id"] for e in events]
            placeholders = ",".join("?" * len(event_ids))
            
            registrations = db.execute(f"""
                SELECT DISTINCT er.player_name, er.telegram_id, er.event_id, e.time as event_time
                FROM event_registrations er
                JOIN events e ON er.event_id = e.id
                WHERE er.event_id IN ({placeholders})
                ORDER BY er.player_name
            """, event_ids).fetchall()
            
            # Get player states
            states = db.execute(f"""
                SELECT player_name, has_rent, is_eliminated, reentry_count, addon_count, final_place, bonus_points
                FROM tournament_player_states
                WHERE event_id IN ({placeholders})
            """, event_ids).fetchall()
            
            states_dict = {s["player_name"]: s for s in states}
            
            players = []
            for reg in registrations:
                player_name = reg["player_name"]
                state = states_dict.get(player_name, {})
                players.append({
                    "name": player_name,
                    "telegram_id": reg["telegram_id"],
                    "event_id": reg["event_id"],
                    "event_time": reg["event_time"],
                    "has_rent": bool(state.get("has_rent", False)),
                    "is_eliminated": bool(state.get("is_eliminated", False)),
                    "reentry_count": state.get("reentry_count", 0) or 0,
                    "addon_count": state.get("addon_count", 0) or 0,
                    "final_place": state.get("final_place"),
                    "bonus_points": state.get("bonus_points", 0) or 0
                })
            
            return jsonify({
                "ok": True,
                "events": [{"id": e["id"], "time": e["time"], "description": e["description"]} for e in events],
                "players": players
            })
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/poker-tournament/<date>/player", methods=["POST"])
def api_update_poker_player(date):
    """Update poker tournament player state (rent, elimination, reentry, addon)."""
    data = request.get_json() or {}
    try:
        require_admin(data)
    except PermissionError:
        return jsonify({"ok": False, "error": "invalid token or not admin"}), 403
    
    player_name = data.get("player_name", "").strip()
    event_id = data.get("event_id")
    action = data.get("action")  # "rent", "eliminate", "reentry", "addon", "finalize"
    place = data.get("place")
    
    if not player_name or not event_id or not action:
        return jsonify({"ok": False, "error": "player_name, event_id and action required"}), 400
    
    # Convert event_id to int if it's a string
    try:
        event_id = int(event_id)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "invalid event_id"}), 400
    
    # Convert place to int if provided
    if place is not None:
        try:
            place = int(place)
        except (ValueError, TypeError):
            place = None
    
    try:
        with get_db() as db:
            # Check if event is poker
            event = db.execute("SELECT id, event_type FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event or event["event_type"] != "Покер":
                return jsonify({"ok": False, "error": "event is not a poker tournament"}), 400
            
            # Get or create player state
            state = db.execute("""
                SELECT * FROM tournament_player_states
                WHERE event_id = ? AND player_name = ?
            """, (event_id, player_name)).fetchone()
            
            if action == "rent":
                if state:
                    db.execute("""
                        UPDATE tournament_player_states
                        SET has_rent = 1, bonus_points = bonus_points + 100, updated_at = CURRENT_TIMESTAMP
                        WHERE event_id = ? AND player_name = ?
                    """, (event_id, player_name))
                else:
                    db.execute("""
                        INSERT INTO tournament_player_states
                        (event_id, player_name, has_rent, bonus_points)
                        VALUES (?, ?, 1, 100)
                    """, (event_id, player_name))
            
            elif action == "eliminate":
                if state:
                    db.execute("""
                        UPDATE tournament_player_states
                        SET is_eliminated = 1, updated_at = CURRENT_TIMESTAMP
                        WHERE event_id = ? AND player_name = ?
                    """, (event_id, player_name))
                else:
                    db.execute("""
                        INSERT INTO tournament_player_states
                        (event_id, player_name, is_eliminated)
                        VALUES (?, ?, 1)
                    """, (event_id, player_name))
            
            elif action == "reentry":
                if state:
                    db.execute("""
                        UPDATE tournament_player_states
                        SET reentry_count = reentry_count + 1,
                            bonus_points = bonus_points + 100,
                            is_eliminated = 0,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE event_id = ? AND player_name = ?
                    """, (event_id, player_name))
                else:
                    db.execute("""
                        INSERT INTO tournament_player_states
                        (event_id, player_name, reentry_count, bonus_points, is_eliminated)
                        VALUES (?, ?, 1, 100, 0)
                    """, (event_id, player_name))
            
            elif action == "addon":
                if state:
                    db.execute("""
                        UPDATE tournament_player_states
                        SET addon_count = addon_count + 1,
                            bonus_points = bonus_points + 100,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE event_id = ? AND player_name = ?
                    """, (event_id, player_name))
                else:
                    db.execute("""
                        INSERT INTO tournament_player_states
                        (event_id, player_name, addon_count, bonus_points)
                        VALUES (?, ?, 1, 100)
                    """, (event_id, player_name))
            
            elif action == "finalize":
                if not place:
                    return jsonify({"ok": False, "error": "place required for finalize"}), 400
                if state:
                    db.execute("""
                        UPDATE tournament_player_states
                        SET final_place = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE event_id = ? AND player_name = ?
                    """, (place, event_id, player_name))
                else:
                    db.execute("""
                        INSERT INTO tournament_player_states
                        (event_id, player_name, final_place)
                        VALUES (?, ?, ?)
                    """, (event_id, player_name, place))
            
            return jsonify({"ok": True})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/poker-tournament/<date>/finalize", methods=["POST"])
def api_finalize_poker_tournament(date):
    """Finalize poker tournament and calculate points based on places."""
    data = request.get_json() or {}
    try:
        require_admin(data)
    except PermissionError:
        return jsonify({"ok": False, "error": "invalid token or not admin"}), 403
    
    event_id = data.get("event_id")
    if not event_id:
        return jsonify({"ok": False, "error": "event_id required"}), 400
    
    # Points table based on place
    PLACE_POINTS = {
        1: 253, 2: 176, 3: 121, 4: 99, 5: 88, 6: 77, 7: 66, 8: 55, 9: 33, 10: 11
    }
    
    try:
        with get_db() as db:
            # Get all players with final places
            players = db.execute("""
                SELECT player_name, final_place, bonus_points
                FROM tournament_player_states
                WHERE event_id = ? AND final_place IS NOT NULL
                ORDER BY final_place
            """, (event_id,)).fetchall()
            
            if not players:
                return jsonify({"ok": False, "error": "No players with final places"}), 400
            
            # Get tournament for this month
            event = db.execute("SELECT date FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return jsonify({"ok": False, "error": "Event not found"}), 404
            
            event_date = datetime.strptime(event["date"], "%Y-%m-%d")
            month_name = "November" if event_date.month == 11 else "December"
            year = event_date.year
            
            tournament = db.execute("""
                SELECT id FROM tournaments WHERE month = ? AND year = ?
            """, (month_name, year)).fetchone()
            
            if not tournament:
                return jsonify({"ok": False, "error": "Tournament not found for this month"}), 404
            
            tournament_id = tournament["id"]
            day_number = event_date.day
            
            # Calculate and save points
            for player in players:
                place = player["final_place"]
                bonus_points = player["bonus_points"] or 0
                place_points = PLACE_POINTS.get(place, 0)
                # Bonus points (+100 for rent, reentry, addon) are added to place points
                total_points = place_points + bonus_points
                
                # Get or create player in players table using telegram_id
                # First try to get telegram_id from event_registrations
                telegram_id = None
                if player.get("telegram_id"):
                    telegram_id = player["telegram_id"]
                else:
                    # Try to get telegram_id from event_registrations by player_name
                    reg = db.execute("""
                        SELECT telegram_id FROM event_registrations 
                        WHERE event_id = ? AND player_name = ? AND telegram_id IS NOT NULL
                        LIMIT 1
                    """, (event_id, player["player_name"])).fetchone()
                    if reg and reg["telegram_id"]:
                        telegram_id = reg["telegram_id"]
                
                # Get or create player by telegram_id (primary) or name (fallback)
                if telegram_id:
                    player_row = db.execute("SELECT id FROM players WHERE telegram_id = ?", (telegram_id,)).fetchone()
                    if not player_row:
                        # Create new player with telegram_id
                        cursor = db.execute("INSERT INTO players (name, telegram_id) VALUES (?, ?)", (player["player_name"], telegram_id))
                        player_id = cursor.lastrowid
                    else:
                        player_id = player_row["id"]
                        # Update name if changed
                        db.execute("UPDATE players SET name = ? WHERE id = ?", (player["player_name"], player_id))
                else:
                    # Fallback: use name if telegram_id not available
                    player_row = db.execute("SELECT id FROM players WHERE name = ? AND telegram_id IS NULL", (player["player_name"],)).fetchone()
                    if not player_row:
                        cursor = db.execute("INSERT INTO players (name) VALUES (?)", (player["player_name"],))
                        player_id = cursor.lastrowid
                    else:
                        player_id = player_row["id"]
                
                # Save points to tournament_results
                db.execute("""
                    INSERT OR REPLACE INTO tournament_results
                    (tournament_id, player_id, game_number, score)
                    VALUES (?, ?, ?, ?)
                """, (tournament_id, player_id, day_number, total_points))
            
            socketio.emit("tournament_update", {"tournament_id": tournament_id})
            return jsonify({"ok": True, "message": "Tournament finalized"})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500




RATING_RULES = [
    {"place": "1 место", "points": 120},
    {"place": "2 место", "points": 100},
    {"place": "3 место", "points": 80},
    {"place": "4–10 место", "points": 60},
    {"place": "11–15 место", "points": 40},
]


def calculate_points(place: int) -> int:
    """Calculate points based on place."""
    if place == 1:
        return 120
    elif place == 2:
        return 100
    elif place == 3:
        return 80
    elif 4 <= place <= 10:
        return 60
    elif 11 <= place <= 15:
        return 40
    else:
        return 20  # For places 16+


players_lock = threading.Lock()
players = []  # List of {"name": str, "place": int, "points": int}


def update_players_from_list(player_list: list):
    """Update players from a list of names with their places."""
    global players
    with players_lock:
        players = []
        for idx, name in enumerate(player_list, start=1):
            if name.strip():
                points = calculate_points(idx)
                players.append({
                    "name": name.strip(),
                    "place": idx,
                    "points": points
                })
        # Sort by points descending, then by place ascending
        players.sort(key=lambda x: (-x["points"], x["place"]))


def get_rating_data():
    """Get current rating data sorted by points."""
    with players_lock:
        return [p.copy() for p in players]


@app.route("/php.php")
def php_proxy():
    action = request.args.get("action")
    if action == "rating_rules":
        return jsonify({"ok": True, "rules": RATING_RULES})
    return jsonify({"ok": False, "error": "unknown action"})


@app.route("/api/telegram/register", methods=["POST"])
def api_register_telegram_user():
    """Register Telegram user for mailing list."""
    data = request.get_json() or {}
    telegram_id = data.get("telegram_id", "").strip()
    first_name = data.get("first_name", "").strip()
    last_name = data.get("last_name", "").strip()
    username = data.get("username", "").strip()
    language_code = data.get("language_code", "").strip()
    is_bot = data.get("is_bot", False)
    registration_source = data.get("registration_source", "telegram_widget")
    
    print(f"=== Register Telegram User ===")
    print(f"telegram_id: {telegram_id}")
    print(f"first_name: {first_name}")
    print(f"username: {username}")
    print(f"registration_source: {registration_source}")
    
    if not telegram_id or not first_name:
        print(f"ERROR: Missing required fields - telegram_id: {bool(telegram_id)}, first_name: {bool(first_name)}")
        return jsonify({"ok": False, "error": "telegram_id and first_name required"}), 400
    
    try:
        with get_db() as db:
            # Ensure telegram_users table exists
            try:
                db.execute("SELECT 1 FROM telegram_users LIMIT 1")
            except sqlite3.OperationalError:
                print("telegram_users table does not exist, creating it...")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        telegram_id TEXT NOT NULL UNIQUE,
                        first_name TEXT NOT NULL,
                        last_name TEXT,
                        username TEXT,
                        language_code TEXT,
                        is_bot BOOLEAN DEFAULT 0,
                        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        registration_source TEXT DEFAULT 'telegram_widget',
                        offer_accepted BOOLEAN DEFAULT 0,
                        offer_accepted_at TIMESTAMP,
                        game_nickname TEXT
                    )
                """)
                db.commit()
                print("telegram_users table created successfully")
            
            # Check if user exists to preserve offer_accepted status
            existing = db.execute("SELECT offer_accepted, game_nickname FROM telegram_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            
            if existing:
                print(f"User {telegram_id} already exists, updating...")
                # Update user but preserve offer_accepted and game_nickname
                db.execute("""
                    UPDATE telegram_users 
                    SET first_name = ?, last_name = ?, username = ?, language_code = ?, 
                        is_bot = ?, registration_source = ?, last_active = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                """, (first_name, last_name or None, username or None, language_code or None, is_bot, registration_source, telegram_id))
                # Access Row object correctly
                offer_accepted = existing["offer_accepted"] if existing["offer_accepted"] else False
                game_nickname = existing["game_nickname"] if existing["game_nickname"] else None
                print(f"User updated successfully. offer_accepted: {offer_accepted}, game_nickname: {game_nickname}")
            else:
                print(f"New user {telegram_id}, inserting...")
                # New user
                db.execute("""
                    INSERT INTO telegram_users 
                    (telegram_id, first_name, last_name, username, language_code, is_bot, registration_source, last_active, offer_accepted)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0)
                """, (telegram_id, first_name, last_name or None, username or None, language_code or None, is_bot, registration_source))
                offer_accepted = False
                game_nickname = None
                print(f"User inserted successfully")
            
            db.commit()
            print(f"✅ User {telegram_id} registered/updated in database")
        
        return jsonify({
            "ok": True, 
            "message": "User registered successfully",
            "offer_accepted": offer_accepted,
            "game_nickname": game_nickname
        })
    except Exception as e:
        print(f"❌ ERROR registering user: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/accept-offer", methods=["POST"])
def api_accept_offer():
    """Accept public offer - saves to database so it's accepted only once."""
    data = request.get_json() or {}
    telegram_id = data.get("telegram_id", "").strip()
    
    print(f"=== Accept Offer ===")
    print(f"telegram_id: {telegram_id}")
    
    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id required"}), 400
    
    try:
        with get_db() as db:
            # Check if user exists first
            user = db.execute("SELECT telegram_id FROM telegram_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if not user:
                print(f"❌ User {telegram_id} not found in database")
                return jsonify({"ok": False, "error": "user not found"}), 404
            
            # Check if offer already accepted
            existing = db.execute("SELECT offer_accepted FROM telegram_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if existing and existing["offer_accepted"]:
                print(f"✅ Offer already accepted for user {telegram_id}")
                return jsonify({"ok": True, "message": "Offer already accepted", "already_accepted": True})
            
            # Update offer_accepted in database
            cursor = db.execute("""
                UPDATE telegram_users 
                SET offer_accepted = 1, offer_accepted_at = CURRENT_TIMESTAMP, last_active = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            """, (telegram_id,))
            
            db.commit()
            print(f"✅ Offer accepted and saved to database for user {telegram_id}")
        
        return jsonify({"ok": True, "message": "Offer accepted successfully"})
    except Exception as e:
        print(f"❌ ERROR accepting offer: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/user-status", methods=["GET"])
def api_get_user_status():
    """Get user status (offer accepted, game_nickname)."""
    telegram_id = request.args.get("telegram_id", "").strip()
    
    print(f"=== Get User Status ===")
    print(f"telegram_id: {telegram_id}")
    
    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id required"}), 400
    
    try:
        with get_db() as db:
            user = db.execute("""
                SELECT offer_accepted, game_nickname, first_name, last_name, username
                FROM telegram_users 
                WHERE telegram_id = ?
            """, (telegram_id,)).fetchone()
            
            if not user:
                print(f"❌ User {telegram_id} not found")
                return jsonify({"ok": False, "error": "user not found"}), 404
            
            # Access Row object correctly
            result = {
                "ok": True,
                "offer_accepted": bool(user["offer_accepted"]) if user["offer_accepted"] else False,
                "game_nickname": user["game_nickname"] if user["game_nickname"] else None,
                "first_name": user["first_name"] if user["first_name"] else "",
                "last_name": user["last_name"] if user["last_name"] else None,
                "username": user["username"] if user["username"] else None
            }
            print(f"✅ User status retrieved: {result}")
            return jsonify(result)
    except Exception as e:
        print(f"❌ ERROR getting user status: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/set-nickname", methods=["POST"])
def api_set_nickname():
    """Set game nickname - user chooses it themselves, not from Telegram."""
    data = request.get_json() or {}
    telegram_id = data.get("telegram_id", "").strip()
    game_nickname = data.get("game_nickname", "").strip()
    
    print(f"=== Set Game Nickname ===")
    print(f"telegram_id: {telegram_id}")
    print(f"game_nickname: {game_nickname}")
    
    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id required"}), 400
    
    if not game_nickname:
        return jsonify({"ok": False, "error": "game_nickname required"}), 400
    
    # Validate game_nickname: 2-20 chars, letters (lat/cyrillic), numbers, underscore, spaces
    import re
    game_nickname = game_nickname.strip()
    if len(game_nickname) < 2 or len(game_nickname) > 20:
        return jsonify({"ok": False, "error": "game_nickname должен содержать 2-20 символов"}), 400
    if not re.match(r'^[a-zA-Zа-яА-ЯёЁ0-9_\s]+$', game_nickname):
        return jsonify({"ok": False, "error": "game_nickname может содержать только буквы (лат/кирилл), цифры, пробелы и _"}), 400
    if not game_nickname.replace(' ', '').replace('_', ''):
        return jsonify({"ok": False, "error": "game_nickname не может состоять только из пробелов и подчеркиваний"}), 400
    
    try:
        with get_db() as db:
            # Check if user exists
            user = db.execute("SELECT telegram_id FROM telegram_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if not user:
                return jsonify({"ok": False, "error": "user not found"}), 404
            
            # Check if nickname is already taken
            existing = db.execute("SELECT telegram_id FROM telegram_users WHERE game_nickname = ? AND telegram_id != ?", (game_nickname, telegram_id)).fetchone()
            if existing:
                return jsonify({"ok": False, "error": "Этот никнейм уже занят"}), 400
            
            # Update game_nickname (user chooses it themselves)
            db.execute("""
                UPDATE telegram_users 
                SET game_nickname = ?, last_active = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            """, (game_nickname, telegram_id))
            
            db.commit()
            print(f"✅ Game nickname '{game_nickname}' saved to database for user {telegram_id}")
        
        return jsonify({"ok": True, "message": "Game nickname set successfully"})
    except Exception as e:
        print(f"❌ ERROR setting nickname: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/set-name", methods=["POST"])
def api_set_name():
    """Set display name for user."""
    data = request.get_json() or {}
    telegram_id = data.get("telegram_id", "").strip()
    display_name = data.get("display_name", "").strip()
    
    print(f"=== Set Display Name ===")
    print(f"telegram_id: {telegram_id}")
    print(f"display_name: {display_name}")
    
    if not telegram_id:
        return jsonify({"ok": False, "error": "telegram_id required"}), 400
    
    if not display_name:
        return jsonify({"ok": False, "error": "display_name required"}), 400
    
    # Validate display_name: 1-50 chars
    if len(display_name) < 1 or len(display_name) > 50:
        return jsonify({"ok": False, "error": "Имя должно содержать от 1 до 50 символов"}), 400
    
    try:
        with get_db() as db:
            # Check if user exists
            user = db.execute("SELECT telegram_id FROM telegram_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if not user:
                return jsonify({"ok": False, "error": "user not found"}), 404
            
            # Update display_name (store in first_name field, or create a new field)
            # For now, we'll store it in first_name
            db.execute("""
                UPDATE telegram_users 
                SET first_name = ?, last_active = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            """, (display_name, telegram_id))
            
            db.commit()
            print(f"✅ Display name '{display_name}' saved to database for user {telegram_id}")
        
        return jsonify({"ok": True, "message": "Display name set successfully"})
    except Exception as e:
        print(f"❌ ERROR setting display name: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/users", methods=["GET"])
def api_get_telegram_users():
    """Get list of registered Telegram users (admin only)."""
    token = request.args.get("token", "")
    telegram_username = request.args.get("telegram_username", "")
    try:
        require_admin({"token": token, "telegram_username": telegram_username})
    except PermissionError:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    try:
        with get_db() as db:
            users = db.execute("""
                SELECT 
                    id, telegram_id, first_name, last_name, username, 
                    language_code, is_bot, registered_at, last_active, registration_source
                FROM telegram_users
                ORDER BY registered_at DESC
            """).fetchall()
            
            result = []
            for user in users:
                result.append({
                    "id": user["id"],
                    "telegram_id": user["telegram_id"],
                    "first_name": user["first_name"],
                    "last_name": user["last_name"],
                    "username": user["username"],
                    "language_code": user["language_code"],
                    "is_bot": bool(user["is_bot"]),
                    "registered_at": user["registered_at"],
                    "last_active": user["last_active"],
                    "registration_source": user["registration_source"],
                    "telegram_link": f"https://t.me/{user['username']}" if user["username"] else f"tg://user?id={user['telegram_id']}"
                })
            
            return jsonify({"ok": True, "users": result, "count": len(result)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/users/export", methods=["GET"])
def api_export_telegram_users():
    """Export Telegram users as CSV (admin only)."""
    token = request.args.get("token", "")
    telegram_username = request.args.get("telegram_username", "")
    try:
        require_admin({"token": token, "telegram_username": telegram_username})
    except PermissionError:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    try:
        with get_db() as db:
            users = db.execute("""
                SELECT 
                    telegram_id, first_name, last_name, username, 
                    language_code, registered_at, last_active
                FROM telegram_users
                WHERE is_bot = 0
                ORDER BY registered_at DESC
            """).fetchall()
            
            import csv
            import io
            
            output = io.StringIO()
            writer = csv.writer(output)
            
            # Header
            writer.writerow(["Telegram ID", "Имя", "Фамилия", "Username", "Язык", "Дата регистрации", "Последняя активность", "Ссылка"])
            
            # Data
            for user in users:
                username = user["username"] or ""
                telegram_link = f"https://t.me/{username}" if username else f"tg://user?id={user['telegram_id']}"
                writer.writerow([
                    user["telegram_id"],
                    user["first_name"],
                    user["last_name"] or "",
                    username,
                    user["language_code"] or "",
                    user["registered_at"],
                    user["last_active"],
                    telegram_link
                ])
            
            from flask import Response
            return Response(
                output.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment; filename=telegram_users.csv"}
            )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/webhook", methods=["POST", "GET"])
def api_telegram_webhook():
    """Webhook endpoint for Telegram bot updates."""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not configured")
        return jsonify({"ok": False, "error": "bot token not configured"}), 500
    
    try:
        # Telegram sends updates as JSON in POST body
        if request.method == "POST":
            update = request.get_json()
        else:
            # GET request - return info
            return jsonify({"ok": True, "message": "Webhook endpoint is active"})
        
        if not update:
            print("No update data received")
            return jsonify({"ok": False, "error": "no data"}), 400
        
        print(f"Received Telegram update: {json.dumps(update, indent=2)}")
        
        # Handle message updates
        if "message" in update:
            message = update["message"]
            user = message.get("from")
            chat_id = message.get("chat", {}).get("id")
            
            if user and chat_id:
                telegram_id = str(user.get("id"))
                first_name = user.get("first_name", "")
                last_name = user.get("last_name", "")
                username = user.get("username", "")
                language_code = user.get("language_code", "")
                is_bot = user.get("is_bot", False)
                
                # Handle /start command - register user in the same database as website
                if message.get("text") and message["text"].startswith("/start"):
                    print(f"📥 /start command received from user: {telegram_id}, {first_name}, {username}")
                    
                    # Register user to database using the same logic as website
                    # This ensures bot and website use the same database
                    try:
                        with get_db() as db:
                            # Ensure telegram_users table exists (same as website)
                            try:
                                db.execute("SELECT 1 FROM telegram_users LIMIT 1")
                            except sqlite3.OperationalError:
                                print("telegram_users table does not exist, creating it...")
                                db.execute("""
                                    CREATE TABLE IF NOT EXISTS telegram_users (
                                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                                        telegram_id TEXT NOT NULL UNIQUE,
                                        first_name TEXT NOT NULL,
                                        last_name TEXT,
                                        username TEXT,
                                        language_code TEXT,
                                        is_bot BOOLEAN DEFAULT 0,
                                        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                        registration_source TEXT DEFAULT 'telegram_widget',
                                        offer_accepted BOOLEAN DEFAULT 0,
                                        offer_accepted_at TIMESTAMP,
                                        game_nickname TEXT
                                    )
                                """)
                                db.commit()
                                print("telegram_users table created successfully")
                            
                            # Check if user exists to preserve offer_accepted and game_nickname
                            existing = db.execute(
                                "SELECT offer_accepted, game_nickname FROM telegram_users WHERE telegram_id = ?",
                                (telegram_id,)
                            ).fetchone()
                            
                            if existing:
                                print(f"✅ User {telegram_id} already exists, updating...")
                                # Update user but preserve offer_accepted and game_nickname
                                db.execute("""
                                    UPDATE telegram_users 
                                    SET first_name = ?, last_name = ?, username = ?, language_code = ?, 
                                        is_bot = ?, registration_source = ?, last_active = CURRENT_TIMESTAMP
                                    WHERE telegram_id = ?
                                """, (first_name, last_name or None, username or None, language_code or None, is_bot, "telegram_bot", telegram_id))
                                offer_accepted = existing["offer_accepted"] if existing["offer_accepted"] else False
                                game_nickname = existing["game_nickname"] if existing["game_nickname"] else None
                                print(f"User updated successfully. offer_accepted: {offer_accepted}, game_nickname: {game_nickname}")
                            else:
                                print(f"✅ New user {telegram_id}, inserting...")
                                # New user - same structure as website registration
                                db.execute("""
                                    INSERT INTO telegram_users 
                                    (telegram_id, first_name, last_name, username, language_code, is_bot, registration_source, last_active, offer_accepted)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0)
                                """, (telegram_id, first_name, last_name or None, username or None, language_code or None, is_bot, "telegram_bot"))
                                print(f"User inserted successfully")
                            
                            db.commit()
                            print(f"✅ User {telegram_id} registered/updated in database from /start command")
                    except Exception as e:
                        print(f"❌ Error saving Telegram user from /start: {e}")
                        import traceback
                        traceback.print_exc()
                    try:
                        # Get base URL from environment or use default
                        base_url = os.environ.get("BASE_URL", "https://pulse-390031593512.europe-north1.run.app")
                        
                        welcome_text = (
                            "🎰 Добро пожаловать в PULSE | CLUB!\n\n"
                            "Вы успешно зарегистрированы в системе.\n\n"
                            "📋 Для полного доступа к функциям сайта:\n"
                            "1. Перейдите на сайт\n"
                            "2. Войдите через Telegram виджет\n"
                            "3. Примите публичную оферту\n"
                            "4. Укажите игровой никнейм\n\n"
                            "После этого вы сможете записываться на турниры и события!"
                        )
                        
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        
                        # Create inline keyboard with button to open website
                        keyboard = {
                            "inline_keyboard": [[
                                {
                                    "text": "🌐 Открыть сайт PULSE | CLUB",
                                    "url": base_url
                                }
                            ]]
                        }
                        
                        response = requests.post(url, json={
                            "chat_id": chat_id,
                            "text": welcome_text,
                            "reply_markup": keyboard,
                            "parse_mode": "HTML"
                        }, timeout=5)
                        print(f"Welcome message sent: {response.json()}")
                    except Exception as e:
                        print(f"Error sending welcome message: {e}")
        
        return jsonify({"ok": True})
    except Exception as e:
        print(f"Error processing webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/migrate-database", methods=["POST"])
def api_migrate_database():
    """Manually trigger database migration/backup (admin only)."""
    try:
        data = request.get_json() or {}
        token = data.get("token", "")
        telegram_username = data.get("telegram_username", "")
        game_nickname = data.get("game_nickname", "")
        telegram_id = data.get("telegram_id", "")
        
        # Check admin access
        try:
            require_admin({"token": token, "telegram_username": telegram_username, "game_nickname": game_nickname, "telegram_id": telegram_id})
        except PermissionError:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        
        # Perform migration
        success = migrate_database()
        
        if success:
            return jsonify({"ok": True, "message": "Database migration completed successfully"})
        else:
            return jsonify({"ok": False, "error": "Migration failed"}), 500
            
    except Exception as e:
        print(f"Error in manual migration: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/admin/download-backup", methods=["GET"])
def api_download_backup():
    """Download latest database backup (admin only)."""
    try:
        token = request.args.get("token", "")
        telegram_username = request.args.get("telegram_username", "")
        game_nickname = request.args.get("game_nickname", "")
        telegram_id = request.args.get("telegram_id", "")
        
        # Check admin access
        try:
            require_admin({"token": token, "telegram_username": telegram_username, "game_nickname": game_nickname, "telegram_id": telegram_id})
        except PermissionError:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        
        # Find latest backup
        backup_dir = os.path.join(DB_DIR, "backups")
        if not os.path.exists(backup_dir):
            return jsonify({"ok": False, "error": "No backups found"}), 404
        
        backups = []
        for filename in os.listdir(backup_dir):
            if filename.startswith("pulse_tournaments_backup_") and filename.endswith(".db"):
                filepath = os.path.join(backup_dir, filename)
                backups.append((os.path.getmtime(filepath), filepath, filename))
        
        if not backups:
            return jsonify({"ok": False, "error": "No backups found"}), 404
        
        # Get latest backup
        backups.sort(reverse=True)
        latest_backup_path = backups[0][1]
        latest_backup_filename = backups[0][2]
        
        from flask import send_file
        return send_file(
            latest_backup_path,
            as_attachment=True,
            download_name=latest_backup_filename,
            mimetype="application/x-sqlite3"
        )
        
    except Exception as e:
        print(f"Error downloading backup: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/setup-webhook", methods=["POST"])
def api_setup_webhook():
    """Setup Telegram webhook (admin only)."""
    try:
        data = request.get_json() or {}
        token = data.get("token", "")
        webhook_url = data.get("webhook_url", "").strip()
        
        print(f"Setup webhook request: token={bool(token)}, webhook_url={webhook_url}")
        
        telegram_username = data.get("telegram_username", "")
        try:
            require_admin({"token": token, "telegram_username": telegram_username})
        except PermissionError:
            print(f"Unauthorized: token mismatch or not admin")
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        
        if not TELEGRAM_BOT_TOKEN:
            print("TELEGRAM_BOT_TOKEN not configured")
            return jsonify({"ok": False, "error": "bot token not configured"}), 500
        
        if not webhook_url:
            print("webhook_url is empty")
            return jsonify({"ok": False, "error": "webhook_url required"}), 400
        
        if not REQUESTS_AVAILABLE:
            print("requests module not available")
            return jsonify({"ok": False, "error": "requests module not available. Please install it: pip install requests"}), 500
        
        # Telegram API requires GET request with URL parameter
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
        params = {"url": webhook_url}
        
        print(f"Calling Telegram API: {url} with params: {params}")
        
        try:
            response = requests.get(url, params=params, timeout=10)
            print(f"Telegram API response status: {response.status_code}")
            print(f"Telegram API response text: {response.text}")
            
            result = response.json()
            print(f"Telegram setWebhook response: {result}")
            
            if result.get("ok"):
                return jsonify({"ok": True, "result": result, "message": "Webhook configured successfully"})
            else:
                error_desc = result.get("description", "Unknown error")
                return jsonify({"ok": False, "error": error_desc, "result": result}), 400
        except requests.exceptions.RequestException as e:
            print(f"Request exception: {e}")
            return jsonify({"ok": False, "error": f"Network error: {str(e)}"}), 500
        except ValueError as e:
            print(f"JSON decode error: {e}, response text: {response.text if 'response' in locals() else 'N/A'}")
            return jsonify({"ok": False, "error": f"Invalid response from Telegram: {str(e)}"}), 500
            
    except Exception as e:
        print(f"Unexpected error in setup-webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/telegram/broadcast", methods=["POST"])
def api_telegram_broadcast():
    """Send broadcast message to all registered users (admin only)."""
    data = request.get_json() or {}
    token = data.get("token", "")
    telegram_username = data.get("telegram_username", "")
    message = data.get("message", "").strip()
    
    try:
        require_admin({"token": token, "telegram_username": telegram_username})
    except PermissionError:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"ok": False, "error": "bot token not configured"}), 500
    
    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400
    
    try:
        with get_db() as db:
            users = db.execute("""
                SELECT telegram_id FROM telegram_users
                WHERE is_bot = 0 AND telegram_id IS NOT NULL
            """).fetchall()
        
        print(f"Broadcasting to {len(users)} users")
        
        success_count = 0
        error_count = 0
        errors = []
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        
        for user in users:
            try:
                telegram_id = user["telegram_id"]
                # Skip manual registrations (they start with "manual_")
                if telegram_id.startswith("manual_"):
                    continue
                    
                response = requests.post(url, json={
                    "chat_id": int(telegram_id),
                    "text": message,
                    "parse_mode": "HTML"
                }, timeout=5)
                
                result = response.json()
                if result.get("ok"):
                    success_count += 1
                else:
                    error_count += 1
                    errors.append(f"User {telegram_id}: {result.get('description', 'unknown error')}")
            except Exception as e:
                error_count += 1
                errors.append(f"User {user.get('telegram_id', 'unknown')}: {str(e)}")
        
        return jsonify({
            "ok": True,
            "sent": success_count,
            "failed": error_count,
            "total": len(users),
            "errors": errors[:10]  # First 10 errors
        })
    except Exception as e:
        print(f"Error in broadcast: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


def require_admin(data):
    """Check admin access - either by token or Telegram username."""
    token = (data or {}).get("token", "")
    telegram_username = (data or {}).get("telegram_username", "")
    
    print(f"🔐 require_admin called: token={bool(token)}, telegram_username='{telegram_username}'")
    
    # Check token first (for backward compatibility)
    if token == ADMIN_TOKEN:
        print("✅ Admin access granted via token")
        return True
    
    # Check Telegram username
    if telegram_username:
        is_admin = check_is_admin(telegram_username)
        if is_admin:
            print("✅ Admin access granted via Telegram username")
            return True
        else:
            print(f"❌ Telegram username '{telegram_username}' is not admin")
    else:
        print("❌ No telegram_username provided")
    
    print("❌ Permission denied - invalid token or not admin")
    raise PermissionError("invalid token or not admin")


@app.route("/api/telegram/check-admin", methods=["GET"])
def api_check_admin():
    """Check if game_nickname is admin."""
    game_nickname = request.args.get("game_nickname", "").strip()
    telegram_id = request.args.get("telegram_id", "").strip()
    
    print(f"📋 /api/telegram/check-admin called: game_nickname='{game_nickname}', telegram_id='{telegram_id}'")
    
    if not game_nickname and not telegram_id:
        print("❌ No game_nickname or telegram_id provided")
        return jsonify({"ok": False, "is_admin": False, "error": "game_nickname or telegram_id required"}), 400
    
    is_admin = check_is_admin(game_nickname, telegram_id)
    result = {"ok": True, "is_admin": is_admin, "game_nickname": game_nickname, "admin_list": ADMIN_GAME_NICKNAMES}
    print(f"📋 Returning: {result}")
    return jsonify(result)


@socketio.on("connect")
def on_connect():
    with state_lock:
        emit("state", build_state())


@socketio.on("action")
def on_action(data):
    action = (data or {}).get("action")
    try:
        require_admin(data)
    except PermissionError:
        return

    handlers = {
        "toggle": handle_toggle,
        "next": handle_next,
        "reset": handle_reset,
        "reset_all": handle_reset_all,
        "config": handle_config,
        "set_players": handle_set_players,
    }
    handler = handlers.get(action)
    if handler:
        handler(data)


def handle_toggle(_data):
    with state_lock:
        state["is_running"] = not state["is_running"]
        state["last_update"] = time.time()
        payload = build_state()
    emit_state(payload)


def handle_next(_data):
    with state_lock:
        if state["is_in_break"]:
            state["is_in_break"] = False
            advance_to_next_level()
        else:
            if state["level_index"] < len(LEVELS) - 1:
                state["level_index"] += 1
            reset_stage(keep_running=True)
        payload = build_state()
    emit_state(payload)


def handle_reset(_data):
    with state_lock:
        reset_stage(keep_running=True)
        payload = build_state()
    emit_state(payload)


def handle_reset_all(_data):
    with state_lock:
        state["level_index"] = 0
        state["is_in_break"] = False
        reset_stage(keep_running=False)
        payload = build_state()
    emit_state(payload)


def handle_config(data):
    cfg = (data or {}).get("cfg", {})
    with state_lock:
        pre = int(cfg.get("preMinutes", level_config["preMinutes"]))
        post = int(cfg.get("postMinutes", level_config["postMinutes"]))
        late = int(cfg.get("lateLevels", level_config["lateLevels"]))
        level_config["preMinutes"] = max(1, min(60, pre))
        level_config["postMinutes"] = max(1, min(60, post))
        level_config["lateLevels"] = max(1, min(len(LEVELS), late))

        breaks = cfg.get("breaks") or []
        for lvl in LEVELS:
            lvl["breakMinutes"] = 0
        for entry in breaks:
            level_num = int(entry.get("level", 0))
            minutes = int(entry.get("minutes", 0))
            if 1 <= level_num <= len(LEVELS) and minutes > 0:
                LEVELS[level_num - 1]["breakMinutes"] = minutes

        if state["is_in_break"]:
            reset_stage(keep_running=True)
        else:
            total = stage_duration_seconds(state["level_index"], False)
            state["ring_total"] = total
            state["time_left"] = min(state["time_left"], total)
            state["last_update"] = time.time()
        state["version"] += 1
        payload = build_state()
    emit_state(payload)


def handle_set_players(data):
    """Handle setting players list via socket."""
    player_list = (data or {}).get("players", [])
    if isinstance(player_list, list):
        update_players_from_list(player_list)
        socketio.emit("rating_update", {"players": get_rating_data()})


# Initialize with default players
default_players = [
    "13 reason for",
    "ANDREYU",
    "Abrasha",
    "tolch__",
    "Artem",
    "Art",
    "Fish2005",
    "St05",
    "Винни",
    "Psychoanya",
    "kolyupaska",
    "apheristka",
    "Livinsl",
    "SergeyKoller",
    "TanyaKoller",
    "dombrovich",
]
update_players_from_list(default_players)


# Initialize database on startup (with error handling)
try:
    init_db()
    print("Database initialized successfully")
except Exception as e:
    print(f"Error initializing database: {e}")
    import traceback
    traceback.print_exc()

# Start timer thread (with error handling)
try:
    timer_thread = threading.Thread(target=timer_loop, daemon=True)
    timer_thread.start()
    print("Timer thread started successfully")
except Exception as e:
    print(f"Error starting timer thread: {e}")
    import traceback
    traceback.print_exc()

def migrate_database():
    """Perform database migration/backup to prevent data loss."""
    try:
        print(f"🔄 Starting database migration at {datetime.now()}")
        
        # Create backup directory if it doesn't exist
        backup_dir = os.path.join(DB_DIR, "backups")
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir, exist_ok=True)
        
        # Create backup filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"pulse_tournaments_backup_{timestamp}.db"
        backup_path = os.path.join(backup_dir, backup_filename)
        
        # Copy database file to backup
        if os.path.exists(DB_PATH):
            shutil.copy2(DB_PATH, backup_path)
            print(f"✅ Database backup created: {backup_path}")
            
            # Perform VACUUM to optimize database
            with get_db() as db:
                db.execute("VACUUM")
                db.commit()
            print(f"✅ Database optimized (VACUUM)")
            
            # Clean old backups (keep last 7 days)
            if os.path.exists(backup_dir):
                now = time.time()
                for filename in os.listdir(backup_dir):
                    filepath = os.path.join(backup_dir, filename)
                    if os.path.isfile(filepath) and filename.startswith("pulse_tournaments_backup_"):
                        # Delete backups older than 7 days
                        if now - os.path.getmtime(filepath) > 7 * 24 * 3600:
                            os.remove(filepath)
                            print(f"🗑️ Deleted old backup: {filename}")
            
            # Send notification to admin via Telegram bot
            send_migration_notification(success=True, backup_path=backup_path)
            
            return True
        else:
            print(f"❌ Database file not found: {DB_PATH}")
            send_migration_notification(success=False, error="Database file not found")
            return False
            
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Error during database migration: {error_msg}")
        import traceback
        traceback.print_exc()
        send_migration_notification(success=False, error=error_msg)
        return False


def send_migration_notification(success=True, backup_path=None, error=None):
    """Send Telegram notification about migration status to admin."""
    if not TELEGRAM_BOT_TOKEN or not REQUESTS_AVAILABLE:
        print("⚠️ Cannot send migration notification: TELEGRAM_BOT_TOKEN or requests not available")
        return
    
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if success:
            message = (
                f"✅ Миграция базы данных проведена успешно\n\n"
                f"📅 Дата и время: {timestamp}\n"
                f"💾 Резервная копия создана\n"
                f"🔄 База данных оптимизирована (VACUUM)\n\n"
                f"Все данные сохранены и защищены от потери."
            )
        else:
            error_text = error or "Неизвестная ошибка"
            message = (
                f"❌ Ошибка при миграции базы данных\n\n"
                f"📅 Дата и время: {timestamp}\n"
                f"⚠️ Ошибка: {error_text}\n\n"
                f"Требуется проверка системы."
            )
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(url, json={
            "chat_id": ADMIN_TELEGRAM_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        
        if response.status_code == 200:
            print(f"✅ Migration notification sent to admin (telegram_id: {ADMIN_TELEGRAM_ID})")
        else:
            print(f"⚠️ Failed to send migration notification: {response.status_code} - {response.text}")
            
    except Exception as e:
        print(f"❌ Error sending migration notification: {e}")


def schedule_daily_migration():
    """Schedule daily database migration (every 24 hours)."""
    def migration_worker():
        while True:
            try:
                # Wait 24 hours (86400 seconds)
                time.sleep(24 * 60 * 60)
                migrate_database()
            except Exception as e:
                print(f"❌ Error in migration worker: {e}")
                import traceback
                traceback.print_exc()
                # Continue even if there's an error
                time.sleep(60)  # Wait 1 minute before retrying
    
    migration_thread = threading.Thread(target=migration_worker, daemon=True)
    migration_thread.start()
    print("✅ Daily database migration scheduler started (every 24 hours)")
    
    # Perform initial migration on startup (after 1 minute delay)
    def initial_migration():
        time.sleep(60)  # Wait 1 minute after startup
        migrate_database()
    
    initial_thread = threading.Thread(target=initial_migration, daemon=True)
    initial_thread.start()


def kill_existing_port(port=8000):
    # Убиваем все процессы, использующие порт
    try:
        output = subprocess.check_output(
            f"lsof -t -i :{port}", shell=True, encoding='utf8'
        )
        pids = [int(pid) for pid in output.strip().split('\n') if pid]
        for pid in pids:
            print(f"Killing process {pid} on port {port}")
            os.kill(pid, signal.SIGKILL)
    except subprocess.CalledProcessError:
        pass  # Никто порт не слушает

# Start daily database migration scheduler
try:
    schedule_daily_migration()
except Exception as e:
    print(f"Error starting migration scheduler: {e}")
    import traceback
    traceback.print_exc()

# Don't kill port on production (Amvera) or when running as module
if os.environ.get("AMVERA") != "true" and __name__ == "__main__":
    kill_existing_port(8000)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)

