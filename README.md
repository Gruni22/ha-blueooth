# Bluetooth API Integration

## Integration: `bluetooth_api`

**Pfad:** `custom_components/bluetooth_api/`

### Architektur

Die Integration öffnet einen RFCOMM-Socket (Classic Bluetooth) und/oder einen BLE
GATT-Service und leitet alle Nachrichten transparent zur lokalen HA WebSocket API
weiter. Die Android App authentifiziert sich mit ihrem Long-Lived Access Token – genau
wie über eine normale Netzwerkverbindung.

### Konfiguration in HA

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → "Bluetooth API"**
2. RFCOMM aktivieren (Standard) und/oder BLE aktivieren
3. Das Android-Gerät mit dem HA-Host per Bluetooth koppeln

### Abhängigkeiten

TO BE DEFINED

### Tests ausführen

```bash
pytest tests/bluetooth_api/ -v
```
