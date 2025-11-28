FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Set environment variable (Railway will override PORT)
ENV PORT=8000
ENV DB_DIR=/data

# Run gunicorn with eventlet
# Railway automatically sets PORT environment variable, gunicorn will use it
# We use a wrapper script to properly handle PORT variable
CMD ["sh", "-c", "exec gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT:-8000} --timeout 120 --access-logfile - --error-logfile - --log-level debug wsgi:application"]

