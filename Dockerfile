# Use official Python runtime as base image
FROM python:3.13-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install system dependencies if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories
RUN mkdir -p /app/logs /app/config /app/data /app/temp

# Copy application files
COPY episeerr.py .
COPY media_processor.py .
COPY episeerr_utils.py .
COPY sonarr_utils.py .
COPY notifications.py .
COPY notification_storage.py .
COPY activity_storage.py .
COPY pending_deletions.py .
COPY dashboard.py .
COPY settings_db.py .
COPY logging_config.py .
COPY integrations/ integrations/
COPY templates/ templates/
COPY static/ static/

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5002/api/series-stats || exit 1

# Expose port
EXPOSE 5002

# Use Gunicorn to serve the application
CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:5002", "--access-logfile", "-", "--error-logfile", "-", "episeerr:app"]
