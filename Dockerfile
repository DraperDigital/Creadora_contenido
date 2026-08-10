FROM node:22-slim

# Install system tools: Python 3, venv, pip, ffmpeg
RUN apt-get update && apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy repository files
COPY . .

# Set up environment and install Python + Node dependencies
RUN python3 -m venv .venv && \
    .venv/bin/pip install --no-cache-dir -e ./server/engine && \
    .venv/bin/pip install --no-cache-dir -e ./pipeline && \
    ( cd pipeline && npm install --no-audit --no-fund --loglevel=error ) && \
    ( cd server/cloud && npm install --no-audit --no-fund --loglevel=error )

EXPOSE 8787

CMD [".venv/bin/python3", "-m", "bionico.cli", "start"]
