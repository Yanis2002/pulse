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

# Set environment variables (Railway will override PORT)
ENV PORT=8000
ENV DB_DIR=/data

# Run gunicorn with eventlet (Railway sets PORT automatically via environment)
# Use exec form with sh -c to properly expand PORT variable
CMD ["sh", "-c", "gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT:-8000} --timeout 120 --access-logfile - --error-logfile - --log-level debug wsgi:application"]

