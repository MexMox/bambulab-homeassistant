#!/usr/bin/env python3
"""
Integrationstest der Bambu Bridge – echte Prozesse gegen einen simulierten Drucker.

Szenarien
  chamber  P1S: MQTT über TLS, Kamera-Protokoll Port 6000, FTPS-Vorschau, Druckende, Befehle
  rtsp     X1C: wie oben, Kamera per RTSPS (mediamtx + ffmpeg), Kammertemperatur

Die Bridge wird – wie im Add-on – über addon_launcher.py gestartet (options.json,
simulierter Supervisor mit Lovelace-Ressourcen-API, Ingress-HTTP-Server).

Voraussetzungen
  * root (Ports 322, 990, 6000 sind privilegiert) – z. B. in einem Test-Container/LXC
  * mosquitto, mosquitto_passwd, openssl, ffmpeg
  * für "rtsp": mediamtx v1.9.x (Pfad per Umgebungsvariable MEDIAMTX=/pfad/mediamtx)
  * pip install -r tests/requirements-dev.txt

Aufruf
  sudo python3 tests/integration/run_integration.py            # alle Szenarien
  sudo python3 tests/integration/run_integration.py chamber
"""
import asyncio
import http.client
import io
import json
import os
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[2]
ADDON = ROOT / "bambu_bridge"
CODE = "12345678"
RESULTS: list[tuple[str, str, bool, str]] = []


# --------------------------------------------------------------------------- Hilfen
def check(scenario: str, name: str, ok, detail: str = ""):
    RESULTS.append((scenario, name, bool(ok), detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""), flush=True)


def wait_for(cond, timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(step)
    return False


def tiny_png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"\x00\xff\xff\xff"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def mqtt_client(cid: str) -> mqtt.Client:
    return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=cid)


def insecure_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# --------------------------------------------------------------------------- Testumgebung
class Environment:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bambu-it-"))
        os.chmod(self.dir, 0o755)   # mosquitto läuft als eigener Benutzer und muss Zertifikate lesen
        self.procs: list[subprocess.Popen] = []
        self.stop = threading.Event()
        self.resources = [{"id": "alt", "type": "module", "url": "/local/bambu/bambu-printer-card.js?v=alt"}]
        self.ftp_server = None
        self.notifications: list[dict] = []

    # ---- Grundlagen
    def certs(self):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", self.dir / "key.pem",
                        "-out", self.dir / "cert.pem", "-days", "2", "-subj", "/CN=printer"],
                       check=True, capture_output=True)
        os.chmod(self.dir / "key.pem", 0o644)

    def spawn(self, args, log_name, env=None):
        log = open(self.dir / log_name, "w")
        proc = subprocess.Popen([str(a) for a in args], stdout=log, stderr=subprocess.STDOUT, env=env)
        self.procs.append(proc)
        return proc

    def brokers(self):
        pw = self.dir / "pw"
        subprocess.run(["mosquitto_passwd", "-c", "-b", pw, "bblp", CODE], check=True, capture_output=True)
        os.chmod(pw, 0o644)
        (self.dir / "printer.conf").write_text(
            f"listener 8883\ncafile {self.dir}/cert.pem\ncertfile {self.dir}/cert.pem\nkeyfile {self.dir}/key.pem\n"
            f"allow_anonymous false\npassword_file {pw}\n")
        (self.dir / "ha.conf").write_text("listener 1883 127.0.0.1\nallow_anonymous true\n")
        self.spawn(["mosquitto", "-c", self.dir / "printer.conf"], "mosquitto-printer.log")
        self.spawn(["mosquitto", "-c", self.dir / "ha.conf"], "mosquitto-ha.log")
        assert wait_for(lambda: socket.create_connection(("127.0.0.1", 1883), 1).close() is None, 5), "HA-Broker startet nicht"
        assert wait_for(lambda: socket.create_connection(("127.0.0.1", 8883), 1).close() is None, 5), "Drucker-Broker startet nicht"

    def ftps(self, files: dict[str, bytes]):
        import logging

        logging.getLogger("pyftpdlib").setLevel(logging.WARNING)
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import TLS_FTPHandler
        from pyftpdlib.servers import FTPServer

        root = self.dir / "sdcard"
        for rel, data in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)

        class Implicit(TLS_FTPHandler):  # Bambu: implizites TLS auf Port 990
            def handle(self):
                self.secure_connection(self.ssl_context)

            def handle_ssl_established(self):
                TLS_FTPHandler.handle(self)

        auth = DummyAuthorizer()
        auth.add_user("bblp", CODE, str(root), perm="elr")
        Implicit.authorizer, Implicit.certfile, Implicit.keyfile = auth, str(self.dir / "cert.pem"), str(self.dir / "key.pem")
        Implicit.tls_control_required = Implicit.tls_data_required = True
        self.ftp_server = FTPServer(("127.0.0.1", 990), Implicit)
        threading.Thread(target=self.ftp_server.serve_forever, kwargs={"handle_exit": False}, daemon=True).start()

    def mock_supervisor(self):
        import websockets

        env = self

        async def handler(ws):
            await ws.send(json.dumps({"type": "auth_required"}))
            if json.loads(await ws.recv()).get("access_token") != "test-token":
                await ws.send(json.dumps({"type": "auth_invalid"}))
                return
            await ws.send(json.dumps({"type": "auth_ok"}))
            async for raw in ws:
                m = json.loads(raw)
                result = None
                if m["type"] == "lovelace/resources":
                    result = env.resources
                elif m["type"] == "lovelace/resources/update":
                    for r in env.resources:
                        if r["id"] == m["resource_id"]:
                            r["url"] = m["url"]
                elif m["type"] == "lovelace/resources/create":
                    env.resources.append({"id": f"n{len(env.resources)}", "type": m["res_type"], "url": m["url"]})
                elif m["type"] == "call_service":
                    env.notifications.append(m)
                await ws.send(json.dumps({"id": m["id"], "type": "result", "success": True, "result": result}))

        async def main():
            async with websockets.serve(handler, "127.0.0.1", 8150):
                while not env.stop.is_set():
                    await asyncio.sleep(0.2)

        threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()

    def cleanup(self):
        self.stop.set()
        if self.ftp_server:
            self.ftp_server.close_all()
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()


# --------------------------------------------------------------------------- simulierter Drucker
class FakePrinter:
    def __init__(self, serial: str, model: str, job: str):
        self.serial, self.model, self.job = serial, model, job
        self.requests: list[dict] = []
        self.client = mqtt_client(f"fake-{serial}")
        self.client.username_pw_set("bblp", CODE)
        self.client.tls_set_context(insecure_ctx())
        self.client.on_connect = lambda c, *_: c.subscribe(f"device/{serial}/request")
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", 8883)
        self.client.loop_start()

    def report(self) -> dict:
        data = {
            "command": "push_status", "gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 95,
            "subtask_name": self.job, "gcode_file": f"{self.job}.gcode.3mf", "layer_num": 57, "total_layer_num": 210,
            "nozzle_temper": 219.6, "nozzle_target_temper": 220, "bed_temper": 54.9, "bed_target_temper": 55,
            "spd_lvl": 2, "cooling_fan_speed": "15", "big_fan1_speed": "6", "big_fan2_speed": "0",
            "wifi_signal": "-52dBm", "lights_report": [{"node": "chamber_light", "mode": "off"}],
            "ams": {"tray_now": "0", "ams": [{"id": "0", "humidity_raw": "21", "temp": "24", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 80}]}]},
        }
        if self.model == "X1C":
            data["chamber_temper"] = 38
        return {"print": data}

    def _on_message(self, client, userdata, msg):
        req = json.loads(msg.payload)
        self.requests.append(req)
        if "pushing" in req:
            client.publish(f"device/{self.serial}/report", json.dumps(self.report()))

    def finish(self):
        self.client.publish(f"device/{self.serial}/report",
                            json.dumps({"print": {"command": "push_status", "gcode_state": "FINISH", "mc_percent": 100}}))

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def chamber_camera(env: Environment, stop: threading.Event):
    """P1/A1-Kameraprotokoll: TLS, 80-Byte-Anmeldung, dann 16-Byte-Header + JPEG."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(env.dir / "cert.pem", env.dir / "key.pem")
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 6000))
    srv.listen(2)
    srv.settimeout(0.5)
    jpeg = b"\xff\xd8" + b"\x10" * 800 + b"\xff\xd9"
    env.cam_auth_ok = False
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        try:
            s = ctx.wrap_socket(conn, server_side=True)
            auth = b""
            while len(auth) < 80:
                auth += s.recv(80 - len(auth))
            env.cam_auth_ok = struct.unpack("<II", auth[:8]) == (0x40, 0x3000) and auth[48:80].rstrip(b"\0") == CODE.encode()
            while not stop.is_set():
                s.sendall(struct.pack("<IIII", len(jpeg), 0, 1, 0) + jpeg)
                time.sleep(0.5)
        except OSError:
            pass
    srv.close()


def rtsp_camera(env: Environment) -> list[subprocess.Popen]:
    mediamtx = os.environ.get("MEDIAMTX") or shutil.which("mediamtx")
    if not mediamtx:
        raise RuntimeError("mediamtx nicht gefunden (MEDIAMTX=/pfad/mediamtx setzen)")
    (env.dir / "mediamtx.yml").write_text(f"""logLevel: warn
rtsp: yes
protocols: [tcp]
encryption: "optional"
rtspAddress: :8554
rtspsAddress: :322
serverKey: {env.dir}/key.pem
serverCert: {env.dir}/cert.pem
rtmp: no
hls: no
webrtc: no
srt: no
api: no
authInternalUsers:
  - user: any
    pass:
    ips: ['127.0.0.1/32']
    permissions: [{{action: publish}}]
  - user: bblp
    pass: "{CODE}"
    permissions: [{{action: read}}]
paths:
  all_others:
""")
    procs = [env.spawn([mediamtx, env.dir / "mediamtx.yml"], "mediamtx.log")]
    time.sleep(1.5)
    procs.append(env.spawn(["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                            "-i", "testsrc=size=640x360:rate=5", "-c:v", "libx264", "-preset", "ultrafast",
                            "-tune", "zerolatency", "-g", "5", "-f", "rtsp", "-rtsp_transport", "tcp",
                            "rtsp://127.0.0.1:8554/streaming/live/1"], "ffmpeg-publisher.log"))
    time.sleep(2.5)
    return procs


class HAWatcher:
    def __init__(self):
        self.msgs: dict[str, bytes] = {}
        self.c = mqtt_client(f"ha-watch-{time.time()}")
        self.c.on_message = lambda c, u, m: self.msgs.__setitem__(m.topic, m.payload)
        self.c.connect("127.0.0.1", 1883)
        self.c.subscribe([("homeassistant/#", 0), ("bambu/#", 0)])
        self.c.loop_start()

    def json(self, topic):
        return json.loads(self.msgs[topic]) if topic in self.msgs else None

    def publish(self, topic, payload):
        self.c.publish(topic, payload)

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


def http_get(path: str, source: str = "127.0.0.1"):
    conn = http.client.HTTPConnection("127.0.0.1", 8099, timeout=5, source_address=(source, 0))
    conn.request("GET", path)
    r = conn.getresponse()
    return r.status, r.getheader("Content-Type"), r.read()


# --------------------------------------------------------------------------- Szenario
def run_scenario(env: Environment, kind: str):
    model = "P1S" if kind == "chamber" else "X1C"
    name = f"IT {model}"
    slug = name.lower().replace(" ", "_")
    serial = f"01TEST{model}0001"
    job = f"Testteil_{model}"
    print(f"\n▶ Szenario {kind} ({model})", flush=True)

    cam_stop = threading.Event()
    cam_procs: list[subprocess.Popen] = []
    if kind == "chamber":
        threading.Thread(target=chamber_camera, args=(env, cam_stop), daemon=True).start()
    else:
        cam_procs = rtsp_camera(env)

    printer = FakePrinter(serial, model, job)
    watcher = HAWatcher()
    data_dir = env.dir / f"data-{kind}"
    ha_dir = env.dir / f"ha-{kind}"
    ha_dir.mkdir()
    (env.dir / f"options-{kind}.json").write_text(json.dumps({
        "log_level": "info", "install_card": True,
        "printers": [{"name": name, "model": model, "host": "127.0.0.1", "serial": serial, "access_code": CODE,
                      "camera": True, "preview": True, "camera_interval": 1, "last_print_delay": 2}],
    }))
    env_vars = {**os.environ, "ADDON_OPTIONS": str(env.dir / f"options-{kind}.json"), "HA_CONFIG_DIR": str(ha_dir),
                "SUPERVISOR_TOKEN": "test-token", "SUPERVISOR_CORE_WS": "ws://127.0.0.1:8150",
                "BAMBU_DATA": str(data_dir), "BAMBU_CONFIG_OUT": str(env.dir / f"bambu-{kind}.yaml"),
                "MQTT_HOST": "127.0.0.1", "MQTT_PORT": "1883", "MQTT_USERNAME": "", "MQTT_PASSWORD": ""}
    bridge_proc = env.spawn([sys.executable, ADDON / "addon_launcher.py"], f"bridge-{kind}.log", env=env_vars)
    base = f"bambu/{slug}"

    try:
        ok = wait_for(lambda: (watcher.json(f"{base}/state") or {}).get("status") == "Druckt", 15)
        check(kind, "Bridge verbindet sich mit Drucker und HA-Broker, Status 'Druckt'", ok)
        state = watcher.json(f"{base}/state") or {}
        check(kind, f"Modell erkannt: {state.get('model')}", state.get("model") == ("P1S" if model == "P1S" else "X1 Carbon"))
        check(kind, f"Kamera-Typ: {state.get('camera_type')}", state.get("camera_type") == kind)
        check(kind, "Fortschritt/Schicht/Temperaturen übernommen",
              state.get("progress") == 42 and state.get("layer") == 57 and state.get("nozzle_temp") == 219.6)
        if model == "X1C":
            check(kind, "Kammertemperatur (X1C) = 38 °C", state.get("chamber_temp") == 38)
        else:
            check(kind, "Keine Kammertemperatur beim P1S", state.get("chamber_temp") is None)

        disc = [t for t in watcher.msgs if t.startswith("homeassistant/") and serial in t]
        expected = 28 if model == "X1C" else 27   # X1C zusätzlich: Sensor "Bauraum"
        check(kind, f"MQTT-Discovery: {len(disc)} Entitäten (erwartet {expected})", len(disc) == expected)
        if model == "X1C":
            check(kind, "Discovery enthält Sensor 'Bauraum'", any("/chamber_temp/" in t for t in disc))

        ok = wait_for(lambda: watcher.msgs.get(f"{base}/camera", b"")[:2] == b"\xff\xd8", 20)
        check(kind, "Kamerabild (JPEG) kommt in HA an", ok)
        if kind == "chamber":
            check(kind, "Kamera-Anmeldung mit Access Code korrekt", getattr(env, "cam_auth_ok", False))

        ok = wait_for(lambda: watcher.msgs.get(f"{base}/preview", b"")[:4] == b"\x89PNG", 15)
        pv = watcher.json(f"{base}/preview_attr") or {}
        check(kind, "3D-Vorschau per FTPS geladen (PNG + Metadaten)", ok and pv.get("weight_g") == 23.5 and pv.get("prediction_min") == 95)

        status, ctype, body = http_get("/")
        check(kind, "Ingress: Dashboard wird ausgeliefert", status == 200 and b"<!DOCTYPE html>" in body, f"HTTP {status}")
        status, _, _ = http_get("/", source="127.0.0.5")
        check(kind, "Ingress: fremde Adressen werden abgewiesen", status == 403, f"HTTP {status}")

        card = ha_dir / "www" / "bambu" / "bambu-printer-card.js"
        check(kind, "Dashboard-Karte nach /config/www/bambu kopiert", card.is_file())
        check(kind, "Neuer www-Ordner → Benachrichtigung 'Home Assistant neu starten'",
              any(n.get("domain") == "persistent_notification"
                  and n.get("service_data", {}).get("notification_id") == "bambu_bridge_restart"
                  for n in env.notifications))
        env.notifications.clear()
        urls = [r["url"] for r in env.resources]
        check(kind, "Lovelace-Ressource aktualisiert (kein Duplikat)",
              sum("bambu-printer-card.js" in u for u in urls) == 1 and not any(u.endswith("v=alt") for u in urls), str(urls))

        before = len(printer.requests)
        watcher.publish(f"{base}/cmd/light", "ON")
        watcher.publish(f"{base}/cmd/speed", "Sport")
        watcher.publish(f"{base}/cmd/pause", "PRESS")
        wait_for(lambda: len(printer.requests) >= before + 3, 5)
        new = printer.requests[before:]
        check(kind, "Befehl Licht → ledctrl beim Drucker", any(r.get("system", {}).get("command") == "ledctrl" for r in new))
        check(kind, "Befehl Geschwindigkeit → print_speed 3",
              any(r.get("print", {}).get("command") == "print_speed" and r["print"].get("param") == "3" for r in new))
        check(kind, "Befehl Pause → pause", any(r.get("print", {}).get("command") == "pause" for r in new))

        printer.finish()
        ok = wait_for(lambda: (watcher.json(f"{base}/last_print_attr") or {}).get("result") == "Fertig", 20)
        lp = watcher.json(f"{base}/last_print_attr") or {}
        check(kind, "Druckende: 'Letzter Druck' gespeichert (Ergebnis Fertig)", ok and lp.get("file") == job)
        check(kind, "Letzter Druck als Datei im Datenordner", (data_dir / f"{slug}_last_print.jpg").is_file())
        check(kind, "Status wechselt auf 'Fertig'",
              wait_for(lambda: (watcher.json(f"{base}/state") or {}).get("status") == "Fertig", 5))
    finally:
        bridge_proc.send_signal(signal.SIGTERM)
        try:
            bridge_proc.wait(10)
        except subprocess.TimeoutExpired:
            bridge_proc.kill()
        check(kind, "Sauberes Beenden: Bridge meldet 'offline'",
              wait_for(lambda: watcher.msgs.get("bambu/bridge/status") == b"offline", 5))
        cam_stop.set()
        for p in cam_procs:
            p.terminate()
        printer.close()
        watcher.close()
        time.sleep(1.5)
        errors = [line for line in (env.dir / f"bridge-{kind}.log").read_text().splitlines()
                  if "Traceback" in line or "ERROR" in line]
        check(kind, "Keine Fehler/Tracebacks im Bridge-Log", not errors, errors[0] if errors else "")


def main():
    scenarios = sys.argv[1:] or ["chamber", "rtsp"]
    if scenarios == ["all"]:
        scenarios = ["chamber", "rtsp"]
    missing = [t for t in ("mosquitto", "mosquitto_passwd", "openssl", "ffmpeg") if not shutil.which(t)]
    if missing:
        sys.exit(f"Fehlende Programme: {', '.join(missing)}")
    if os.geteuid() != 0:
        sys.exit("Bitte als root ausführen (Ports 322/990/6000).")

    env = Environment()
    print(f"Testordner: {env.dir}")
    try:
        env.certs()
        env.brokers()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/plate_1.png", tiny_png())
            z.writestr("Metadata/plate_1.gcode", "G28\n")
            z.writestr("Metadata/slice_info.config",
                       '<config><plate><metadata key="index" value="1"/><metadata key="prediction" value="5712"/>'
                       '<metadata key="weight" value="23.47"/><filament id="1" type="PLA" color="#FFFFFF" used_g="23.47"/>'
                       '</plate></config>')
        env.ftps({f"cache/Testteil_{m}.gcode.3mf": buf.getvalue() for m in ("P1S", "X1C")})
        env.mock_supervisor()
        for kind in scenarios:
            run_scenario(env, kind)
    finally:
        env.cleanup()

    passed = sum(ok for *_, ok, _ in RESULTS)
    print(f"\n{'=' * 60}\nErgebnis: {passed}/{len(RESULTS)} Prüfungen bestanden")
    for scenario, name, ok, detail in RESULTS:
        if not ok:
            print(f"  ❌ [{scenario}] {name} {detail}")
    print(f"Logs: {env.dir}")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
