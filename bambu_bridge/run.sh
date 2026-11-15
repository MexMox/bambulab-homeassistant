#!/usr/bin/with-contenv bashio
# Startet die Bambu Bridge als Home-Assistant-Add-on.
# MQTT-Zugangsdaten kommen automatisch vom Mosquitto-Add-on (Supervisor-Dienst "mqtt").

if bashio::services.available "mqtt"; then
    export MQTT_HOST="$(bashio::services mqtt 'host')"
    export MQTT_PORT="$(bashio::services mqtt 'port')"
    export MQTT_USERNAME="$(bashio::services mqtt 'username')"
    export MQTT_PASSWORD="$(bashio::services mqtt 'password')"
    bashio::log.info "MQTT-Broker vom Supervisor: ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.warning "Kein MQTT-Dienst gefunden – Mosquitto-Add-on installieren oder mqtt_host eintragen."
fi

exec python3 /app/addon_launcher.py
