# Bambu Lab Bridge

Verbindet Bambu-Lab-Drucker **lokal** mit Home Assistant – ohne Cloud und ohne HACS.
Unterstützt: P1S, P1P, A1, A1 mini, X1, X1 Carbon, X1E, P2S, H2S, H2D, H2D Pro, H2C.
Die ausführliche Anleitung mit allen Details steht in der README des Repositorys.

## Voraussetzungen

- **Mosquitto broker** (offizielles Add-on) installiert und gestartet, **MQTT**-Integration eingerichtet
- Am Drucker: **LAN-Modus** an. Für die Steuerung (Licht, Pause, Geschwindigkeit, Stopp) zusätzlich den **Developer Mode** aktivieren.
- Feste IP für den Drucker (DHCP-Reservierung im Router)
- Nur X1/H2/P2: am Drucker zusätzlich „LAN Mode Liveview“ aktivieren (Kamera über RTSPS)

## Einrichtung

1. Im Tab **Konfiguration** beim Drucker eintragen:
   - `model`: Druckermodell (z. B. `P1S`, `A1MINI`, `X1C`, `H2D`; unbekannt = `OTHER`)
   - `host`: IP des Druckers
   - `serial`: Seriennummer (Display → Einstellungen → Gerät)
   - `access_code`: Access Code (Display → WLAN/LAN)
2. **Speichern** → **Starten**. Im Tab **Protokoll** sollte stehen:
   ```
   Mit Home-Assistant-Broker verbunden
   [P1S] Mit Drucker verbunden
   [P1S] Kamera verbunden
   ```
3. Unter *Einstellungen → Geräte & Dienste → MQTT* erscheint das Gerät **„Bambu P1S“**.

## Dashboard

- **Seitenleiste:** Der Menüpunkt **„3D-Drucker“** öffnet das komplette Dashboard (dafür „In Seitenleiste anzeigen“ im Tab *Info* aktivieren).
- **Eigene Karte:** Dashboard bearbeiten → *Karte hinzufügen* → **„MexMox Bambu Drucker“**, oder:
  ```yaml
  type: custom:bambu-printer-card
  entity: sensor.bambu_p1s_status
  full_width: true          # optional
  ```
  Die Karte wird bei jedem Add-on-Start automatisch aktualisiert. Wurde der Ordner `www` beim ersten Start neu angelegt, Home Assistant **einmal neu starten**.

## Optionen pro Drucker

| Option | Standard | Bedeutung |
|---|---|---|
| `camera` | `true` | Kamerabild an HA weitergeben |
| `camera_mode` | `auto` | `auto` (nach Modell), `chamber` (P1/A1, Port 6000), `rtsp` (X1/H2/P2, Port 322) |
| `preview` | `true` | 3D-Vorschau (Slicer-Bild) beim Druckstart per FTPS laden |
| `last_print_light` | `false` | Für das Foto „Letzter Druck“ kurz das Licht einschalten |
| `last_print_delay` | `5` | Sekunden nach Druckende bis zum Foto |
| `camera_interval` | `2` | Sekunden zwischen Kamerabildern |
| `pushall_interval` | `300` | Vollständiger Status-Abgleich in Sekunden |
| `allow_gcode` | `false` | Rohe G-Code-Befehle erlauben (Vorsicht!) |

Mehrere Drucker: im Formular weitere Einträge in der Liste `printers` anlegen (jeweils mit eigenem `name`).

## Umstieg vom LXC-Container

Die alte Bridge im LXC **vorher stoppen** (`systemctl disable --now bambu-bridge`), sonst laufen zwei Bridges parallel. Entitäten, Automationen und Karten bleiben erhalten, weil die Entitäts-IDs gleich sind.

## Fehlersuche

| Meldung | Lösung |
|---|---|
| `Drucker hat Verbindung abgelehnt` | Access Code prüfen (ändert sich beim Aus-/Einschalten des LAN-Modus) |
| Werte kommen an, Befehle wirken nicht | Developer Mode am Drucker aktivieren |
| `Kein MQTT-Dienst gefunden` | Mosquitto-Add-on installieren und starten |
| Karte „Custom element doesn't exist“ | HA einmal neu starten, Browser neu laden (Strg+F5) |
