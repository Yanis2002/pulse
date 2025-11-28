"""
WSGI entry point for Railway deployment
"""
import os
import sys

# Force output to be unbuffered so logs appear immediately
sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

# Print immediately to ensure we see this in logs
print("=" * 50, file=sys.stderr)
print("Starting WSGI application...", file=sys.stderr)
print(f"Python version: {sys.version}", file=sys.stderr)
print(f"Working directory: {os.getcwd()}", file=sys.stderr)
print(f"PORT environment variable: {os.environ.get('PORT', 'not set')}", file=sys.stderr)
print("=" * 50, file=sys.stderr)
sys.stderr.flush()

try:
    # Import app and socketio - this will trigger initialization
    print("Importing app module...", file=sys.stderr)
    sys.stderr.flush()
    from app import app, socketio
    print("App module imported successfully", file=sys.stderr)
    sys.stderr.flush()
    
    # For gunicorn with eventlet, use the Flask app directly
    # SocketIO will work through the eventlet worker automatically
    application = app
    print("WSGI application created successfully", file=sys.stderr)
    print(f"Application ready to serve requests", file=sys.stderr)
    print(f"Flask app name: {app.name}", file=sys.stderr)
    sys.stderr.flush()
    
except Exception as e:
    print(f"ERROR importing app: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()
    raise

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Starting server on port {port}")
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)

