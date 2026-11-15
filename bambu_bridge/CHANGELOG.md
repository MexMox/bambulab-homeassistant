# Changelog

## 1.5.2
- Legt die App den Ordner `/config/www` neu an, erscheint in Home Assistant eine Benachrichtigung „bitte neu starten“ (sonst fehlt die Karte)
- Doku: Begriff „Apps“ statt „Add-ons“ (HA 2026.2), Hinweis auf App-Store statt Marketplace, Neustart-Hinweis als eigener Warnkasten

## 1.5.1
- Installation repariert: neues offizielles Basis-Image `ghcr.io/home-assistant/base` (aktuelles Alpine), `build.yaml` entfernt (von Home Assistant abgelöst)

## 1.5.0
- **Alle Bambu-Lab-Serien:** P1 (P1S, P1P), A1 (A1, A1 mini), X1 (X1, X1C, X1E), P2S, H2 (H2S, H2D, H2D Pro, H2C)
- Kamera der X1/H2/P2-Serie per RTSPS (Port 322, ffmpeg ist im Add-on enthalten)
- Kamera-Typ wird automatisch aus dem Modell abgeleitet, bei unbekanntem Modell per Port-Test erkannt
- Bauraum-Temperatur (X1/H2), zwei Düsen (H2D, H2D Pro, H2C), AMS HT
- Neue Optionen `model` und `camera_mode`
- Fehler behoben: Absturz des Vorschau-Abrufs, wenn der Drucker per FTP nicht erreichbar ist
- Testsuite: 59 Unit-Tests und ein Integrationstest mit 42 Prüfungen
- Ausführliche README mit Screenshots, MIT-Lizenz

## 1.4.0
- Erste Version als Home-Assistant-Add-on
- Einrichtung über das Formular, MQTT-Zugang automatisch vom Mosquitto-Add-on
- Dashboard-Karte wird automatisch kopiert und als Ressource eingetragen
- Dashboard als Menüpunkt „3D-Drucker“ in der Seitenleiste
- 3D-Vorschau (Slicer-Bild) und „Letzter Druck“ bei ausgeschaltetem Drucker
