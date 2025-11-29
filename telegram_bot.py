"""
Telegram Bot Module
Handles all Telegram bot functionality while sharing the database with the main application.
"""
import os
import json
import sqlite3
from datetime import datetime

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    print("WARNING: requests module not found. Telegram bot features will not work.")
    REQUESTS_AVAILABLE = False
    requests = None

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8574583723:AAHGnyANIA7z_7yPftV1q_HBoYWH4XkMVnI")

# Admin telegram_id for migration notifications
ADMIN_TELEGRAM_ID = os.environ.get("ADMIN_TELEGRAM_ID", "463639949")

# Base URL for Web App
BASE_URL = os.environ.get("BASE_URL", "https://pulse-390031593512.europe-north1.run.app")


def send_message(chat_id, text, reply_markup=None, parse_mode="HTML", timeout=10):
    """
    Send a message via Telegram Bot API.
    
    Args:
        chat_id: Telegram chat ID
        text: Message text
        reply_markup: Optional keyboard markup
        parse_mode: HTML or Markdown
        timeout: Request timeout
    
    Returns:
        dict: Response from Telegram API
    """
    if not TELEGRAM_BOT_TOKEN or not REQUESTS_AVAILABLE:
        print("⚠️ Cannot send message: TELEGRAM_BOT_TOKEN or requests not available")
        return {"ok": False, "error": "bot not configured"}
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        
        if reply_markup:
            payload["reply_markup"] = reply_markup
        
        response = requests.post(url, json=payload, timeout=timeout)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("ok"):
                print(f"✅ Message sent successfully to {chat_id}")
                return result
            else:
                print(f"⚠️ Failed to send message: {result}")
                return result
        else:
            print(f"❌ Error sending message: HTTP {response.status_code} - {response.text}")
            return {"ok": False, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        print(f"❌ Error sending message: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def handle_start_command(telegram_id, first_name, last_name, username, language_code, is_bot, chat_id, get_db_func):
    """
    Handle /start command from user.
    Registers/updates user in database and sends welcome message.
    
    Args:
        telegram_id: User's Telegram ID
        first_name: User's first name
        last_name: User's last name
        username: User's Telegram username
        language_code: User's language code
        is_bot: Whether user is a bot
        chat_id: Chat ID for sending message
        get_db_func: Function to get database connection (from main app)
    
    Returns:
        bool: True if successful, False otherwise
    """
    print(f"📥 /start command received from user: {telegram_id}, {first_name}, {username}")
    
    # Register user to database using the same logic as website
    try:
        with get_db_func() as db:
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
        return False
    
    # Send welcome message
    try:
        welcome_text = (
            "🎰 Добро пожаловать в PULSE | CLUB!\n\n"
            "Это бот для записи на турниры по покеру в Санкт-Петербурге.\n\n"
            "📋 Для записи на турниры:\n"
            "1. Откройте сайт через кнопку ниже\n"
            "2. Примите публичную оферту\n"
            "3. Укажите игровой никнейм\n\n"
            "После этого вы сможете записываться на турниры и получать уведомления о подтверждении регистрации!"
        )
        
        # Create inline keyboard with Web App button for auto-authorization
        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "🌐 Открыть сайт PULSE | CLUB",
                    "web_app": {"url": BASE_URL}
                }
            ]]
        }
        
        result = send_message(chat_id, welcome_text, reply_markup=keyboard)
        
        if not result.get("ok"):
            # Try to send a simple error message
            send_message(chat_id, "❌ Произошла ошибка при отправке сообщения. Попробуйте позже.", timeout=5)
            return False
        
        return True
    except Exception as e:
        print(f"❌ Error sending welcome message: {e}")
        import traceback
        traceback.print_exc()
        return False


def send_tournament_registration_confirmation(telegram_id, event):
    """
    Send tournament registration confirmation message to user via Telegram bot.
    
    Args:
        telegram_id: User's Telegram ID
        event: Event dictionary with date, time, event_type, description
    """
    if not TELEGRAM_BOT_TOKEN or not REQUESTS_AVAILABLE:
        print("⚠️ Cannot send registration confirmation: TELEGRAM_BOT_TOKEN or requests not available")
        return
    
    try:
        # Format date from YYYY-MM-DD to DD month name
        event_date = event.get("date", "")
        event_time = event.get("time", "")
        event_type = event.get("event_type", "")
        description = event.get("description", "")
        
        # Parse date
        date_obj = None
        try:
            date_obj = datetime.strptime(event_date, "%Y-%m-%d")
            day = date_obj.day
            month_names = {
                1: "января", 2: "февраля", 3: "марта", 4: "апреля",
                5: "мая", 6: "июня", 7: "июля", 8: "августа",
                9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
            }
            month_name = month_names.get(date_obj.month, "")
            formatted_date = f"{day} {month_name}"
        except:
            formatted_date = event_date
        
        # Format time
        try:
            time_obj = datetime.strptime(event_time, "%H:%M")
            formatted_time = time_obj.strftime("%H:%M")
        except:
            formatted_time = event_time
        
        # Format tournament name
        if date_obj and event_type:
            tournament_name = f"{event_type} — {date_obj.strftime('%d.%m')} {formatted_time}"
        else:
            tournament_name = description or "Турнир"
        
        # Build confirmation message
        message = (
            "✅Ваша регистрация на турнир подтверждена! ✅\n\n"
            f"▪️ 🗓 Дата: {formatted_date}\n\n"
            f"▪️ ⏰ Начало: {formatted_time}\n\n"
            f"▪️ 🏆 Турнир: {tournament_name}\n\n"
            "📍 Адрес: СПБ, улица Восстания, 15С\n\n"
            "🧭 Как пройти: https://yandex.ru/maps/-/CLW~qQKs\n\n"
            "⏰ Поздняя регистрация и ре-энтри открыты до 20:30:00\n\n"
            "🔺 (это время, до которого можно присоединиться к турниру)\n\n"
            "⚠️Правила ответственного бронирования:\n\n"
            "🔺 Предупредите об отмене минимум за 2 часа для того чтобы слоты не пропадали — иначе в следующий раз запись по предоплате, проявляйте уважение к другим участникам клуба.\n\n"
            "❗️Важно: Играем не на деньги. Призы не предусмотрены. 18+\n\n"
            "🔺 Оплата производится за аренду инвентаря картой или QR-кодом\n\n"
            "🔺 Оплата наличными невозможна\n\n"
            "Остались вопросы? Поддержка 24/7"
        )
        
        send_message(telegram_id, message)
        
    except Exception as e:
        print(f"❌ Error sending registration confirmation: {e}")
        import traceback
        traceback.print_exc()


def send_migration_notification(success=True, backup_path=None, error=None):
    """
    Send database migration notification to admin via Telegram bot.
    
    Args:
        success: Whether migration was successful
        backup_path: Path to backup file (if successful)
        error: Error message (if failed)
    """
    if not TELEGRAM_BOT_TOKEN or not REQUESTS_AVAILABLE:
        print("⚠️ Cannot send migration notification: TELEGRAM_BOT_TOKEN or requests not available")
        return
    
    try:
        if success:
            message = (
                "✅ Миграция базы данных выполнена успешно!\n\n"
                f"📦 Бэкап создан: {backup_path if backup_path else 'N/A'}\n\n"
                "База данных оптимизирована и готова к работе."
            )
        else:
            message = (
                "❌ Ошибка при миграции базы данных!\n\n"
                f"Ошибка: {error if error else 'Неизвестная ошибка'}\n\n"
                "Проверьте логи сервера для подробностей."
            )
        
        send_message(ADMIN_TELEGRAM_ID, message)
        
    except Exception as e:
        print(f"❌ Error sending migration notification: {e}")
        import traceback
        traceback.print_exc()


def process_webhook_update(update, get_db_func):
    """
    Process incoming webhook update from Telegram.
    
    Args:
        update: Telegram update dictionary
        get_db_func: Function to get database connection (from main app)
    
    Returns:
        dict: Result of processing
    """
    if not update:
        return {"ok": False, "error": "no update data"}
    
    print(f"📨 Processing Telegram update: {json.dumps(update, indent=2)}")
    
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
            
            # Handle /start command
            if message.get("text") and message["text"].startswith("/start"):
                success = handle_start_command(
                    telegram_id, first_name, last_name, username,
                    language_code, is_bot, chat_id, get_db_func
                )
                return {"ok": success}
    
    return {"ok": True}


def get_webhook_info():
    """
    Get current webhook information from Telegram API.
    
    Returns:
        dict: Webhook info from Telegram API
    """
    if not TELEGRAM_BOT_TOKEN or not REQUESTS_AVAILABLE:
        return {"ok": False, "error": "bot not configured"}
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo"
        response = requests.get(url, timeout=5)
        return response.json()
    except Exception as e:
        print(f"❌ Error getting webhook info: {e}")
        return {"ok": False, "error": str(e)}


def setup_webhook(webhook_url):
    """
    Setup Telegram webhook.
    
    Args:
        webhook_url: URL for webhook endpoint
    
    Returns:
        dict: Result from Telegram API
    """
    if not TELEGRAM_BOT_TOKEN or not REQUESTS_AVAILABLE:
        return {"ok": False, "error": "bot not configured"}
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
        params = {"url": webhook_url}
        
        print(f"Calling Telegram API: {url} with params: {params}")
        
        response = requests.get(url, params=params, timeout=10)
        print(f"Telegram API response status: {response.status_code}")
        print(f"Telegram API response text: {response.text}")
        
        result = response.json()
        print(f"Telegram setWebhook response: {result}")
        
        return result
    except Exception as e:
        print(f"❌ Error setting webhook: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def broadcast_message(message, get_db_func):
    """
    Send broadcast message to all registered users.
    
    Args:
        message: Message text to send
        get_db_func: Function to get database connection (from main app)
    
    Returns:
        dict: Result with success/failure counts
    """
    if not TELEGRAM_BOT_TOKEN or not REQUESTS_AVAILABLE:
        return {"ok": False, "error": "bot not configured"}
    
    if not message:
        return {"ok": False, "error": "message required"}
    
    try:
        with get_db_func() as db:
            users = db.execute("""
                SELECT telegram_id FROM telegram_users
                WHERE is_bot = 0 AND telegram_id IS NOT NULL
            """).fetchall()
        
        print(f"Broadcasting to {len(users)} users")
        
        success_count = 0
        error_count = 0
        errors = []
        
        for user in users:
            try:
                telegram_id = user["telegram_id"]
                # Skip manual registrations (they start with "manual_")
                if telegram_id.startswith("manual_"):
                    continue
                
                result = send_message(int(telegram_id), message, timeout=5)
                
                if result.get("ok"):
                    success_count += 1
                else:
                    error_count += 1
                    errors.append(f"User {telegram_id}: {result.get('description', 'unknown error')}")
            except Exception as e:
                error_count += 1
                errors.append(f"User {user.get('telegram_id', 'unknown')}: {str(e)}")
        
        return {
            "ok": True,
            "sent": success_count,
            "failed": error_count,
            "total": len(users),
            "errors": errors[:10]  # First 10 errors
        }
    except Exception as e:
        print(f"❌ Error in broadcast: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}

