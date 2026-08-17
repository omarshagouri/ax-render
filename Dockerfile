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
# --- caption font: Montserrat ExtraBold (SIL OFL) — exact family libass matches is "Montserrat ExtraBold"
RUN apt-get update && apt-get install -y --no-install-recommends fontconfig curl \
    && mkdir -p /usr/share/fonts/truetype/montserrat \
    && curl -sL -o /usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf \
       https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-ExtraBold.ttf \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# --- bake the whisper model so runtime never downloads it
ENV HF_HOME=/models
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')"
