# HausBroker

Lokale Drehscheibe der Hausautomation auf einem **Raspberry Pi 3 Model B**:
ein MQTT-Broker (Mosquitto) im Heimnetz und eine Bridge, die alles übernimmt,
was HTTPS braucht. Die Arduino-Steuerungen mit Ethernet-Shield können kein TLS
und sprechen deshalb nur noch mit dem lokalen Broker.

```text
                 Internet (HTTPS)
   Val Town ◄───────────►┐        ThingSpeak  (später: Bridge schreibt dorthin)
                         │
                ┌────────▼─────────┐
                │ Raspberry Pi     │
                │  Bridge (Python) │
                │  Mosquitto :1883 │
                └────────┬─────────┘
                         │ MQTT (Heimnetz, ohne TLS)
        ┌────────────────┼────────────────┐
  Austragungssteuerung, Boiler, PV, Pumpwerk  (Wallbox: offen)
```

Die Bridge arbeitet in beide Richtungen: Sie holt Werte aus Val Town und stellt sie lokal bereit
(Asche-Prognose), und sie schickt Ereignisse der Steuerungen nach Val Town (Pumpwerk).

Fällt das Internet aus, läuft alles Lokale weiter; nur Werte aus der Cloud
werden alt. Jede Steuerung muss auch **ohne den Pi** sinnvoll weiterlaufen
(lokale Schutzfunktionen bleiben in der Steuerung).

## Stand

**Asche-Prognose (PoC):** Die Prognose der Aschebox (Wächter in Val Town, siehe Repo
`StefansHomeDashboard`) kommt per MQTT an die Austragungssteuerung, die sie
auf dem LCD und an der Störlampe anzeigt.

**Messwerte:** PV-Anlage, Boiler und Pumpwerk veröffentlichen ihre Werte zusätzlich zu ThingSpeak per MQTT (Tabelle unten);
der Boiler nimmt die PV-Leistung zuerst vom Broker. Die Ereignisse des Pumpwerks (exakte Pausen) gibt die Bridge an Val Town weiter
(Abschnitt «Pumpwerk-Ereignisse nach Val Town»).

| Topic | Retained | Nutzlast | Wer |
|---|---|---|---|
| `haus/heizung/asche` | ja | `<prozent>;<tage_bis_voll>` z. B. `87;2` | Bridge → Austragungssteuerung |
| `haus/heizung/asche/json` | ja | ganze Prognose aus Val Town (JSON) | Bridge |
| `haus/bridge/online` | ja | `1` / `0` (Last Will) | Bridge |
| `haus/bridge/ereignisse_offen` | ja | Anzahl Pumpwerk-Ereignisse, die noch nicht an Val Town gesendet sind (`0` = alles gesendet) | Bridge |
| `haus/heizung/online` | ja | `1` / `0` (Last Will) | Austragungssteuerung |
| `haus/pv/leistung` | **nein** | aktuelle PV-Leistung in W, z. B. `2400` (`0` nachts), alle 2 s | PV-Anlage → Boilersteuerung |
| `haus/pv/energie/tag` `…/monat` `…/jahr` `…/gestern` `…/letzter_monat` | ja | Energie in kWh, eine Nachkommastelle, einmal pro Minute | PV-Anlage |
| `haus/pv/online` | ja | `1` / `0` (Last Will) | PV-Anlage |
| `haus/boiler/online` | ja | `1` / `0` (Last Will) | Boilersteuerung |
| `haus/boiler/temp_mitte` `…/temp_unten` | ja | Boilertemperatur in °C, eine Nachkommastelle, einmal pro Minute (bei Sensorfehler nicht gesendet) | Boilersteuerung |
| `haus/boiler/ladeleistung` | ja | aktuelle Ladestufe in W (`0` / `1000` / `1800` / `4000`) | Boilersteuerung |
| `haus/boiler/einschaltleistung` | ja | minimale PV-Leistung zum Einschalten in W | Boilersteuerung |
| `haus/boiler/pv_verwendet` | ja | PV-Wert in W, mit dem die Steuerung rechnet | Boilersteuerung |
| `haus/boiler/pv_quelle` | ja | `mqtt` oder `thingspeak` | Boilersteuerung |
| `haus/boiler/voll` | ja | `1` = Boiler voll geladen, sonst `0` | Boilersteuerung |
| `haus/boiler/sensorfehler` | ja | `1` = Temperatursensor defekt, sonst `0` | Boilersteuerung |
| `haus/pumpwerk/betrieb` `…/stoerung` | ja | `1` = Pumpe läuft / Störung aktiv, sonst `0`; wird bei jedem Wechsel sofort gesendet | Pumpwerk-Logger |
| `haus/pumpwerk/einschaltungen_heute` `…/einschaltungen_gesamt` | ja | Anzahl Einschaltungen (ThingSpeak field2/field4) | Pumpwerk-Logger |
| `haus/pumpwerk/laufzeit_heute_s` `…/laufzeit_gesamt_s` `…/laufzeit_gesamt_h` | ja | Laufzeit in s bzw. Dezimalstunden mit 3 Stellen (field3/field5) | Pumpwerk-Logger |
| `haus/pumpwerk/stoerzeit_heute_s` `…/stoerzeit_gesamt_s` | ja | Störzeit in s (field7/field8) | Pumpwerk-Logger |
| `haus/pumpwerk/status` | ja | Klartext `Pumpe ein` / `laeuft` / `aus` / `wartet` (ThingSpeak-Status) | Pumpwerk-Logger |
| `haus/pumpwerk/pause_letzte_s` `…/pause_min_heute_s` `…/pause_min_gesamt_s` | ja | Pause zwischen zwei Einschaltungen in s, auf die Millisekunde gemessen: letzte, kürzeste heute, kürzeste seit Beginn (leer = noch keine gemessen) | Pumpwerk-Logger |
| `haus/pumpwerk/ereignis` | **nein** | JSON je Ein-/Ausschalten: `{"typ":"start","t":<UTC-Epoche>,"pause_s":123.456}` bzw. `{"typ":"stopp","t":…,"lauf_s":12.345}` | Pumpwerk-Logger |
| `haus/pumpwerk/online` | ja | `1` / `0` (Last Will) | Pumpwerk-Logger |

`prozent` und `tage_bis_voll`: `-1` = unbekannt (keine Prognose, ausserhalb
der Heizsaison oder Prognose älter als 120 min), `tage_bis_voll` `0` = jetzt
bzw. heute voll, `999` = über 120 Tage (z. B. direkt nach einer Leerung).
Die Bridge publiziert alle 5 min neu, auch unverändert: Kommt bei der
Steuerung 45 min nichts, gilt der Wert als veraltet.

### PV-Leistung für den Boiler

Die PV-Anlage veröffentlicht die Werte von ThingSpeak (Felder 1–6): die Leistung
alle 2 s, die Energiewerte einmal pro Minute. `haus/pv/leistung` ist bewusst **nicht retained**: Jede
Nachricht, die ankommt, ist frisch. Eine retained Nachricht hätte kein Alter,
der Boiler könnte einen gestrigen Wert nicht erkennen.

Die Boilersteuerung nimmt die PV-Leistung **zuerst vom Broker**. Sie gilt, wenn
die letzte Nachricht höchstens 30 s alt ist und `haus/pv/online` nicht `0`
ist. Sonst (Broker weg, PV-Steuerung weg, Netz gestört) liest sie den Wert
wie bisher von **ThingSpeak** (Kanal 172430, Feld 1). Beide Wege sind
unabhängig: Fällt der Pi aus, ändert sich für den Boiler nichts ausser der
Quelle; sie steht im ThingSpeak-Status des Boiler-Kanals.

### Pumpwerk-Ereignisse nach Val Town

Der Pumpwerk-Logger misst jede Pause zwischen zwei Einschaltungen auf die Millisekunde und
meldet jedes Ein-/Ausschalten als JSON an `haus/pumpwerk/ereignis` (nicht retained). Die Bridge
abonniert das Topic, legt jedes Ereignis in eine Warteschlange und schickt sie gebündelt
(höchstens 200 je Aufruf, alle paar Sekunden) per HTTPS-POST an `pumpwerk.ts` im Val
**GrosjeansHomeDashboard** (Val Town). Dort steht jedes Ereignis als Zeile in SQLite, das
Dashboard liest sie von da. ThingSpeak läuft unabhängig davon unverändert weiter.

- **Ausfallsicher:** Die Warteschlange liegt auf der SD-Karte (`/var/lib/hausbroker/pumpwerk_ereignisse.jsonl`)
  und übersteht Neustart und Internetausfall. Bei Fehlern wird mit wachsender Pause (bis 5 min)
  wiederholt. Dasselbe Ereignis speichert Val Town nur einmal. Wie viele Ereignisse noch
  warten, steht in `haus/bridge/ereignisse_offen` (0 = alles gesendet).
- **Grenze:** Ereignisse, die kommen, während die Bridge nicht läuft (z. B. beim Neustart des Pi),
  gehen verloren. Der Logger sendet sie ohne Zustellgarantie. Die Pausen-Topics
  `haus/pumpwerk/pause_*` bleiben davon unberührt.
- **Einrichten (einmalig, die Passwörter trägst du selbst ein):**
  1. Ein langes Zufallspasswort erzeugen, z. B. `openssl rand -hex 24`.
  2. Im Val **GrosjeansHomeDashboard** die Umgebungsvariable `PUMPWERK_TOKEN` mit diesem Wert setzen.
  3. Auf dem Pi in `/etc/hausbroker/bridge.env` bei `PUMPWERK_TOKEN=` denselben Wert eintragen
     (`PUMPWERK_URL` steht schon da; `sudo ./setup.sh` trägt beide Zeilen in eine bestehende
     Datei nach). Danach `sudo systemctl restart hausbroker-bridge`.
  4. Prüfen: `journalctl -u hausbroker-bridge -f` zeigt bei jedem Ein-/Ausschalten
     «Pumpwerk: Ereignis …» und kurz danach «… an Val Town gesendet». Meldet das Log HTTP 503,
     fehlt `PUMPWERK_TOKEN` im Val, bei HTTP 401 stimmen die Passwörter nicht überein.
- **Ohne Token ist die Weitergabe aus** (die Bridge macht dann nur, was sie vorher auch tat).

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
(`bridge` mit Zufallspasswort, `heizung`, `pv`, `boiler`, `pumpwerk` und `stefan` fragt es
nach; bereits vorhandene Benutzer bleiben unverändert) und startet beide
Dienste. Es darf mehrfach laufen. Jedes Passwort kommt in die `Secrets.h` der
jeweiligen Steuerung.

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

Im Browser: **<http://hausbroker.local:8080/>** (oder `http://192.168.0.50:8080/`)
öffnen, Benutzer und Passwort eingeben. Die Seite durchsucht den MQTT-Baum unter
`haus/#` und zeigt pro Gruppe (`heizung`, `pv`, `boiler`, `bridge`, …) eine Karte
mit allen Werten und ihrem Alter; neue Topics erscheinen von selbst. Die Seite liefert
der Dienst `hausbroker-web` (Python, Port 8080); sie verbindet sich per WebSocket
(`ws://…:9001/mqtt`) mit dem Broker.
Fremde Web-Clients über HTTPS (z. B. MQTTX Web) funktionieren nicht, weil sie kein
unverschlüsseltes `ws://` öffnen dürfen.

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
   (PV-Anlage, Boiler, Pumpwerk: erledigt; Wallbox: offen).
2. Steuerungen, die heute über ThingSpeak Werte anderer Steuerungen holen,
   abonnieren sie lokal, ThingSpeak als Rückfall (Boiler: PV-Leistung,
   erledigt; Wallbox: offen).
3. Die Bridge schreibt an ThingSpeak, die Arduinos nicht mehr. Damit gibt es
   je Kanal nur noch einen Schreiber (kein Zeitlimit-Konflikt), und die Bridge
   kann bei Internet-Ausfall puffern und nachliefern.
