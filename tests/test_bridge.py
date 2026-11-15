"""Unit-Tests der Bridge: alle Druckerserien, Normalisierung, Befehle, Vorschau, Discovery, HTTP."""
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from http.server import ThreadingHTTPServer

import pytest

from conftest import FakeMQTT, FakeMsg, bridge


# --------------------------------------------------------------------------- Beispiel-Reports
def p1s_report(**over):
    data = {
        "command": "push_status", "gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 95,
        "subtask_name": "Gridfinity_Bin_2x3", "gcode_file": "Gridfinity_Bin_2x3.gcode.3mf",
        "layer_num": 57, "total_layer_num": 210,
        "nozzle_temper": 219.6, "nozzle_target_temper": 220, "bed_temper": 54.9, "bed_target_temper": 55,
        "chamber_temper": 25, "spd_lvl": 2, "spd_mag": 100,
        "cooling_fan_speed": "15", "big_fan1_speed": "6", "big_fan2_speed": "0", "heatbreak_fan_speed": "15",
        "wifi_signal": "-52dBm", "print_error": 0,
        "lights_report": [{"node": "chamber_light", "mode": "on"}],
        "hms": [{"attr": 0x03000100, "code": 0x00010007}],
        "ams": {"tray_now": "1", "ams": [{"id": "0", "humidity": "4", "humidity_raw": "21", "temp": "24.3", "tray": [
            {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 80, "tray_sub_brands": "PLA Basic"},
            {"id": "1", "tray_type": "PETG", "tray_color": "1F7A3DFF", "remain": 35, "tray_sub_brands": "PETG HF"},
            {"id": "2"},
            {"id": "3", "tray_type": "PLA", "tray_color": "000000FF", "remain": -1}]}]},
        "vt_tray": {"id": "254", "tray_type": "", "tray_color": "00000000"},
    }
    data.update(over)
    return data


def feed(p, data):
    p._on_message(None, None, FakeMsg({"print": data}))


# --------------------------------------------------------------------------- Modelle
@pytest.mark.parametrize("value,code,name", [
    ("P1S", "P1S", "P1S"), ("p1p", "P1P", "P1P"), ("A1", "A1", "A1"), ("A1 mini", "A1MINI", "A1 mini"),
    ("a1-mini", "A1MINI", "A1 mini"), ("X1", "X1", "X1"), ("X1 Carbon", "X1C", "X1 Carbon"),
    ("x1c", "X1C", "X1 Carbon"), ("X1E", "X1E", "X1E"), ("P2S", "P2S", "P2S"), ("H2S", "H2S", "H2S"),
    ("H2D", "H2D", "H2D"), ("H2D Pro", "H2DPRO", "H2D Pro"), ("H2C", "H2C", "H2C"),
    ("OTHER", None, "Bambu Lab"), ("", None, "Bambu Lab"), ("Zukunftsdrucker", None, "Zukunftsdrucker"),
])
def test_resolve_model(value, code, name):
    assert bridge.resolve_model(value) == (code, name)


@pytest.mark.parametrize("model,camera", [
    ("P1S", "chamber"), ("P1P", "chamber"), ("A1", "chamber"), ("A1MINI", "chamber"),
    ("X1", "rtsp"), ("X1C", "rtsp"), ("X1E", "rtsp"), ("P2S", "rtsp"),
    ("H2S", "rtsp"), ("H2D", "rtsp"), ("H2DPRO", "rtsp"), ("H2C", "rtsp"), ("OTHER", None),
])
def test_camera_type_follows_model(make_bridge, model, camera):
    _, p = make_bridge(model=model)
    assert p.camera_type == camera


def test_camera_mode_override_and_validation(make_bridge):
    _, p = make_bridge(model="P1S", camera_mode="rtsp")
    assert p.camera_type == "rtsp"
    with pytest.raises(ValueError):
        make_bridge(model="P1S", camera_mode="webcam")


# --------------------------------------------------------------------------- Normalisierung
def test_normalize_p1s_full_report(make_bridge):
    _, p = make_bridge(model="P1S")
    feed(p, p1s_report())
    s = p.normalize()
    assert s["status"] == "Druckt" and s["printing"] is True
    assert s["progress"] == 42 and s["remaining_min"] == 95
    assert datetime.fromisoformat(s["end_time"]).tzinfo is not None
    assert s["layer"] == 57 and s["total_layers"] == 210
    assert s["nozzle_temp"] == 219.6 and s["nozzle_target"] == 220
    assert s["fan_part"] == 100 and s["fan_aux"] == 40 and s["fan_chamber"] == 0
    assert s["wifi"] == -52 and s["light"] == "on"
    assert s["speed_name"] == "Standard"
    assert s["chamber_temp"] is None, "P1S hat keinen Kammersensor"
    assert s["active_filament"] == "PETG (PETG HF)"
    assert s["hms"][0]["code"] == "0300_0100_0001_0007" and s["has_error"] is True
    unit = s["ams"][0]
    assert unit["label"] == "AMS 1" and unit["humidity"] == 21 and unit["humidity_level"] == 4
    assert [t["color"] for t in unit["trays"]] == ["#FFFFFF", "#1F7A3D", None, "#000000"]
    assert unit["trays"][2]["empty"] is True and unit["trays"][3]["remain"] is None
    assert s["external_spool"]["empty"] is True
    assert s["camera_type"] == "chamber"


def test_delta_updates_are_merged(make_bridge):
    _, p = make_bridge()
    feed(p, p1s_report())
    feed(p, {"command": "push_status", "mc_percent": 77, "layer_num": 120})
    s = p.normalize()
    assert s["progress"] == 77 and s["layer"] == 120
    assert s["nozzle_temp"] == 219.6 and len(s["ams"][0]["trays"]) == 4, "alte Werte bleiben erhalten"


def test_command_replies_do_not_overwrite_state(make_bridge):
    _, p = make_bridge()
    feed(p, p1s_report())
    feed(p, {"command": "pause", "result": "success", "sequence_id": "9"})
    assert p.normalize()["status"] == "Druckt"


def test_idle_and_finish_states(make_bridge):
    _, p = make_bridge()
    feed(p, p1s_report(gcode_state="FINISH", mc_percent=100))
    s = p.normalize()
    assert s["status"] == "Fertig" and s["printing"] is False and s["end_time"] is None and s["remaining_min"] == 0


def test_a1_ams_lite_without_humidity(make_bridge):
    _, p = make_bridge(model="A1")
    report = p1s_report()
    unit = report["ams"]["ams"][0]
    for key in ("humidity", "humidity_raw", "temp"):
        unit.pop(key)
    feed(p, report)
    u = p.normalize()["ams"][0]
    assert u["humidity"] is None and u["humidity_level"] is None and u["temp"] is None


def test_x1c_chamber_temperature(make_bridge):
    _, p = make_bridge(model="X1C")
    feed(p, p1s_report(chamber_temper=38))
    s = p.normalize()
    assert s["chamber_temp"] == 38 and s["model"] == "X1 Carbon" and s["camera_type"] == "rtsp"


def test_h2d_dual_nozzle_chamber_and_ams_ht(make_bridge):
    _, p = make_bridge(model="H2D")
    report = p1s_report()
    for key in ("nozzle_temper", "nozzle_target_temper", "chamber_temper"):
        report.pop(key)
    report["device"] = {
        "extruder": {"info": [{"id": 0, "temp": (220 << 16) | 218}, {"id": 1, "temp": 35}]},
        "ctc": {"info": {"temp": (45 << 16) | 42}},
    }
    report["ams"]["ams"].append({"id": "128", "humidity_raw": "12", "temp": "55",
                                 "tray": [{"id": "0", "tray_type": "PA-CF", "tray_color": "333333FF", "remain": 60}]})
    report["ams"]["tray_now"] = "128"
    feed(p, report)
    s = p.normalize()
    assert s["nozzles"] == [{"id": 0, "temp": 218, "target": 220}, {"id": 1, "temp": 35, "target": None}]
    assert s["nozzle_temp"] == 218 and s["nozzle_target"] == 220
    assert s["chamber_temp"] == 42
    ht = s["ams"][1]
    assert ht["label"] == "AMS HT 1" and ht["trays"][0]["index"] == 128 and ht["trays"][0]["slot"] == 1
    assert ht["trays"][0]["active"] is True and s["active_filament"] == "PA-CF"


def test_single_nozzle_models_have_no_nozzle_list(make_bridge):
    _, p = make_bridge(model="H2S")
    report = p1s_report()
    report["device"] = {"extruder": {"info": [{"id": 0, "temp": (250 << 16) | 249}]}}
    feed(p, report)
    assert p.normalize()["nozzles"] == []


# --------------------------------------------------------------------------- Befehle
def _requests(p):
    return [json.loads(payload) for topic, payload, _ in p.client.published if topic == "device/SN1/request"]


def test_commands_are_sent_to_printer(make_bridge):
    _, p = make_bridge()
    p.handle_command("pause", "PRESS")
    p.handle_command("resume", "PRESS")
    p.handle_command("stop", "PRESS")
    p.handle_command("light", "ON")
    p.handle_command("speed", "Sport")
    p.handle_command("speed", "4")
    p.handle_command("refresh", "PRESS")
    sent = _requests(p)
    assert [m["print"]["command"] for m in sent[:3]] == ["pause", "resume", "stop"]
    assert sent[3]["system"]["command"] == "ledctrl" and sent[3]["system"]["led_mode"] == "on"
    assert sent[4]["print"] == {**sent[4]["print"], "command": "print_speed", "param": "3"}
    assert sent[5]["print"]["param"] == "4"
    assert sent[6]["pushing"]["command"] == "pushall"
    assert len({m[next(iter(m))]["sequence_id"] for m in sent}) == len(sent), "Sequenz-IDs eindeutig"


def test_invalid_speed_and_gcode_protection(make_bridge):
    _, p = make_bridge()
    p.handle_command("speed", "turbo")
    p.handle_command("gcode", "G28")
    assert _requests(p) == []
    _, p2 = make_bridge(allow_gcode=True)
    p2.handle_command("gcode", "G28")
    assert _requests(p2)[0]["print"] == {**_requests(p2)[0]["print"], "command": "gcode_line", "param": "G28\n"}


def test_commands_dropped_when_offline(make_bridge):
    _, p = make_bridge()
    p.connected = False
    p.handle_command("pause", "PRESS")
    assert _requests(p) == []


def test_ha_command_topic_routing(make_bridge):
    b, p = make_bridge(name="Werkstatt P1S")
    assert p.slug == "werkstatt_p1s"

    class M:
        topic = "bambu/werkstatt_p1s/cmd/light"
        payload = b"OFF"

    b._ha_message(None, None, M())
    assert _requests(p)[0]["system"]["led_mode"] == "off"


# --------------------------------------------------------------------------- Druckende, Vorschau, letzter Druck
def test_job_lifecycle_triggers_preview_and_last_print(make_bridge, sync_threads, monkeypatch):
    _, p = make_bridge()
    calls = []
    monkeypatch.setattr(p, "_fetch_preview", lambda info: calls.append(("preview", info["gcode_file"])))
    monkeypatch.setattr(p, "_capture_last_print", lambda result, info, *a: calls.append(("last", result, info["file"])))
    feed(p, p1s_report(gcode_state="IDLE"))
    feed(p, p1s_report(gcode_state="RUNNING"))
    feed(p, {"command": "push_status", "gcode_state": "FINISH"})
    feed(p, {"command": "push_status", "gcode_state": "PREPARE"})
    feed(p, {"command": "push_status", "gcode_state": "IDLE"})
    assert calls == [("preview", "Gridfinity_Bin_2x3.gcode.3mf"), ("last", "Fertig", "Gridfinity_Bin_2x3"),
                     ("preview", "Gridfinity_Bin_2x3.gcode.3mf"), ("last", "Abgebrochen", "Gridfinity_Bin_2x3")]


def test_capture_last_print_saves_and_publishes(make_bridge):
    b, p = make_bridge()
    p.jpeg, p.job_start = b"\xff\xd8testbild\xff\xd9", None
    p._capture_last_print("Unterbrochen", {"file": "Teil", "layers": 10, "filament": "PLA"}, True)
    img, retained = b.ha.last("bambu/test/last_print")
    meta = json.loads(b.ha.last("bambu/test/last_print_attr")[0])
    assert img == p.jpeg and retained is True
    assert meta["result"] == "Unterbrochen" and meta["bambu_role"] == "last_print"
    assert datetime.fromisoformat(meta["finished_at"]).tzinfo is not None
    # nach Neustart wieder geladen
    b2, p2 = make_bridge()
    assert p2.last_jpeg == p.jpeg and p2.last_meta["file"] == "Teil"


def _make_3mf(plates_with_gcode=(2,), plates=(1, 2)):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in plates:
            z.writestr(f"Metadata/plate_{n}.png", f"PNG{n}".encode())
            z.writestr(f"Metadata/plate_{n}_small.png", b"klein")
        for n in plates_with_gcode:
            z.writestr(f"Metadata/plate_{n}.gcode", "G28\n")
        z.writestr("Metadata/slice_info.config",
                   '<config><plate><metadata key="index" value="2"/><metadata key="prediction" value="5712"/>'
                   '<metadata key="weight" value="23.47"/><filament id="1" type="PLA" color="#FFFFFF" used_g="23.47"/>'
                   '</plate></config>')
    return buf.getvalue()


def test_extract_preview_picks_sliced_plate(tmp_path):
    f = tmp_path / "job.gcode.3mf"
    f.write_bytes(_make_3mf())
    png, meta = bridge.Printer._extract_preview(str(f))
    assert png == b"PNG2" and meta["plate"] == 2
    assert meta["prediction_min"] == 95 and meta["weight_g"] == 23.5
    assert meta["filaments"] == [{"type": "PLA", "color": "#FFFFFF", "used_g": 23.47}]


def test_extract_preview_without_png(tmp_path):
    f = tmp_path / "leer.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("3D/3dmodel.model", "x")
    assert bridge.Printer._extract_preview(str(f)) == (None, {})


def test_find_3mf_prefers_exact_match():
    class FakeFTP:
        listing = {"/": ["Anderes.gcode.3mf", "Notiz.txt"],
                   "/cache": ["/cache/Gridfinity_Bin_2x3.gcode.3mf", "/cache/Gridfinity_Bin_2x3_v2.gcode.3mf"]}

        def nlst(self, d):
            if d not in self.listing:
                raise bridge.ftplib.error_perm("550")
            return self.listing[d]

    info = {"file": "Gridfinity_Bin_2x3", "gcode_file": "Gridfinity_Bin_2x3.gcode.3mf"}
    assert bridge.Printer._find_3mf(FakeFTP(), info) == "/cache/Gridfinity_Bin_2x3.gcode.3mf"
    assert bridge.Printer._find_3mf(FakeFTP(), {"file": "Nichts", "gcode_file": ""}) is None


# --------------------------------------------------------------------------- Discovery
def _configs(b):
    return {t: json.loads(pl) for t, pl, _ in b.ha.published if t.endswith("/config")}


def test_discovery_p1s(make_bridge):
    b, p = make_bridge(model="P1S")
    b.publish_discovery(p)
    cfgs = _configs(b)
    keys = {t.split("/")[-2] for t in cfgs}
    for k in ("status", "progress", "remaining", "end_time", "nozzle_temp", "bed_temp", "chamber_light",
              "speed", "pause", "resume", "stop", "camera", "last_print", "preview", "printing", "error"):
        assert k in keys, k
    assert "chamber_temp" not in keys and "nozzle1_temp" not in keys
    assert all(c["default_entity_id"].split(".")[1].startswith("bambu_test_") for c in cfgs.values())
    assert all(c["device"]["model"] == "P1S" for c in cfgs.values())
    lp = next(c for t, c in cfgs.items() if "/last_print/" in t)
    assert lp["availability"] == [{"topic": "bambu/bridge/status"}], "bleibt bei ausgeschaltetem Drucker verfügbar"
    status = next(c for t, c in cfgs.items() if "/status/" in t)
    assert len(status["availability"]) == 2 and status["availability_mode"] == "all"


def test_discovery_h2d_has_extra_sensors(make_bridge):
    b, p = make_bridge(model="H2D")
    b.publish_discovery(p)
    keys = {t.split("/")[-2] for t in _configs(b)}
    assert {"chamber_temp", "nozzle1_temp", "nozzle2_temp"} <= keys


def test_discovery_legacy_object_id(make_bridge):
    b, p = make_bridge(ha_extra={"legacy_object_id": True})
    b.publish_discovery(p)
    cfgs = _configs(b).values()
    assert all("object_id" in c and "default_entity_id" not in c for c in cfgs)


def test_disabled_features_are_not_announced(make_bridge):
    b, p = make_bridge(camera=False, preview=False)
    b.publish_discovery(p)
    keys = {t.split("/")[-2] for t in _configs(b)}
    assert not {"camera", "last_print", "preview"} & keys


# --------------------------------------------------------------------------- HTTP / Ingress
def _serve(b, http_cfg):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_http_handler(b, http_cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_http_serves_dashboard_and_respects_allowlist(make_bridge, tmp_path):
    b, p = make_bridge()
    (tmp_path / "index.html").write_text("<!DOCTYPE html>dashboard")
    srv, url = _serve(b, {"www_dir": str(tmp_path), "allow": ["127.0.0.1"]})
    try:
        with urllib.request.urlopen(url + "/") as r:
            assert r.status == 200 and b"dashboard" in r.read()
        with urllib.request.urlopen(url + "/health") as r:
            assert json.loads(r.read())["printers"]["test"]["connected"] is True
        with urllib.request.urlopen(url + "/test/state.json") as r:
            assert json.loads(r.read())["serial"] == "SN1"
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(url + "/test/snapshot.jpg")
        assert err.value.code == 503
    finally:
        srv.shutdown()
    srv, url = _serve(b, {"www_dir": str(tmp_path), "allow": ["172.30.32.2"]})
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(url + "/")
        assert err.value.code == 403
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- Konfiguration
def test_config_validation(tmp_path):
    with pytest.raises(ValueError):
        bridge.Bridge({"data_dir": str(tmp_path), "printers": []})
    with pytest.raises(ValueError):
        bridge.Bridge({"data_dir": str(tmp_path), "printers": [{"name": "X", "host": "1.2.3.4"}]})
    with pytest.raises(ValueError):
        bridge.Bridge({"data_dir": str(tmp_path), "printers": [
            {"name": "A", "host": "1", "serial": "1", "access_code": "1"},
            {"name": "a", "host": "2", "serial": "2", "access_code": "2"}]})
