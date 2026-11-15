#!/usr/bin/env python3
"""
Add-on-Starter für die MexMox Bambu Bridge.
1. Liest die Formularwerte (/data/options.json) und die MQTT-Daten vom Supervisor
2. Kopiert die Dashboard-Karte nach /homeassistant/www/bambu und trägt sie als Ressource ein
3. Startet bridge.py mit der erzeugten Konfiguration
"""
import hashlib
import json
import logging
import os
import shutil
import sys

import yaml

OPTIONS = os.environ.get("ADDON_OPTIONS", "/data/options.json")
HA_CONFIG = os.environ.get("HA_CONFIG_DIR", "/homeassistant")
APP_DIR = os.environ.get("APP_DIR", os.path.dirname(os.path.abspath(__file__)))
CORE_WS = os.environ.get("SUPERVISOR_CORE_WS", "ws://supervisor/core/websocket")
CONFIG_OUT = os.environ.get("BAMBU_CONFIG_OUT", "/tmp/bambu.yaml")
CARD_NAME = "bambu-printer-card.js"
CARD_URL_BASE = f"/local/bambu/{CARD_NAME}"

log = logging.getLogger("bambu-addon")


def build_config(opts: dict) -> dict:
    printers = []
    for p in opts.get("printers") or []:
        clean = {k: v for k, v in p.items() if v not in (None, "")}
        missing = [k for k in ("host", "serial", "access_code") if not clean.get(k)]
        if missing:
            log.error("Drucker '%s': Feld(er) %s fehlen – bitte im Tab 'Konfiguration' ausfüllen.",
                      clean.get("name", "?"), ", ".join(missing))
            continue
        printers.append(clean)
    if not printers:
        raise SystemExit("Kein vollständig eingerichteter Drucker – Add-on wird beendet.")

    mqtt = {
        "host": opts.get("mqtt_host") or os.environ.get("MQTT_HOST") or "core-mosquitto",
        "port": int(opts.get("mqtt_port") or os.environ.get("MQTT_PORT") or 1883),
        "username": opts.get("mqtt_username") or os.environ.get("MQTT_USERNAME") or "",
        "password": opts.get("mqtt_password") or os.environ.get("MQTT_PASSWORD") or "",
    }
    return {
        "log_level": str(opts.get("log_level", "info")).upper(),
        "data_dir": os.environ.get("BAMBU_DATA", "/data"),
        "homeassistant": {**mqtt, "discovery_prefix": "homeassistant", "base_topic": "bambu",
                          "client_id": "mexmox-bambu-addon"},
        "printers": printers,
        # Ingress: Dashboard + Kamera-Stream, nur für den Supervisor-Proxy erreichbar
        "http": {"enabled": True, "bind": "0.0.0.0", "port": 8099,
                 "www_dir": os.path.join(APP_DIR, "www"),
                 "allow": ["172.30.32.2", "127.0.0.1"]},
    }


def ws_call(ws, msg_id: int, payload: dict) -> dict:
    payload["id"] = msg_id
    ws.send(json.dumps(payload))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == msg_id and msg.get("type") == "result":
            return msg


def register_resource(url: str) -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        log.warning("Kein SUPERVISOR_TOKEN – Ressource bitte manuell eintragen: %s (JavaScript-Modul)", url)
        return
    import websocket  # websocket-client

    ws = websocket.create_connection(CORE_WS, timeout=15)
    try:
        json.loads(ws.recv())  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            log.warning("Anmeldung an Home Assistant fehlgeschlagen – Ressource bitte manuell eintragen: %s", url)
            return
        res = ws_call(ws, 1, {"type": "lovelace/resources"})
        if not res.get("success"):
            log.warning("Ressourcen nicht lesbar (Dashboards im YAML-Modus?) – bitte manuell eintragen: %s", url)
            return
        existing = [r for r in res.get("result") or [] if CARD_URL_BASE in str(r.get("url", ""))]
        if existing and existing[0].get("url") == url:
            log.info("Dashboard-Karte ist aktuell (%s)", url)
            return
        if existing:
            out = ws_call(ws, 2, {"type": "lovelace/resources/update", "resource_id": existing[0]["id"],
                                  "res_type": "module", "url": url})
            action = "aktualisiert"
        else:
            out = ws_call(ws, 2, {"type": "lovelace/resources/create", "res_type": "module", "url": url})
            action = "eingetragen"
        if out.get("success"):
            log.info("Dashboard-Karte als Ressource %s: %s", action, url)
        else:
            log.warning("Ressource konnte nicht %s werden (%s) – bitte manuell: %s",
                        action, out.get("error", {}).get("message"), url)
    finally:
        ws.close()


def install_card() -> None:
    src = os.path.join(APP_DIR, "www", CARD_NAME)
    www = os.path.join(HA_CONFIG, "www")
    www_created = not os.path.isdir(www)
    dst_dir = os.path.join(www, "bambu")
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copyfile(src, os.path.join(dst_dir, CARD_NAME))
    with open(src, "rb") as fh:
        version = hashlib.sha1(fh.read()).hexdigest()[:8]
    url = f"{CARD_URL_BASE}?v={version}"
    log.info("Dashboard-Karte nach %s kopiert", dst_dir)
    try:
        register_resource(url)
    except Exception as exc:  # noqa: BLE001 – Karte ist optional, Bridge soll trotzdem starten
        log.warning("Ressource konnte nicht eingetragen werden (%s) – bitte manuell: %s", exc, url)
    if www_created:
        log.warning("Ordner 'www' wurde neu angelegt – Home Assistant EINMAL neu starten, "
                    "damit die Karte unter /local/ erreichbar ist.")


def main() -> None:
    try:
        with open(OPTIONS, "r", encoding="utf-8") as fh:
            opts = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"Optionen nicht lesbar: {exc}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = build_config(opts)
    with open(CONFIG_OUT, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    os.chmod(CONFIG_OUT, 0o600)

    if opts.get("install_card", True):
        try:
            install_card()
        except OSError as exc:
            log.warning("Dashboard-Karte konnte nicht kopiert werden: %s", exc)

    log.info("Starte Bridge mit %d Drucker(n) …", len(cfg["printers"]))
    os.execv(sys.executable, [sys.executable, os.path.join(APP_DIR, "bridge.py"), CONFIG_OUT])


if __name__ == "__main__":
    main()
