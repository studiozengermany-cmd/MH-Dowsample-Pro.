FROM python:3.11-slim

# Install system dependencies and FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright dependencies and Chromium browser
RUN playwright install --with-deps chromium

# Copy project files
COPY . .

# Run the telegram bot
CMD ["python", "bot.py"]
