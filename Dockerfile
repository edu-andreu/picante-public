# Use Python 3.8 slim as base image
FROM python:3.8-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    PORT=8000 \
    HOST=0.0.0.0 \
    CHROME_BIN=/usr/bin/google-chrome \
    CHROMEDRIVER_PATH=/usr/local/bin/chromedriver \
    PYTHONPATH=/app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    xvfb \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install ChromeDriver
RUN CHROME_VERSION=$(google-chrome --version | awk '{print $3}' | cut -d'.' -f1) \
    && CHROMEDRIVER_VERSION=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_$CHROME_VERSION") \
    && wget -q "https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/$CHROMEDRIVER_VERSION/linux64/chromedriver-linux64.zip" \
    && unzip chromedriver-linux64.zip \
    && mv chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf chromedriver-linux64.zip chromedriver-linux64

# Create app directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application files
COPY . .

# Create necessary directories
RUN mkdir -p /app/data/logs /app/data/downloads

# Create startup script
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
echo "Starting API..."\n\
echo "Current directory: $(pwd)"\n\
echo "Files in directory:"\n\
ls -la\n\
\n\
# Remove any existing Xvfb lock file\n\
rm -f /tmp/.X99-lock\n\
\n\
echo "Starting Xvfb..."\n\
Xvfb :99 -screen 0 1920x1080x24 &\n\
\n\
# Wait for Xvfb to start\n\
sleep 2\n\
\n\
echo "Running API..."\n\
cd /app\n\
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level debug\n\
' > /app/start.sh && chmod +x /app/start.sh

# Expose the port that will be used by the application
EXPOSE 8000

# Add healthcheck
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Command to run the API
CMD ["/app/start.sh"] 