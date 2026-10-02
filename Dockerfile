# Image de base avec Python
FROM python:3.11-slim

# Outils nécessaires au bot : ffmpeg (vidéo), polices (sous-titres propres)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY render_bot.py .

# Dossiers de données / fichiers temporaires (voir le script)
RUN mkdir -p /app/data /app/local

CMD ["python", "render_bot.py"]
