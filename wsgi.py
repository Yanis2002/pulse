"""
WSGI entry point for Railway deployment
"""
import os
import sys

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

print("Starting WSGI application...")
print(f"Python version: {sys.version}")
print(f"Working directory: {os.getcwd()}")

try:
    # Import app and socketio - this will trigger initialization
    print("Importing app module...")
    from app import app, socketio
    print("App module imported successfully")
    
    # For gunicorn with eventlet, we need to use the app directly
    # SocketIO will work through the eventlet worker
    application = app
    print("WSGI application created successfully")
    
except Exception as e:
    print(f"Error importing app: {e}")
    import traceback
    traceback.print_exc()
    raise

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Starting server on port {port}")
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)

