"""Gemeinsame Test-Hilfen: lädt bridge.py und addon_launcher.py direkt aus dem Add-on-Ordner."""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "bambu_bridge"
sys.path.insert(0, str(ADDON))


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ADDON / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load("bridge", "bridge.py")
launcher = _load("addon_launcher", "addon_launcher.py")


class _Result:
    def wait_for_publish(self, timeout=None):
        return True


class FakeMQTT:
    """Ersetzt paho-Clients: merkt sich alle Veröffentlichungen."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        return _Result()

    def topics(self):
        return [t for t, _, _ in self.published]

    def last(self, topic):
        for t, payload, retain in reversed(self.published):
            if t == topic:
                return payload, retain
        return None, None


class FakeMsg:
    def __init__(self, data):
        self.payload = json.dumps(data).encode()
        self.topic = "device/SN1/report"


@pytest.fixture
def make_bridge(tmp_path):
    """Erzeugt eine Bridge mit einem Drucker, ohne echte Netzwerkverbindungen."""

    def _make(ha_extra=None, **printer):
        cfg = {
            "data_dir": str(tmp_path / "data"),
            "homeassistant": {"host": "127.0.0.1", **(ha_extra or {})},
            "printers": [{"name": "Test", "host": "127.0.0.1", "serial": "SN1",
                          "access_code": "12345678", **printer}],
        }
        b = bridge.Bridge(cfg)
        b.ha = FakeMQTT()
        b.ha_connected = True
        p = b.printers[0]
        p.client = FakeMQTT()
        p.connected = True
        p._fetch_preview = lambda info: None   # keine echten FTP-Verbindungen in Unit-Tests
        return b, p

    return _make


@pytest.fixture
def sync_threads(monkeypatch):
    """Führt Hintergrund-Threads der Bridge sofort und synchron aus."""

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(bridge.threading, "Thread", ImmediateThread)
