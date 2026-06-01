FROM python:3.12-slim

WORKDIR /app

# SDL2 runtime libs needed by pygame (audio driver set to dummy — no hardware required)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsdl2-2.0-0 \
        libsdl2-mixer-2.0-0 \
        libglib2.0-0 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-generate piano WAV files at build time so startup is instant
RUN python3 generate_sounds.py

ENV SDL_VIDEODRIVER=dummy
# SDL_AUDIODRIVER is intentionally NOT set here so docker-compose can override it
# (set to "alsa" for real sound, or "dummy" for silent/CI testing)
ENV TERM=xterm-256color

CMD ["python3", "main.py"]
