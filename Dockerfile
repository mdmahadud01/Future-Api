# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── Install system deps + cloudflared ────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        ca-certificates \
    && wget -q \
        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
        -O /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── App ───────────────────────────────────────────────────────────────────────
WORKDIR /app

# Install Python dependencies first (Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files (including pyquotex-1.1.0.tar.gz)
COPY . .

# Extract pyquotex library from bundled tar.gz → creates pyquotex/ folder
RUN tar -xf pyquotex-1.1.0.tar.gz

# Railway sets $PORT automatically; default fallback 5000
EXPOSE 5000

CMD ["python", "main.py"]
