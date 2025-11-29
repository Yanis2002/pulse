"""
Local version of the application for development/testing via tunneling.
This version uses a separate database and configuration.
"""
import os
import sys

# Set environment variables for local version
os.environ["LOCAL_MODE"] = "true"
os.environ["DB_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_db")
os.environ["PORT"] = "8000"

# Import main app
from app import app, socketio

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"🚀 Starting LOCAL version on port {port}")
    print(f"📁 Local database directory: {os.environ.get('DB_DIR')}")
    print(f"🌐 Access via tunnel: http://localhost:{port}")
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True, debug=True)

