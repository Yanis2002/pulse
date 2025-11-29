"""
Local version of the application for development/testing via tunneling.
This version uses a separate database and configuration.

⚠️ IMPORTANT: This file is for LOCAL DEVELOPMENT ONLY.
It will NOT be deployed to production servers.
"""
import os
import sys

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Prevent running in production environments (Cloud Run, Amvera, GAE)
is_production = (
    os.environ.get("AMVERA") == "true" or
    os.environ.get("GAE_ENV") or
    os.environ.get("K_SERVICE") or  # Cloud Run
    os.environ.get("K_REVISION") or  # Cloud Run
    os.environ.get("K_CONFIGURATION")  # Cloud Run
)

if is_production:
    platform = "Cloud Run" if os.environ.get("K_SERVICE") else ("Amvera" if os.environ.get("AMVERA") else "GAE")
    print(f"❌ ERROR: app_local.py should not be run in production ({platform})!")
    print("   Use app.py or wsgi.py for production deployment.")
    sys.exit(1)

# Set environment variables for local version
os.environ["LOCAL_MODE"] = "true"
local_db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_db")
os.environ["DB_DIR"] = local_db_dir
os.environ["PORT"] = "8000"

# Import main app from parent directory
from app import app, socketio

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print("=" * 60)
    print("🚀 LOCAL DEVELOPMENT VERSION")
    print("=" * 60)
    print(f"📁 Local database directory: {os.environ.get('DB_DIR')}")
    print(f"🌐 Server: http://localhost:{port}")
    print(f"🔗 Use tunneling service for public access")
    print("=" * 60)
    print("⚠️  This is NOT the production version!")
    print("=" * 60)
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True, debug=True)

