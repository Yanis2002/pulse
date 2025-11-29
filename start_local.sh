#!/bin/bash

# Start local version of the application for tunneling

echo "🚀 Starting LOCAL version of PULSE | CLUB"
echo "=========================================="

# Create local database directory if it doesn't exist
mkdir -p local_db

# Set environment variables for local mode
export LOCAL_MODE=true
export DB_DIR="$(pwd)/local_db"
export PORT=8000

# Check if port 8000 is already in use
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null ; then
    echo "⚠️  Port 8000 is already in use. Killing existing process..."
    lsof -ti:8000 | xargs kill -9 2>/dev/null || true
    sleep 2
fi

# Start the application
echo "📁 Local database: $DB_DIR/pulse_tournaments.db"
echo "🌐 Server will start on: http://localhost:8000"
echo "🔗 Tunnel URL: Use your tunneling service (e.g., tunnel4.com)"
echo ""
echo "Starting server..."

python3 app_local.py

