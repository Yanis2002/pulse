"""
WSGI entry point for Cloud Run/Amvera/GAE deployment
PRODUCTION VERSION ONLY - uses app.py (not app_local.py)
"""
import os
import sys

# Ensure we're in production mode
# Cloud Run sets K_SERVICE, but we'll also set LOCAL_MODE=false explicitly
os.environ["LOCAL_MODE"] = "false"

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

print("=" * 60)
print("🌐 Starting PRODUCTION WSGI application...")
print("=" * 60)
print(f"Python version: {sys.version}")
print(f"Working directory: {os.getcwd()}")

# Detect platform
platform = "Unknown"
if os.environ.get("K_SERVICE"):
    platform = "Cloud Run"
elif os.environ.get("AMVERA"):
    platform = "Amvera"
elif os.environ.get("GAE_ENV"):
    platform = "Google App Engine"
    
print(f"Platform: {platform}")
print(f"Environment: PRODUCTION")

try:
    # Import app and socketio - this will trigger initialization
    # IMPORTANT: Import from app.py, NOT app_local.py
    print("Importing app module (production)...")
    from app import app, socketio
    print("✅ App module imported successfully")
    
    # For gunicorn with eventlet, we need to use the app directly
    # SocketIO will work through the eventlet worker
    application = app
    print("✅ WSGI application created successfully")
    print("=" * 60)
    
except Exception as e:
    print(f"❌ Error importing app: {e}")
    import traceback
    traceback.print_exc()
    raise

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)

