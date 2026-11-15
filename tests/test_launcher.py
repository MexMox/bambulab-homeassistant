"""Unit-Tests des Add-on-Starters: Formularwerte -> Bridge-Konfiguration, Karte installieren."""
import os

import pytest

from conftest import launcher

PRINTER = {"name": "P1S", "model": "P1S", "host": "192.0.2.10", "serial": "01P00A000000001", "access_code": "12345678"}


def test_build_config_uses_supervisor_mqtt(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "1883")
    monkeypatch.setenv("MQTT_USERNAME", "addons")
    monkeypatch.setenv("MQTT_PASSWORD", "geheim")
    cfg = launcher.build_config({"log_level": "debug", "printers": [PRINTER]})
    assert cfg["log_level"] == "DEBUG"
    assert cfg["homeassistant"]["host"] == "core-mosquitto" and cfg["homeassistant"]["username"] == "addons"
    assert cfg["printers"][0]["serial"] == PRINTER["serial"]
    assert cfg["http"]["allow"] == ["172.30.32.2", "127.0.0.1"] and cfg["http"]["port"] == 8099


def test_manual_mqtt_overrides_supervisor(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    cfg = launcher.build_config({"printers": [PRINTER], "mqtt_host": "192.0.2.5", "mqtt_port": 1884,
                                 "mqtt_username": "ich", "mqtt_password": "pw"})
    assert (cfg["homeassistant"]["host"], cfg["homeassistant"]["port"]) == ("192.0.2.5", 1884)


def test_incomplete_printers_are_skipped():
    cfg = launcher.build_config({"printers": [PRINTER, {"name": "Leer", "host": "", "serial": "", "access_code": ""}]})
    assert [p["name"] for p in cfg["printers"]] == ["P1S"]
    assert all(v not in ("", None) for v in cfg["printers"][0].values())


def test_no_valid_printer_stops_addon():
    with pytest.raises(SystemExit):
        launcher.build_config({"printers": [{"name": "Leer", "host": "", "serial": "", "access_code": ""}]})


def test_install_card_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "HA_CONFIG", str(tmp_path))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    launcher.install_card()
    target = tmp_path / "www" / "bambu" / launcher.CARD_NAME
    assert target.is_file() and target.stat().st_size > 1000
    src = os.path.join(launcher.APP_DIR, "www", launcher.CARD_NAME)
    assert target.read_bytes() == open(src, "rb").read()
