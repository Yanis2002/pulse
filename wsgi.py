"""
WSGI entry point for Amvera deployment
"""
import os
import sys

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

from app import app, socketio

# Export app wrapped with SocketIO for WSGI servers (gunicorn with eventlet)
application = socketio.WSGIApp(socketio, app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)

