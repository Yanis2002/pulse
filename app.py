import os
import threading
import time
import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from contextlib import contextmanager
import signal
import subprocess

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit


ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "local-admin")

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
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

DB_PATH = "pulse_tournaments.db"


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
        
        # Players table
        db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


@app.route("/")
def index():
    """Main dashboard page with splash screen."""
    return render_template("dashboard.html", is_admin=True, admin_token=ADMIN_TOKEN)


@app.route("/timer")
def timer():
    """Timer page."""
    return render_template("index.html", is_admin=True, admin_token=ADMIN_TOKEN)


@app.route("/rating")
def rating():
    return render_template("rating.html", is_admin=True, admin_token=ADMIN_TOKEN)

@app.route("/contacts")
def contacts():
    return render_template("contacts.html")


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
    token = data.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    
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
    token = data.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    
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
    token = data.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    
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
                SELECT e.*, 
                       COALESCE(GROUP_CONCAT(er.player_name, '|'), '') as registered_players,
                       COALESCE(GROUP_CONCAT(er.telegram_username, '|'), '') as telegram_usernames,
                       COUNT(er.id) as registration_count
                FROM events e
                LEFT JOIN event_registrations er ON e.id = er.event_id
                WHERE e.date >= ? AND e.date <= ?
                GROUP BY e.id
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
                
                result.append({
                    "id": event["id"],
                    "date": event["date"],
                    "time": event["time"],
                    "event_type": event["event_type"],
                    "description": event["description"] or "",
                    "registered": registered,
                    "telegram_users": telegram_users,
                    "registration_count": event["registration_count"] or 0,
                    "is_registered": is_user_registered
                })
            
            return jsonify({"ok": True, "events": result})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "traceback": traceback.format_exc()}), 500


@app.route("/api/events", methods=["POST"])
def api_create_event():
    """Create a new event (admin only)."""
    data = request.get_json() or {}
    token = data.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    
    date = data.get("date", "").strip()
    time = data.get("time", "").strip()
    event_type = data.get("event_type", "").strip()
    description = data.get("description", "").strip()
    
    if not date or not time or not event_type:
        return jsonify({"ok": False, "error": "date, time and event_type required"}), 400
    
    if event_type not in ["Мафия", "Покер", "Свободная игра"]:
        return jsonify({"ok": False, "error": "invalid event_type"}), 400
    
    try:
        with get_db() as db:
            cursor = db.execute("""
                INSERT INTO events (date, time, event_type, description)
                VALUES (?, ?, ?, ?)
            """, (date, time, event_type, description))
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
    
    if not player_name:
        return jsonify({"ok": False, "error": "player_name required"}), 400
    
    if not telegram_username and not telegram_id:
        return jsonify({"ok": False, "error": "telegram_username or telegram_id required"}), 400
    
    try:
        with get_db() as db:
            # Check if event exists
            event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return jsonify({"ok": False, "error": "event not found"}), 404
            
            # Register
            db.execute("""
                INSERT INTO event_registrations (event_id, player_name, telegram_username, telegram_id)
                VALUES (?, ?, ?, ?)
            """, (event_id, player_name, telegram_username or None, telegram_id or None))
        
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
                    "description": event["description"]
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
    token = data.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    
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
    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    
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
    token = data.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    
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
    token = data.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    
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
                
                # Get or create player in players table
                player_row = db.execute("SELECT id FROM players WHERE name = ?", (player["player_name"],)).fetchone()
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


def require_admin(data):
    token = (data or {}).get("token", "")
    if token != ADMIN_TOKEN:
        raise PermissionError("invalid token")


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


timer_thread = threading.Thread(target=timer_loop, daemon=True)
timer_thread.start()

# Initialize database on startup
init_db()

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

# Don't kill port on production (Amvera)
if os.environ.get("AMVERA") != "true":
    kill_existing_port(8000)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)

