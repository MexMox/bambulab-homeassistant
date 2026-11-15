# Basis-Image: offizielles Home-Assistant-Image (Multi-Arch, aktuelles Alpine, inkl. bashio + s6)
FROM ghcr.io/home-assistant/base:latest

ENV PYTHONUNBUFFERED=1 \
    BAMBU_DATA=/data

# Python + ffmpeg (Kamera der X1/H2/P2-Serie über RTSPS)
RUN apk add --no-cache python3 py3-pip ffmpeg

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY bridge.py addon_launcher.py ./
COPY www/ ./www/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL io.hass.type="app" \
      io.hass.name="Bambu Lab Bridge" \
      org.opencontainers.image.source="https://github.com/mexmox/bambulab-homeassistant"

CMD ["/run.sh"]
