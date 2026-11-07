# HausBroker

Lokale Drehscheibe der Hausautomation auf einem **Raspberry Pi 3 Model B**:
ein MQTT-Broker (Mosquitto) im Heimnetz und eine Bridge, die alles übernimmt,
was HTTPS braucht. Die Arduino-Steuerungen mit Ethernet-Shield können kein TLS
und sprechen deshalb nur noch mit dem lokalen Broker.

```text
                 Internet (HTTPS)
   Val Town ─────────────┐        ThingSpeak  (später: Bridge schreibt dorthin)
                         │
                ┌────────▼─────────┐
                │ Raspberry Pi     │
                │  Bridge (Python) │
                │  Mosquitto :1883 │
                └────────┬─────────┘
                         │ MQTT (Heimnetz, ohne TLS)
        ┌────────────────┼────────────────┐
  Austragungssteuerung   Boiler, PV, …  (Migration Schritt für Schritt)
```

Fällt das Internet aus, läuft alles Lokale weiter; nur Werte aus der Cloud
werden alt. Jede Steuerung muss auch **ohne den Pi** sinnvoll weiterlaufen
(lokale Schutzfunktionen bleiben in der Steuerung).

## Stand: PoC Aschebox

Die Prognose der Aschebox (Wächter in Val Town, siehe Repo
`StefansHomeDashboard`) kommt per MQTT an die Austragungssteuerung, die sie
auf dem LCD und an der Störlampe anzeigt.

| Topic | Retained | Nutzlast | Wer |
|---|---|---|---|
| `haus/heizung/asche` | ja | `<prozent>;<tage_bis_voll>` z. B. `87;2` | Bridge → Austragungssteuerung |
| `haus/heizung/asche/json` | ja | ganze Prognose aus Val Town (JSON) | Bridge |
| `haus/bridge/online` | ja | `1` / `0` (Last Will) | Bridge |
| `haus/heizung/online` | ja | `1` / `0` (Last Will) | Austragungssteuerung |

`prozent` und `tage_bis_voll`: `-1` = unbekannt (keine Prognose, ausserhalb
der Heizsaison oder Prognose älter als 120 min), `tage_bis_voll` `0` = jetzt
bzw. heute voll, `999` = über 120 Tage (z. B. direkt nach einer Leerung).
Die Bridge publiziert alle 5 min neu, auch unverändert: Kommt bei der
Steuerung 45 min nichts, gilt der Wert als veraltet.

## Einrichtung

### 1. SD-Karte vorbereiten

Mit dem **Raspberry Pi Imager**: *Raspberry Pi OS Lite (64-bit)*, in den
Einstellungen (Zahnrad bzw. «Einstellungen bearbeiten»):

- Hostname `hausbroker`
- Benutzer `stefan` mit Passwort
- SSH aktivieren, am besten mit öffentlichem Schlüssel
- Zeitzone `Europe/Zurich`, Tastatur `ch`
- kein WLAN (der Pi hängt am Kabel)

Im Router eine **DHCP-Reservation** (feste IP) für den Pi anlegen. Diese IP
kommt in `Config.h` der Austragungssteuerung (`MQTT_BROKER_IP`).

### 2. Installieren

```bash
ssh stefan@hausbroker.local
sudo apt-get install -y git
git clone https://github.com/GrosjHOME/HausBroker.git
cd HausBroker
sudo ./setup.sh
```

`setup.sh` installiert Mosquitto und die Bridge, legt die MQTT-Benutzer an
(`bridge` mit Zufallspasswort, `heizung` und `stefan` fragt es nach) und
startet beide Dienste. Es darf mehrfach laufen.

Die Web-Adresse von `heizung.ts` steht schon in `/etc/hausbroker/bridge.env`
(`HEIZUNG_URL`, gleich wie `ASCHE.URL` im Dashboard).

### 3. Prüfen

```bash
systemctl status mosquitto hausbroker-bridge
journalctl -u hausbroker-bridge -f      # alle 5 min: "Asche: 87 %, voll in 2 Tagen ..."
mosquitto_sub -h localhost -u stefan -P <passwort> -t 'haus/#' -v
```

Vom PC aus geht dasselbe mit `mosquitto_sub -h <ip-des-pi> …` oder einem
MQTT-Programm wie MQTT Explorer.

Im Browser geht es über WebSocket: z. B. mit MQTTX Web (<https://mqttx.app/web-client>)
eine Verbindung anlegen mit Host `ws://hausbroker.local` (oder `ws://192.168.0.50`),
Port `9001`, Pfad `/mqtt`, Benutzer und Passwort wie oben. Hinweis: Eine Seite, die
über HTTPS geladen ist, darf kein unverschlüsseltes `ws://` öffnen – dann die
http-Variante des Web-Clients nehmen oder MQTTX als Desktop-App.

Updates: `cd HausBroker && git pull && sudo ./setup.sh`.

## SD-Karte

Für den PoC läuft der Pi von der SD-Karte. Damit sie lange hält:

- das Systemjournal liegt nur im RAM (`setup.sh`, nach einem Neustart weg)
- Mosquitto schreibt die retained Werte nur alle 30 min auf die Karte
- eine Markenkarte (A1/A2, «High Endurance») verwenden

Später, wenn alles läuft, eine der beiden Varianten:

- **Read-only-Overlay:** `sudo raspi-config` → *Performance Options* →
  *Overlay File System*. Für Änderungen wieder ausschalten.
- **USB-SSD:** Der Pi 3 **Model B** (nicht B+) bootet erst von USB, wenn
  einmalig das OTP-Bit gesetzt ist: `program_usb_boot_mode=1` in
  `/boot/firmware/config.txt`, neu starten, prüfen mit
  `vcgencmd otp_dump | grep 17:` (→ `17:3020000a`), Zeile wieder entfernen.
  Das ist endgültig, der Pi bootet danach aber weiterhin auch von SD.

## Nächste Schritte

1. Weitere Steuerungen publizieren ihre Messwerte zusätzlich per MQTT
   (`haus/pv/…`, `haus/boiler/…`).
2. Steuerungen, die heute über ThingSpeak Werte anderer Steuerungen holen
   (Boiler: PV-Leistung), abonnieren sie lokal, ThingSpeak als Rückfall.
3. Die Bridge schreibt an ThingSpeak, die Arduinos nicht mehr. Damit gibt es
   je Kanal nur noch einen Schreiber (kein Zeitlimit-Konflikt), und die Bridge
   kann bei Internet-Ausfall puffern und nachliefern.
