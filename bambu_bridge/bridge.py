#!/usr/bin/env python3
"""
MexMox Bambu Bridge
-------------------
Verbindet Bambu Lab P1S Drucker (lokales MQTT über TLS, Port 8883 + Kamera Port 6000)
mit Home Assistant über MQTT Discovery. Keine HACS-Integration nötig.

Drucker  --(TLS MQTT, bblp/AccessCode)-->  Bridge  --(MQTT)-->  Mosquitto (HA)  --> Entitäten
Kamera   --(TLS :6000, JPEG-Frames)----->  Bridge  --(MQTT camera / HTTP MJPEG)
"""
from __future__ import annotations

import ftplib
import json
import logging
import os
import xml.etree.ElementTree as ET
import zipfile
import re
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt
import yaml

VERSION = "1.5.0"

# Modell-Code -> (Anzeigename, Kamera-Typ, Kammer-Temperatursensor, Doppeldüse)
#   chamber = TLS-JPEG-Stream auf Port 6000 (P1/A1-Serie)
#   rtsp    = RTSPS auf Port 322, Abruf per ffmpeg (X1/H2/P2-Serie)
MODELS = {
    "P1S": ("P1S", "chamber", False, False),
    "P1P": ("P1P", "chamber", False, False),
    "A1": ("A1", "chamber", False, False),
    "A1MINI": ("A1 mini", "chamber", False, False),
    "X1": ("X1", "rtsp", True, False),
    "X1C": ("X1 Carbon", "rtsp", True, False),
    "X1E": ("X1E", "rtsp", True, False),
    "P2S": ("P2S", "rtsp", False, False),
    "H2S": ("H2S", "rtsp", True, False),
    "H2D": ("H2D", "rtsp", True, True),
    "H2DPRO": ("H2D Pro", "rtsp", True, True),
    "H2C": ("H2C", "rtsp", True, True),
}
MODEL_ALIASES = {"X1CARBON": "X1C", "A1M": "A1MINI", "H2DP": "H2DPRO"}


def resolve_model(value) -> tuple[str | None, str]:
    """'A1 mini', 'x1-carbon', 'H2D Pro' … -> (Code | None, Anzeigename)."""
    raw = str(value or "").strip()
    code = re.sub(r"[^A-Z0-9]", "", raw.upper())
    code = MODEL_ALIASES.get(code, code)
    if code in ("", "OTHER", "AUTO"):
        return None, "Bambu Lab"
    if code in MODELS:
        return code, MODELS[code][0]
    return None, raw or "Bambu Lab"
CARD_FILE = "bambu-printer-card.js"
log = logging.getLogger("bambu-bridge")

STATUS_DE = {
    "IDLE": "Bereit",
    "PREPARE": "Vorbereitung",
    "SLICING": "Slicing",
    "RUNNING": "Druckt",
    "PAUSE": "Pausiert",
    "FINISH": "Fertig",
    "FAILED": "Fehlgeschlagen",
    "UNKNOWN": "Unbekannt",
}
ACTIVE_STATES = ("PREPARE", "SLICING", "RUNNING", "PAUSE")
SPEED_NAMES = {1: "Leise", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
SPEED_BY_NAME = {v.lower(): k for k, v in SPEED_NAMES.items()}


# --------------------------------------------------------------------------- helpers
def _num(value, default=None, cast=float):
    try:
        if value is None or value == "":
            return default
        return cast(float(value)) if cast is int else cast(value)
    except (TypeError, ValueError):
        return default


def _fan_pct(value):
    n = _num(value, None, float)
    if n is None:
        return None
    return max(0, min(100, int(round(n / 15 * 100))))


def deep_merge(dst: dict, src: dict) -> dict:
    """P1-Drucker senden nur Deltas – diese in den Gesamtzustand mergen."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "printer"


def new_client(client_id: str) -> mqtt.Client:
    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
    )


def insecure_tls_context() -> ssl.SSLContext:
    # Der Drucker nutzt ein selbstsigniertes Zertifikat (Bambu CA) -> keine Verifikation im LAN.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ImplicitFTPS(ftplib.FTP_TLS):
    """Bambu-Drucker: FTPS mit implizitem TLS (Port 990) und TLS-Session-Reuse im Datenkanal."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def download(self, path: str, fh) -> None:
        # eigene Schleife ohne TLS-unwrap (der Drucker beantwortet close_notify nicht zuverlässig)
        self.voidcmd("TYPE I")
        conn = self.transfercmd(f"RETR {path}")
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                fh.write(data)
        finally:
            conn.close()
        try:
            self.voidresp()
        except ftplib.all_errors:
            pass


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Verbindung geschlossen")
        buf.extend(chunk)
    return bytes(buf)


# --------------------------------------------------------------------------- printer
class Printer:
    def __init__(self, cfg: dict, bridge: "Bridge"):
        for key in ("host", "serial", "access_code"):
            if not cfg.get(key):
                raise ValueError(f"Drucker-Konfiguration: '{key}' fehlt")
        self.bridge = bridge
        self.name = str(cfg.get("name", "P1S"))
        self.slug = slugify(str(cfg.get("slug") or self.name))
        self.model_code, self.model = resolve_model(cfg.get("model", "P1S"))
        info = MODELS.get(self.model_code or "", (self.model, None, False, False))
        self.has_chamber_sensor = bool(cfg.get("chamber_sensor", info[2]))
        self.dual_nozzle = info[3]
        mode = str(cfg.get("camera_mode", "auto")).lower()
        if mode not in ("auto", "chamber", "rtsp"):
            raise ValueError(f"camera_mode '{mode}' ungültig (auto, chamber, rtsp)")
        # auto: aus dem Modell ableiten, bei unbekanntem Modell per Port-Test ermitteln
        self.camera_type = info[1] if mode == "auto" else mode
        self.ffmpeg = cfg.get("ffmpeg_path") or shutil.which("ffmpeg")
        self.host = str(cfg["host"]).strip()
        self.serial = str(cfg["serial"]).strip()
        self.code = str(cfg["access_code"]).strip()
        self.camera_enabled = bool(cfg.get("camera", True))
        self.camera_interval = float(cfg.get("camera_interval", 2))
        self.pushall_interval = float(cfg.get("pushall_interval", 300))
        self.allow_gcode = bool(cfg.get("allow_gcode", False))
        self.last_print_light = bool(cfg.get("last_print_light", False))
        self.last_print_delay = float(cfg.get("last_print_delay", 5))
        self.preview_enabled = bool(cfg.get("preview", True))

        self.raw: dict = {"print": {}}
        self.lock = threading.RLock()
        self.connected = False
        self.dirty = False
        self.last_message = 0.0
        self.last_pushall = 0.0
        self.last_state_pub = 0.0
        self.seq = 0

        self.jpeg: bytes | None = None
        self.jpeg_ts = 0.0
        self.cam_pub_frame_ts = 0.0
        self.cam_pub_time = 0.0
        self.cam_connected = False
        self.frame_cond = threading.Condition()

        base = bridge.base_topic
        self.t_state = f"{base}/{self.slug}/state"
        self.t_avail = f"{base}/{self.slug}/availability"
        self.t_cmd = f"{base}/{self.slug}/cmd"
        self.t_camera = f"{base}/{self.slug}/camera"
        self.t_cam_attr = f"{base}/{self.slug}/camera_attr"
        self.t_report = f"device/{self.serial}/report"
        self.t_request = f"device/{self.serial}/request"
        self.t_last = f"{base}/{self.slug}/last_print"
        self.t_last_attr = f"{base}/{self.slug}/last_print_attr"
        self.client: mqtt.Client | None = None

        # Letzter Druckauftrag (Screenshot + Metadaten), bleibt über Neustarts erhalten
        self.prev_gs: str | None = None
        self.job_start: float | None = None
        self.job_captured = False
        self.last_jpeg: bytes | None = None
        self.last_meta: dict | None = None
        self.data_dir = bridge.data_dir
        self._load_last_print()

        # 3D-Vorschau (Slicer-Bild aus der .gcode.3mf)
        self.t_preview = f"{base}/{self.slug}/preview"
        self.t_preview_attr = f"{base}/{self.slug}/preview_attr"
        self.preview_png: bytes | None = None
        self.preview_meta: dict | None = None
        self.preview_key: str | None = None
        self.preview_lock = threading.Lock()
        self._load_preview()

    # ---- lifecycle
    def start(self):
        c = new_client(f"mexmox-bambu-{self.slug}-{os.getpid()}")
        c.username_pw_set("bblp", self.code)
        c.tls_set_context(insecure_tls_context())
        c.reconnect_delay_set(2, 60)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        log.info("[%s] Verbinde mit Drucker %s:8883 (Serial %s)", self.name, self.host, self.serial)
        c.connect_async(self.host, 8883, keepalive=30)
        c.loop_start()
        self.client = c
        threading.Thread(target=self._publisher_loop, daemon=True, name=f"pub-{self.slug}").start()
        if self.camera_enabled:
            threading.Thread(target=self._camera_loop, daemon=True, name=f"cam-{self.slug}").start()

    def stop(self):
        if self.client:
            self.client.loop_stop()
            try:
                self.client.disconnect()
            except Exception:
                pass

    # ---- printer MQTT
    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc.is_failure:
            log.error("[%s] Drucker hat Verbindung abgelehnt (%s) – Access Code / LAN-Modus prüfen", self.name, rc)
            return
        log.info("[%s] Mit Drucker verbunden", self.name)
        self.connected = True
        client.subscribe(self.t_report, qos=0)
        self.pushall()
        self.bridge.publish_availability(self)

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        if self.connected:
            log.warning("[%s] Verbindung zum Drucker getrennt (%s)", self.name, rc)
            # Drucker während eines Drucks ausgeschaltet/weg -> letztes Kamerabild sichern
            if self.prev_gs in ACTIVE_STATES and not self.job_captured and self.jpeg:
                threading.Thread(target=self._capture_last_print, args=("Unterbrochen", self._job_info(), True),
                                 daemon=True).start()
        self.connected = False
        self.bridge.publish_availability(self)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        with self.lock:
            pr = data.get("print")
            if isinstance(pr, dict):
                cmd = pr.get("command")
                if cmd in ("push_status", None) or "gcode_state" in pr or "mc_percent" in pr:
                    deep_merge(self.raw.setdefault("print", {}), pr)
                    self._track_job()
                if pr.get("result") and str(pr.get("result")).lower() not in ("success", "ok"):
                    log.warning("[%s] Befehl '%s' fehlgeschlagen: %s %s", self.name, cmd,
                                pr.get("result"), pr.get("reason", ""))
            sysmsg = data.get("system")
            if isinstance(sysmsg, dict) and sysmsg.get("result") and \
                    str(sysmsg.get("result")).lower() not in ("success", "ok"):
                log.warning("[%s] System-Befehl '%s' fehlgeschlagen: %s (Developer Mode aktiv?)",
                            self.name, sysmsg.get("command"), sysmsg.get("result"))
            self.last_message = time.time()
            self.dirty = True

    # ---- letzter Druckauftrag
    def _last_paths(self):
        return (os.path.join(self.data_dir, f"{self.slug}_last_print.jpg"),
                os.path.join(self.data_dir, f"{self.slug}_last_print.json"))

    def _load_last_print(self):
        jp, mp = self._last_paths()
        try:
            with open(jp, "rb") as fh:
                self.last_jpeg = fh.read()
            with open(mp, "r", encoding="utf-8") as fh:
                self.last_meta = json.load(fh)
            log.info("[%s] Letzten Druck geladen: %s", self.name, self.last_meta.get("file") or "?")
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] Letzter Druck konnte nicht geladen werden: %s", self.name, exc)
            self.last_jpeg = self.last_meta = None

    def _job_info(self) -> dict:
        n = self.normalize()
        return {"file": n["file"], "layers": n["total_layers"], "filament": n["active_filament"]}

    def _track_job(self):
        """Erkennt Start/Ende eines Druckauftrags (Aufruf mit gehaltenem Lock)."""
        gs = str(self.raw.get("print", {}).get("gcode_state") or "").upper()
        if not gs or gs == self.prev_gs:
            return
        prev, self.prev_gs = self.prev_gs, gs
        if gs in ACTIVE_STATES and prev not in ACTIVE_STATES:
            self.job_start = time.time()
            self.job_captured = False
            p = self.raw.get("print", {})
            info = {"file": p.get("subtask_name") or "", "gcode_file": p.get("gcode_file") or ""}
            threading.Thread(target=self._fetch_preview, args=(info,), daemon=True).start()
        elif prev in ACTIVE_STATES and gs in ("FINISH", "FAILED", "IDLE"):
            result = {"FINISH": "Fertig", "FAILED": "Fehlgeschlagen", "IDLE": "Abgebrochen"}[gs]
            threading.Thread(target=self._capture_last_print, args=(result, self._job_info()),
                             daemon=True).start()

    def _capture_last_print(self, result: str, info: dict, use_current_frame: bool = False):
        if not self.camera_enabled:
            return
        img = None
        if use_current_frame:
            img = self.jpeg
        else:
            light_switched = False
            if self.last_print_light and self.connected:
                with self.lock:
                    cur = next((item.get("mode") for item in self.raw.get("print", {}).get("lights_report") or []
                                if item.get("node") == "chamber_light"), None)
                if cur != "on":
                    self.handle_command("light", "ON")
                    light_switched = True
            if self.bridge.stop_event.wait(self.last_print_delay):
                return
            t_req = time.time()
            with self.frame_cond:
                self.frame_cond.wait_for(lambda: self.jpeg_ts >= t_req, timeout=10)
                if self.jpeg_ts >= t_req:
                    img = self.jpeg
            if img is None and self.jpeg and time.time() - self.jpeg_ts < 120:
                img = self.jpeg  # Kamera hängt kurz -> jüngstes vorhandenes Bild nehmen
            if light_switched:
                self.handle_command("light", "OFF")
        if not img:
            log.warning("[%s] Kein Kamerabild für den letzten Druck verfügbar", self.name)
            return
        meta = {
            "bambu_serial": self.serial,
            "bambu_slug": self.slug,
            "bambu_role": "last_print",
            "printer_name": self.name,
            "result": result,
            "file": info.get("file") or "",
            "layers": info.get("layers"),
            "filament": info.get("filament"),
            "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "duration_min": int((time.time() - self.job_start) / 60) if self.job_start else None,
        }
        self.job_captured = True
        self.last_jpeg, self.last_meta = img, meta
        jp, mp = self._last_paths()
        try:
            for path, data, mode in ((jp, img, "wb"), (mp, json.dumps(meta, ensure_ascii=False, indent=1), "w")):
                tmp = path + ".tmp"
                with open(tmp, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
                    fh.write(data)
                os.replace(tmp, path)
        except OSError as exc:
            log.warning("[%s] Letzter Druck konnte nicht gespeichert werden (%s) – nur per MQTT verfügbar",
                        self.name, exc)
        self.bridge.publish_last_print(self)
        log.info("[%s] Letzter Druck gespeichert: %s (%s)", self.name, meta["file"] or "?", result)

    # ---- 3D-Vorschau aus der Druckdatei
    def _preview_paths(self):
        return (os.path.join(self.data_dir, f"{self.slug}_preview.png"),
                os.path.join(self.data_dir, f"{self.slug}_preview.json"))

    def _load_preview(self):
        pp, mp = self._preview_paths()
        try:
            with open(pp, "rb") as fh:
                self.preview_png = fh.read()
            with open(mp, "r", encoding="utf-8") as fh:
                self.preview_meta = json.load(fh)
            self.preview_key = self.preview_meta.get("source")
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] Vorschau konnte nicht geladen werden: %s", self.name, exc)
            self.preview_png = self.preview_meta = None

    @staticmethod
    def _find_3mf(ftp: ImplicitFTPS, info: dict) -> str | None:
        want = os.path.basename(str(info.get("gcode_file") or ""))
        sub = str(info.get("file") or "")
        candidates = []
        for directory in ("/", "/cache", "/model"):
            try:
                names = ftp.nlst(directory)
            except ftplib.all_errors:
                continue
            for name in names:
                base = os.path.basename(name)
                if not base.lower().endswith(".3mf"):
                    continue
                full = name if name.startswith("/") else f"{directory.rstrip('/')}/{base}"
                if want and base == want:
                    score = 3
                elif sub and base.startswith(sub + "."):
                    score = 2
                elif sub and sub in base:
                    score = 1
                else:
                    continue
                candidates.append((score, directory == "/cache", full))
        return max(candidates)[2] if candidates else None

    @staticmethod
    def _extract_preview(path: str):
        plate_num = lambda n: int(re.findall(r"\d+", n.rsplit("/", 1)[-1])[0])  # noqa: E731
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            pngs = sorted((n for n in names if re.fullmatch(r"Metadata/plate_\d+\.png", n)), key=plate_num)
            sliced = {plate_num(n) for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)}
            pick = next((n for n in pngs if plate_num(n) in sliced), pngs[0] if pngs else None)
            if not pick:
                return None, {}
            plate = plate_num(pick)
            meta: dict = {"plate": plate}
            if "Metadata/slice_info.config" in names:
                root = ET.fromstring(z.read("Metadata/slice_info.config"))
                plates = list(root.iter("plate"))
                for pl in plates:
                    md = {m.get("key"): m.get("value") for m in pl.findall("metadata")}
                    if len(plates) > 1 and str(md.get("index")) != str(plate):
                        continue
                    pred = _num(md.get("prediction"), None, float)
                    weight = _num(md.get("weight"), None, float)
                    meta["prediction_min"] = round(pred / 60) if pred else None
                    meta["weight_g"] = round(weight, 1) if weight else None
                    meta["filaments"] = [{"type": f.get("type"), "color": f.get("color"),
                                          "used_g": _num(f.get("used_g"), None, float)}
                                         for f in pl.findall("filament")]
                    break
            return z.read(pick), meta

    def _fetch_preview(self, info: dict):
        if not self.preview_enabled:
            return
        key = info.get("gcode_file") or info.get("file")
        if not key or key == self.preview_key or not self.preview_lock.acquire(blocking=False):
            return
        tmp = os.path.join(self.data_dir, f"{self.slug}_download.3mf.tmp")
        try:
            ftp = ImplicitFTPS(context=insecure_tls_context(), timeout=30)
            ftp.connect(self.host, 990)
            ftp.login("bblp", self.code)
            ftp.prot_p()
            path = self._find_3mf(ftp, info)
            if not path:
                log.warning("[%s] Druckdatei für die Vorschau nicht gefunden (%s)", self.name, key)
                ftp.close()
                return
            with open(tmp, "wb") as fh:
                ftp.download(path, fh)
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()
            png, plate_meta = self._extract_preview(tmp)
            if not png:
                log.warning("[%s] Kein Vorschaubild in %s", self.name, path)
                return
            meta = {
                "bambu_serial": self.serial,
                "bambu_slug": self.slug,
                "bambu_role": "preview",
                "printer_name": self.name,
                "file": info.get("file") or os.path.basename(path).split(".")[0],
                "source": key,
                "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                **plate_meta,
            }
            self.preview_png, self.preview_meta, self.preview_key = png, meta, key
            pp, mp = self._preview_paths()
            for target, data, mode in ((pp, png, "wb"), (mp, json.dumps(meta, ensure_ascii=False, indent=1), "w")):
                with open(target + ".tmp", mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
                    fh.write(data)
                os.replace(target + ".tmp", target)
            self.bridge.publish_preview(self)
            log.info("[%s] 3D-Vorschau geladen: %s (Platte %s)", self.name, meta["file"], meta.get("plate"))
        except (*ftplib.all_errors, zipfile.BadZipFile, ET.ParseError) as exc:
            log.warning("[%s] Vorschau konnte nicht geladen werden: %s", self.name, exc)
        finally:
            self.preview_lock.release()
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _next_seq(self) -> str:
        self.seq = (self.seq + 1) % 100000
        return str(self.seq)

    def send(self, payload: dict) -> bool:
        if not self.client or not self.connected:
            log.warning("[%s] Befehl verworfen – Drucker nicht verbunden", self.name)
            return False
        for section in payload.values():
            if isinstance(section, dict):
                section["sequence_id"] = self._next_seq()
        log.debug("[%s] -> %s", self.name, payload)
        self.client.publish(self.t_request, json.dumps(payload), qos=0)
        return True

    def pushall(self):
        self.last_pushall = time.time()
        if self.client and self.connected:
            self.client.publish(self.t_request, json.dumps({
                "pushing": {"sequence_id": self._next_seq(), "command": "pushall",
                            "version": 1, "push_target": 1}
            }))

    # ---- commands from Home Assistant / dashboard
    def handle_command(self, action: str, payload: str):
        payload = (payload or "").strip()
        log.info("[%s] Befehl: %s %s", self.name, action, payload if action != "gcode" else "")
        if action in ("pause", "resume", "stop"):
            self.send({"print": {"command": action, "param": ""}})
        elif action == "light":
            on = payload.upper() in ("ON", "1", "TRUE", "AN")
            if self.send({"system": {
                "command": "ledctrl", "led_node": "chamber_light", "led_mode": "on" if on else "off",
                "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0,
            }}):
                with self.lock:  # optimistisch, Drucker bestätigt mit nächstem Report
                    self.raw.setdefault("print", {})["lights_report"] = [
                        {"node": "chamber_light", "mode": "on" if on else "off"}]
                    self.dirty = True
        elif action == "speed":
            lvl = _num(payload, None, int)
            if lvl is None:
                lvl = SPEED_BY_NAME.get(payload.lower())
            if lvl not in SPEED_NAMES:
                log.warning("[%s] Ungültige Geschwindigkeit: %r", self.name, payload)
                return
            if self.send({"print": {"command": "print_speed", "param": str(lvl)}}):
                with self.lock:
                    self.raw.setdefault("print", {})["spd_lvl"] = lvl
                    self.dirty = True
        elif action in ("refresh", "pushall"):
            self.pushall()
        elif action == "gcode":
            if not self.allow_gcode:
                log.warning("[%s] G-Code abgelehnt – allow_gcode ist deaktiviert", self.name)
                return
            if payload:
                self.send({"print": {"command": "gcode_line", "param": payload.rstrip("\n") + "\n"}})
        else:
            log.warning("[%s] Unbekannter Befehl: %s", self.name, action)

    # ---- state normalisation
    @staticmethod
    def _tray(t: dict, idx: int, active: bool) -> dict:
        ttype = t.get("tray_type") or ""
        color = str(t.get("tray_color") or "")[:6]
        remain = _num(t.get("remain"), -1, int)
        return {
            "index": idx,
            "slot": (idx % 4) + 1 if idx < 128 else None,
            "type": ttype,
            "brand": t.get("tray_sub_brands") or "",
            "color": f"#{color.upper()}" if len(color) == 6 else None,
            "remain": remain if remain is not None and remain >= 0 else None,
            "empty": not ttype,
            "active": active,
            "nozzle_min": _num(t.get("nozzle_temp_min"), None, int),
            "nozzle_max": _num(t.get("nozzle_temp_max"), None, int),
        }

    def normalize(self) -> dict:
        with self.lock:
            p = json.loads(json.dumps(self.raw.get("print", {})))

        gs = str(p.get("gcode_state") or "UNKNOWN").upper()
        progress = _num(p.get("mc_percent"), 0, int)
        remaining = _num(p.get("mc_remaining_time"), 0, int)
        active = gs in ACTIVE_STATES
        end_time = None
        if active and remaining:
            et = datetime.now(timezone.utc) + timedelta(minutes=remaining)
            end_time = et.replace(second=0, microsecond=0).isoformat()

        light = None
        for item in p.get("lights_report") or []:
            if item.get("node") == "chamber_light":
                light = item.get("mode")

        wifi = _num(str(p.get("wifi_signal", "")).replace("dBm", ""), None, int)
        spd = _num(p.get("spd_lvl"), 2, int)

        ams_root = p.get("ams") or {}
        tray_now = _num(ams_root.get("tray_now"), 255, int)
        units = []
        for u in ams_root.get("ams") or []:
            uid = _num(u.get("id"), 0, int)
            is_ht = uid >= 128                       # AMS HT (H2-Serie): IDs ab 128, 1 Slot
            trays = []
            for t in u.get("tray") or []:
                tid = _num(t.get("id"), 0, int)
                gidx = uid if is_ht else uid * 4 + tid
                tray = self._tray(t, gidx, gidx == tray_now)
                tray["slot"] = tid + 1
                trays.append(tray)
            trays.sort(key=lambda x: x["index"])
            units.append({
                "id": uid,
                "label": f"AMS HT {uid - 127}" if is_ht else f"AMS {uid + 1}",
                "humidity_level": _num(u.get("humidity"), None, int),
                "humidity": _num(u.get("humidity_raw"), None, int),
                "temp": _num(u.get("temp"), None, float),
                "trays": trays,
            })
        vt = p.get("vt_tray")
        external = self._tray(vt, 254, tray_now == 254) if isinstance(vt, dict) else None

        active_tray = None
        for u in units:
            for t in u["trays"]:
                if t["active"]:
                    active_tray = t
        if external and external["active"]:
            active_tray = external
        active_filament = "—"
        if active_tray and active_tray["type"]:
            active_filament = active_tray["type"] + (f" ({active_tray['brand']})" if active_tray["brand"] else "")

        hms = []
        for h in p.get("hms") or []:
            a = _num(h.get("attr"), 0, int)
            c = _num(h.get("code"), 0, int)
            code = f"{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}"
            hms.append({"code": code, "url": f"https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/{code}"})
        print_error = _num(p.get("print_error"), 0, int) or 0

        # H2-Serie: Temperaturen je Extruder in device.extruder.info[].temp
        # (Bit-gepackt: untere 16 Bit = Ist, obere 16 Bit = Soll)
        def unpack(v):
            n = _num(v, None, int)
            if n is None:
                return None, None
            return (n & 0xFFFF, n >> 16) if n > 0xFFFF else (n, None)

        device = p.get("device") or {}
        nozzles = []
        for i, e in enumerate((device.get("extruder") or {}).get("info") or []):
            cur, tgt = unpack(e.get("temp"))
            if cur is not None:
                nozzles.append({"id": _num(e.get("id"), i, int), "temp": cur, "target": tgt})
        nozzle_temp = _num(p.get("nozzle_temper"), None, lambda v: round(float(v), 1))
        nozzle_target = _num(p.get("nozzle_target_temper"), None, lambda v: round(float(v)))
        if nozzle_temp is None and nozzles:
            nozzle_temp, nozzle_target = nozzles[0]["temp"], nozzles[0]["target"]

        chamber_temp = None
        if self.has_chamber_sensor:
            chamber_temp = _num(p.get("chamber_temper"), None, lambda v: round(float(v), 1))
            if chamber_temp is None:
                chamber_temp = unpack(((device.get("ctc") or {}).get("info") or {}).get("temp"))[0]

        return {
            "bambu_bridge": True,
            "name": self.name,
            "slug": self.slug,
            "serial": self.serial,
            "model": self.model,
            "cmd_topic": self.t_cmd,
            "gcode_allowed": self.allow_gcode,
            "gcode_state": gs,
            "status": STATUS_DE.get(gs, gs.title()),
            "printing": active,
            "progress": progress,
            "remaining_min": remaining if active else 0,
            "end_time": end_time,
            "file": p.get("subtask_name") or p.get("gcode_file") or "",
            "layer": _num(p.get("layer_num"), None, int),
            "total_layers": _num(p.get("total_layer_num"), None, int),
            "nozzle_temp": nozzle_temp,
            "nozzle_target": nozzle_target,
            "nozzles": nozzles if len(nozzles) > 1 else [],
            "chamber_temp": chamber_temp,
            "bed_temp": _num(p.get("bed_temper"), None, lambda v: round(float(v), 1)),
            "bed_target": _num(p.get("bed_target_temper"), None, lambda v: round(float(v))),
            "nozzle_diameter": p.get("nozzle_diameter"),
            "speed_level": spd,
            "speed_name": SPEED_NAMES.get(spd, "Standard"),
            "speed_percent": _num(p.get("spd_mag"), None, int),
            "fan_part": _fan_pct(p.get("cooling_fan_speed")),
            "fan_aux": _fan_pct(p.get("big_fan1_speed")),
            "fan_chamber": _fan_pct(p.get("big_fan2_speed")),
            "fan_heatbreak": _fan_pct(p.get("heatbreak_fan_speed")),
            "light": light,
            "wifi": wifi,
            "ams": units,
            "external_spool": external,
            "active_filament": active_filament,
            "hms": hms,
            "print_error": f"{print_error:08X}" if print_error else None,
            "has_error": bool(hms) or bool(print_error) or gs == "FAILED",
            "camera_online": self.cam_connected,
            "camera_type": self.camera_type,
            "updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    # ---- loops
    def _publisher_loop(self):
        while not self.bridge.stop_event.is_set():
            now = time.time()
            if self.connected and now - self.last_pushall >= self.pushall_interval:
                self.pushall()
            if self.dirty or (self.connected and now - self.last_state_pub >= 60):
                self.dirty = False
                self.last_state_pub = now
                self.bridge.publish_state(self)
            if (self.camera_enabled and self.jpeg and self.jpeg_ts > self.cam_pub_frame_ts
                    and now - self.cam_pub_time >= self.camera_interval):
                self.cam_pub_frame_ts = self.jpeg_ts
                self.cam_pub_time = now
                self.bridge.publish_camera(self, self.jpeg)
            self.bridge.stop_event.wait(1.0)

    def _set_frame(self, img: bytes):
        if not self.cam_connected:
            log.info("[%s] Kamera verbunden (%s)", self.name, self.camera_type)
            self.cam_connected = True
            self.dirty = True
        with self.frame_cond:
            self.jpeg = img
            self.jpeg_ts = time.time()
            self.frame_cond.notify_all()

    def _probe_camera(self) -> str | None:
        """Unbekanntes Modell: Kamera-Typ über offene Ports bestimmen."""
        for port, kind in ((6000, "chamber"), (322, "rtsp")):
            try:
                with socket.create_connection((self.host, port), timeout=4):
                    return kind
            except OSError:
                continue
        return None

    def _camera_loop(self):
        while self.camera_type is None and not self.bridge.stop_event.is_set():
            self.camera_type = self._probe_camera()
            if self.camera_type:
                log.info("[%s] Kamera-Typ erkannt: %s", self.name, self.camera_type)
                self.dirty = True
            else:
                log.warning("[%s] Kamera-Port (6000/322) nicht erreichbar – neuer Versuch in 60 s", self.name)
                self.bridge.stop_event.wait(60)
        if self.camera_type == "rtsp":
            self._camera_rtsp()
        elif self.camera_type == "chamber":
            self._camera_chamber()

    def _camera_rtsp(self):
        """X1/H2/P2-Kamera: RTSPS auf Port 322, Einzelbilder per ffmpeg als MJPEG-Strom."""
        if not self.ffmpeg:
            log.error("[%s] ffmpeg nicht gefunden – für die Kamera dieses Modells erforderlich "
                      "(apt install ffmpeg / im Add-on enthalten)", self.name)
            return
        url = f"rtsps://bblp:{quote(self.code, safe='')}@{self.host}:322/streaming/live/1"
        fps = max(0.2, min(5.0, 1.0 / max(self.camera_interval, 0.2)))
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
               "-i", url, "-an", "-vf", f"fps={fps:g}", "-q:v", "5",
               "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        backoff = 5
        while not self.bridge.stop_event.is_set():
            proc = None
            err_lines: list[str] = []
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                threading.Thread(target=lambda: err_lines.extend(
                    line.decode("utf-8", "replace").strip() for line in proc.stderr), daemon=True).start()
                buf = b""
                while not self.bridge.stop_event.is_set():
                    chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                        if start < 0:
                            buf = buf[-1:]          # kein Bildanfang: Rest verwerfen
                            break
                        if end < 0:
                            buf = buf[start:]       # Bild noch unvollständig
                            break
                        self._set_frame(buf[start:end + 2])
                        backoff = 5
                        buf = buf[end + 2:]
                    if len(buf) > 10_000_000:
                        buf = b""
            except OSError as exc:
                err_lines.append(str(exc))
            finally:
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            if self.bridge.stop_event.is_set():
                return
            if self.cam_connected:
                self.dirty = True
            self.cam_connected = False
            reason = err_lines[-1] if err_lines else "Stream beendet"
            log.warning("[%s] Kamera (RTSPS): %s – neuer Versuch in %ss "
                        "(LAN-Live-Ansicht am Drucker aktiv?)", self.name, reason.replace(self.code, "***"), backoff)
            self.bridge.stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _camera_chamber(self):
        """P1/A1 Kamera: TLS-Socket auf Port 6000, 80-Byte Auth-Paket, danach 16-Byte-Header + JPEG."""
        auth = (struct.pack("<IIII", 0x40, 0x3000, 0, 0)
                + b"bblp".ljust(32, b"\0")
                + self.code.encode().ljust(32, b"\0"))
        ctx = insecure_tls_context()
        backoff = 5
        while not self.bridge.stop_event.is_set():
            try:
                with socket.create_connection((self.host, 6000), timeout=10) as raw:
                    with ctx.wrap_socket(raw, server_hostname=None) as s:
                        s.settimeout(20)
                        s.sendall(auth)
                        while not self.bridge.stop_event.is_set():
                            hdr = _recv_exact(s, 16)
                            size = struct.unpack_from("<I", hdr, 0)[0]
                            if size <= 0 or size > 8_000_000:
                                raise ValueError(f"Ungültige Framegröße {size}")
                            img = _recv_exact(s, size)
                            if img[:2] == b"\xff\xd8" and img[-2:] == b"\xff\xd9":
                                backoff = 5
                                self._set_frame(img)
            except Exception as exc:  # noqa: BLE001
                if self.cam_connected:
                    self.dirty = True
                self.cam_connected = False
                log.warning("[%s] Kamera: %s – neuer Versuch in %ss", self.name, exc, backoff)
                self.bridge.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)


# --------------------------------------------------------------------------- bridge
class Bridge:
    def __init__(self, cfg: dict):
        ha = cfg.get("homeassistant") or {}
        self.ha_cfg = ha
        self.base_topic = str(ha.get("base_topic", "bambu")).strip("/")
        self.prefix = str(ha.get("discovery_prefix", "homeassistant")).strip("/")
        self.legacy_object_id = bool(ha.get("legacy_object_id", False))
        self.t_bridge_status = f"{self.base_topic}/bridge/status"
        self.stop_event = threading.Event()

        # Speicherort für "Letzter Druck" (systemd setzt STATE_DIRECTORY, Docker BAMBU_DATA)
        data_dir = (cfg.get("data_dir") or os.environ.get("BAMBU_DATA")
                    or os.environ.get("STATE_DIRECTORY", "").split(":")[0] or "data")
        self.data_dir = os.path.abspath(str(data_dir))
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError as exc:
            log.warning("Datenordner %s nicht anlegbar (%s)", self.data_dir, exc)

        printers = cfg.get("printers") or []
        if not printers:
            raise ValueError("Keine Drucker in der Konfiguration")
        self.printers = [Printer(p, self) for p in printers]
        self.by_slug = {p.slug: p for p in self.printers}
        if len(self.by_slug) != len(self.printers):
            raise ValueError("Drucker-Slugs/Namen müssen eindeutig sein")

        self.ha = new_client(str(ha.get("client_id", "mexmox-bambu-bridge")))
        if ha.get("username"):
            self.ha.username_pw_set(str(ha["username"]), str(ha.get("password", "")))
        if ha.get("tls"):
            self.ha.tls_set_context(ssl.create_default_context() if ha.get("tls_verify", True)
                                    else insecure_tls_context())
        self.ha.will_set(self.t_bridge_status, "offline", qos=1, retain=True)
        self.ha.reconnect_delay_set(2, 60)
        self.ha.on_connect = self._ha_connect
        self.ha.on_disconnect = self._ha_disconnect
        self.ha.on_message = self._ha_message
        self.ha_connected = False

    # ---- HA broker
    def start(self):
        host = self.ha_cfg.get("host", "localhost")
        port = int(self.ha_cfg.get("port", 1883))
        log.info("Verbinde mit Home-Assistant-Broker %s:%s", host, port)
        self.ha.connect_async(host, port, keepalive=60)
        self.ha.loop_start()
        for p in self.printers:
            p.start()

    def stop(self):
        self.stop_event.set()
        for p in self.printers:
            p.stop()
        if self.ha_connected:
            self.ha.publish(self.t_bridge_status, "offline", qos=1, retain=True).wait_for_publish(2)
        self.ha.loop_stop()
        self.ha.disconnect()

    def _ha_connect(self, client, userdata, flags, rc, props=None):
        if rc.is_failure:
            log.error("Home-Assistant-Broker hat Verbindung abgelehnt: %s (Benutzer/Passwort?)", rc)
            return
        log.info("Mit Home-Assistant-Broker verbunden")
        self.ha_connected = True
        client.publish(self.t_bridge_status, "online", qos=1, retain=True)
        client.subscribe(f"{self.base_topic}/+/cmd/#", qos=0)
        client.subscribe(f"{self.prefix}/status", qos=0)
        for p in self.printers:
            self.publish_discovery(p)
            self.publish_availability(p)
            self.publish_last_print(p)
            self.publish_preview(p)
            p.dirty = True

    def _ha_disconnect(self, client, userdata, flags, rc, props=None):
        if self.ha_connected:
            log.warning("Verbindung zum Home-Assistant-Broker getrennt (%s)", rc)
        self.ha_connected = False

    def _ha_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace")
        if topic == f"{self.prefix}/status":
            if payload == "online":
                log.info("Home Assistant neu gestartet – sende Discovery erneut")
                for p in self.printers:
                    self.publish_discovery(p)
                    self.publish_availability(p)
                    self.publish_last_print(p)
                    self.publish_preview(p)
                    p.dirty = True
            return
        parts = topic.split("/")
        base_len = len(self.base_topic.split("/"))
        if len(parts) == base_len + 3 and parts[base_len + 1] == "cmd":
            printer = self.by_slug.get(parts[base_len])
            if printer:
                printer.handle_command(parts[base_len + 2], payload)

    # ---- publishing
    def publish_availability(self, p: Printer):
        if self.ha_connected:
            self.ha.publish(p.t_avail, "online" if p.connected else "offline", qos=1, retain=True)

    def publish_state(self, p: Printer):
        if self.ha_connected:
            self.ha.publish(p.t_state, json.dumps(p.normalize(), ensure_ascii=False), qos=0, retain=True)

    def publish_last_print(self, p: Printer):
        if self.ha_connected and p.camera_enabled and p.last_jpeg and p.last_meta:
            self.ha.publish(p.t_last_attr, json.dumps(p.last_meta, ensure_ascii=False), qos=1, retain=True)
            self.ha.publish(p.t_last, p.last_jpeg, qos=1, retain=True)

    def publish_preview(self, p: Printer):
        if self.ha_connected and p.preview_enabled and p.preview_png and p.preview_meta:
            self.ha.publish(p.t_preview_attr, json.dumps(p.preview_meta, ensure_ascii=False), qos=1, retain=True)
            self.ha.publish(p.t_preview, p.preview_png, qos=1, retain=True)

    def publish_camera(self, p: Printer, jpeg: bytes):
        if self.ha_connected:
            self.ha.publish(p.t_camera, jpeg, qos=0, retain=False)

    def publish_discovery(self, p: Printer):
        dev = {
            "identifiers": [f"bambu_{p.serial}"],
            "name": f"Bambu {p.name}",
            "manufacturer": "Bambu Lab",
            "model": p.model,
            "serial_number": p.serial,
        }
        origin = {"name": "MexMox Bambu Bridge", "sw_version": VERSION}
        avail = [{"topic": self.t_bridge_status}, {"topic": p.t_avail}]

        def ent(component: str, key: str, name: str, **extra):
            cfg = {
                "name": name,
                "unique_id": f"bambu_{p.serial}_{key}",
                "device": dev,
                "origin": origin,
                "availability": avail,
                "availability_mode": "all",
            }
            if self.legacy_object_id:
                cfg["object_id"] = f"bambu_{p.slug}_{key}"
            else:
                cfg["default_entity_id"] = f"{component}.bambu_{p.slug}_{key}"
            cfg.update(extra)
            self.ha.publish(f"{self.prefix}/{component}/bambu_{p.serial}/{key}/config",
                            json.dumps(cfg, ensure_ascii=False), qos=1, retain=True)

        st = p.t_state

        def sensor(key, name, tpl, **extra):
            ent("sensor", key, name, state_topic=st, value_template=tpl, **extra)

        sensor("status", "Status", "{{ value_json.status }}", icon="mdi:printer-3d",
               json_attributes_topic=st)
        sensor("progress", "Fortschritt", "{{ value_json.progress }}", unit_of_measurement="%",
               state_class="measurement", icon="mdi:progress-clock")
        sensor("remaining", "Restzeit", "{{ value_json.remaining_min }}", unit_of_measurement="min",
               device_class="duration", icon="mdi:timer-sand")
        sensor("end_time", "Fertig um", "{{ value_json.end_time if value_json.end_time else None }}",
               device_class="timestamp")
        sensor("layer", "Schicht", "{{ value_json.layer }}", icon="mdi:layers-outline")
        sensor("total_layers", "Schichten gesamt", "{{ value_json.total_layers }}", icon="mdi:layers")
        sensor("file", "Datei", "{{ value_json.file[:250] if value_json.file else None }}", icon="mdi:file-cad")
        for key, name in (("nozzle_temp", "Düse"), ("nozzle_target", "Düse Ziel"),
                          ("bed_temp", "Druckbett"), ("bed_target", "Druckbett Ziel")):
            sensor(key, name, "{{ value_json.%s }}" % key, unit_of_measurement="°C",
                   device_class="temperature", state_class="measurement")
        for key, name in (("fan_part", "Bauteillüfter"), ("fan_aux", "Hilfslüfter"),
                          ("fan_chamber", "Kammerlüfter")):
            sensor(key, name, "{{ value_json.%s }}" % key, unit_of_measurement="%", icon="mdi:fan")
        if p.has_chamber_sensor:
            sensor("chamber_temp", "Bauraum", "{{ value_json.chamber_temp }}", unit_of_measurement="°C",
                   device_class="temperature", state_class="measurement")
        if p.dual_nozzle:
            for idx, side in ((0, "rechts"), (1, "links")):
                sensor(f"nozzle{idx + 1}_temp", f"Düse {side}",
                       "{{ value_json.nozzles[%d].temp if value_json.nozzles | length > %d else None }}" % (idx, idx),
                       unit_of_measurement="°C", device_class="temperature", state_class="measurement")
        sensor("active_filament", "Aktives Filament", "{{ value_json.active_filament }}", icon="mdi:printer-3d-nozzle")
        sensor("wifi", "WLAN-Signal", "{{ value_json.wifi }}", unit_of_measurement="dBm",
               device_class="signal_strength", entity_category="diagnostic")

        ent("binary_sensor", "printing", "Druck aktiv", state_topic=st, device_class="running",
            value_template="{{ 'ON' if value_json.printing else 'OFF' }}")
        ent("binary_sensor", "error", "Fehler", state_topic=st, device_class="problem",
            value_template="{{ 'ON' if value_json.has_error else 'OFF' }}")

        ent("light", "chamber_light", "Kammerlicht", command_topic=f"{p.t_cmd}/light",
            state_topic=st, state_value_template="{{ 'ON' if value_json.light == 'on' else 'OFF' }}",
            payload_on="ON", payload_off="OFF")
        ent("select", "speed", "Geschwindigkeit", command_topic=f"{p.t_cmd}/speed", state_topic=st,
            value_template="{{ value_json.speed_name }}", options=list(SPEED_NAMES.values()),
            icon="mdi:speedometer")
        for key, name, icon in (("pause", "Pausieren", "mdi:pause"), ("resume", "Fortsetzen", "mdi:play"),
                                ("stop", "Stoppen", "mdi:stop"), ("refresh", "Aktualisieren", "mdi:refresh")):
            ent("button", key, name, command_topic=f"{p.t_cmd}/{key}", payload_press="PRESS", icon=icon)

        if p.camera_enabled:
            ent("camera", "camera", "Kamera", topic=p.t_camera, json_attributes_topic=p.t_cam_attr)
            self.ha.publish(p.t_cam_attr, json.dumps({"bambu_serial": p.serial, "bambu_slug": p.slug,
                                                      "bambu_role": "live"}), qos=1, retain=True)
            # Letzter Druck: nur von der Bridge abhängig -> bleibt bei ausgeschaltetem Drucker verfügbar
            ent("camera", "last_print", "Letzter Druck", topic=p.t_last, json_attributes_topic=p.t_last_attr,
                availability=[{"topic": self.t_bridge_status}], icon="mdi:image-check")
        if p.preview_enabled:
            ent("camera", "preview", "3D-Vorschau", topic=p.t_preview, json_attributes_topic=p.t_preview_attr,
                availability=[{"topic": self.t_bridge_status}], icon="mdi:cube-outline")
        log.info("[%s] Discovery an Home Assistant gesendet", p.name)


# --------------------------------------------------------------------------- optional HTTP (MJPEG)
def make_http_handler(bridge: Bridge, http_cfg: dict | None = None):
    http_cfg = http_cfg or {}
    allow = set(http_cfg.get("allow") or [])          # leer = alle erlaubt (Standalone)
    www_dir = http_cfg.get("www_dir")                 # Add-on: Dashboard für Ingress

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _serve_file(self, name: str, ctype: str):
            path = os.path.join(www_dir, name) if www_dir else None
            if not path or not os.path.isfile(path):
                return self._send(404, "text/plain", b"not found")
            with open(path, "rb") as fh:
                return self._send(200, ctype, fh.read())

        def _send(self, code, ctype, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if allow and self.client_address[0] not in allow:
                return self._send(403, "text/plain", b"forbidden")
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts in ([""], ["index.html"]):
                return self._serve_file("index.html", "text/html; charset=utf-8")
            if parts == [CARD_FILE]:
                return self._serve_file(CARD_FILE, "application/javascript; charset=utf-8")
            if parts == ["health"]:
                body = {"version": VERSION, "ha_connected": bridge.ha_connected,
                        "printers": {p.slug: {"connected": p.connected, "camera": p.cam_connected}
                                     for p in bridge.printers}}
                return self._send(200, "application/json", json.dumps(body).encode())
            if len(parts) != 2 or parts[0] not in bridge.by_slug:
                return self._send(404, "text/plain", b"not found")
            p = bridge.by_slug[parts[0]]
            if parts[1] == "state.json":
                return self._send(200, "application/json",
                                  json.dumps(p.normalize(), ensure_ascii=False).encode())
            if parts[1] == "snapshot.jpg":
                if not p.jpeg:
                    return self._send(503, "text/plain", b"kein Kamerabild")
                return self._send(200, "image/jpeg", p.jpeg)
            if parts[1] == "stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last = 0.0
                try:
                    while not bridge.stop_event.is_set():
                        with p.frame_cond:
                            p.frame_cond.wait_for(lambda: p.jpeg_ts > last, timeout=30)
                            img, last = p.jpeg, p.jpeg_ts
                        if not img:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(img)}\r\n\r\n".encode())
                        self.wfile.write(img + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self._send(404, "text/plain", b"not found")

    return Handler


# --------------------------------------------------------------------------- main
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main():
    path = os.environ.get("BAMBU_CONFIG") or (sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    try:
        cfg = load_config(path)
    except FileNotFoundError:
        print(f"Konfiguration nicht gefunden: {path}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    log.info("MexMox Bambu Bridge %s startet (%s)", VERSION, path)

    try:
        bridge = Bridge(cfg)
    except ValueError as exc:
        log.error("Konfigurationsfehler: %s", exc)
        sys.exit(1)

    http_cfg = cfg.get("http") or {}
    if http_cfg.get("enabled"):
        bind = str(http_cfg.get("bind", "0.0.0.0"))
        port = int(http_cfg.get("port", 8099))
        srv = ThreadingHTTPServer((bind, port), make_http_handler(bridge, http_cfg))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
        log.info("HTTP-Server aktiv auf %s:%s (/<slug>/stream.mjpg, /<slug>/snapshot.jpg)", bind, port)

    def shutdown(signum, frame):
        log.info("Beende …")
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    bridge.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
