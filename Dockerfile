# Official Playwright image: Chromium + all system libraries preinstalled.
# Tag MUST match the playwright version in requirements.txt (1.50.0).
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

# ffmpeg is not in the Playwright image — add it.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run provides $PORT (default 8080).
ENV PORT=8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
