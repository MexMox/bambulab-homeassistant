ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    BAMBU_DATA=/data

WORKDIR /app
# ffmpeg: Kamera der X1/H2/P2-Serie (RTSPS)
RUN apk add --no-cache ffmpeg
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY bridge.py addon_launcher.py ./
COPY www/ ./www/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
