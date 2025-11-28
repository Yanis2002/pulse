"""
WSGI entry point for Amvera deployment
"""
import os
from app import app, socketio

# Export app and socketio for WSGI servers
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)

