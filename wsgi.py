"""
WSGI entry point for Amvera deployment
"""
import os
import sys

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

# Import app and socketio - this will trigger initialization
from app import app, socketio

# For gunicorn with eventlet, we need to use the app directly
# SocketIO will work through the eventlet worker
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)

