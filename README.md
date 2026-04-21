# ha-core-changes – Bluetooth API Integration

Diese Dateien gehören in den Fork von `home-assistant/core` und implementieren
die serverseitige Bluetooth-Unterstützung für die HA Android App.

## Fork-Workflow

1. Gehe zu https://github.com/home-assistant/core und erstelle einen Fork
2. Erstelle einen Branch `bluetooth` in deinem Fork
3. Kopiere den Inhalt dieses Ordners in die entsprechenden Pfade deines Forks:
   - `ha-core-changes/homeassistant/` → `homeassistant/`
   - `ha-core-changes/tests/` → `tests/`
4. Committe und pushe
5. Öffne einen PR gegen `home-assistant/core:dev`

## Integration: `bluetooth_api`

**Pfad:** `homeassistant/components/bluetooth_api/`

### Architektur

```
Android App ←─ RFCOMM/BLE ─→ bluetooth_api ←─ ws://127.0.0.1:8123/api/websocket ─→ HA Core
```

Die Integration öffnet einen RFCOMM-Socket (Classic Bluetooth) und/oder einen BLE
GATT-Service und leitet alle Nachrichten transparent zur lokalen HA WebSocket API
weiter. Die Android App authentifiziert sich mit ihrem Long-Lived Access Token – genau
wie über eine normale Netzwerkverbindung.

### Konfiguration in HA

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → "Bluetooth API"**
2. RFCOMM aktivieren (Standard) und/oder BLE aktivieren
3. Das Android-Gerät mit dem HA-Host per Bluetooth koppeln

### Abhängigkeiten

- **RFCOMM:** Kein zusätzliches Python-Paket nötig (`socket.AF_BLUETOOTH` ist in Python 3 eingebaut)
- **BLE GATT:** Erfordert `bless` (`pip install bless`) – optional

### Tests ausführen

```bash
pytest tests/components/bluetooth_api/ -v
```
