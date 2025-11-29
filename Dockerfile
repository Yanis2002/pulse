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

# Expose port (Cloud Run uses 8080 by default, but we'll use PORT env var)
EXPOSE 8080

# Set default port (will be overridden by Cloud Run)
ENV PORT=8080

# Run gunicorn with eventlet (with timeout and logging)
# Use PORT environment variable for flexibility
CMD gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT:-8080} --timeout 120 --access-logfile - --error-logfile - --log-level debug wsgi:application

