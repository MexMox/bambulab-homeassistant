FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 BAMBU_CONFIG=/config/config.yaml BAMBU_DATA=/data
WORKDIR /app
# ffmpeg: Kamera der X1/H2/P2-Serie (RTSPS)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py .
RUN useradd -r -u 1000 bambu && mkdir -p /data && chown bambu /data
USER bambu
CMD ["python", "bridge.py"]
