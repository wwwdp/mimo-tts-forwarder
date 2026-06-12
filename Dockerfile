FROM python:3.12-slim

LABEL maintainer="MiMO-TTS-Forwarder"
LABEL description="TTS Forwarder with Voice Cloning via MiMo API"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY main.py .
COPY .env* ./

# Create data directories
RUN mkdir -p /app/data/voices

# Expose port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')" || exit 1

# Run
CMD ["python", "main.py"]
