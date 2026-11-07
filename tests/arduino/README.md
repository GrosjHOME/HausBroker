# Tests mit Arduino (UNO R4 Minima, UNO R3)

Testsketche fuer einen **Arduino UNO R4 Minima** oder **UNO R3** mit **Ethernet Shield**, um die Verbindung
zum HausBroker (Raspberry Pi, Mosquitto, Port 1883) zu pruefen.

## ethernet_test

- holt per DHCP eine IP und gibt alle 2 s Zustand und Werte seriell (USB, 115200 Baud) an den PC aus,
  z. B. `t=12s n=6 link=ON ip=192.168.0.35 broker=ok haus/heizung/asche=28;6 (vor 3 s) A0=512`
- meldet sich mit Benutzer und Passwort am Broker an (`BROKER_IP` im Sketch, aktuell 192.168.0.50:1883),
  abonniert `MQTT_TOPIC` (Standard `haus/heizung/asche`) und gibt jeden empfangenen Wert sofort aus:
  `MQTT >> haus/heizung/asche = 28;6`. Der Broker schickt beim Abonnieren zuerst den zuletzt gespeicherten
  Wert (retained). Mit dem Benutzer `stefan` kann man auch `haus/#` abonnieren und sieht alle Topics.
- `broker=` zeigt den Zustand: `ok`, `NICHT erreichbar`, `Login abgelehnt` (Code 4 = falsches Passwort,
  5 = Topic nicht erlaubt), `KEIN LOGIN (MQTT_USER leer)`; bei Fehlern wird alle 10 s neu versucht
- antwortet im Browser unter `http://<IP des Arduino>/` mit denselben Werten
- Test-LED extern an **Pin 7** (220-330 Ohm Vorwiderstand nach GND): langsam (1 Hz) = IP und Link ok,
  schnell (5 Hz) = keine IP oder kein Link

### Zugangsdaten

`ethernet_test/secrets.example.h` nach `secrets.h` *kopieren* (die Vorlage bleibt liegen) und Benutzer und Passwort des Brokers eintragen.
Ohne `secrets.h` kompiliert der Sketch nicht. Beim Start gibt er `Build:` (Zeit des Kompilierens) und `Login:` (Benutzer, Laenge des Passworts) aus, daran sieht man, welche Version laeuft. `secrets.h` steht in `.gitignore` und kommt nie ins Repo. Der Benutzer `stefan` darf `haus/#` lesen,
`heizung` nur `haus/heizung/asche` (siehe `mosquitto/hausbroker.acl`).

### Hardware-Hinweise

Die eingebaute LED an Pin 13 eignet sich nicht, weil Pin 13 auch SCK ist und das Shield am selben SPI-Bus
haengt. Die LEDs auf dem Shield sind fest verdrahtet. Belegt sind ausserdem die Pins 4, 10, 11 und 12.
Beim aelteren Shield Rev3 (W5100) kann der Chip den Link nicht melden (`link=unbekannt`).
Auf dem Uno R3 (2 KB RAM) belegt der Sketch 37 % des RAM fest; die Texte stehen deshalb mit `F()` im Flash.

### Flashen

```bash
arduino-cli compile --fqbn arduino:renesas_uno:minima --upload -p COMx tests/arduino/ethernet_test
arduino-cli compile --fqbn arduino:avr:uno            --upload -p COMx tests/arduino/ethernet_test
```

Benoetigt die Bibliothek *Ethernet* (getestet mit 2.0.2) und den passenden Core (*Arduino UNO R4 Boards*
bzw. *Arduino AVR Boards*). MQTT ist von Hand implementiert (MQTT 3.1.1, QoS 0), es ist keine MQTT-Bibliothek
noetig.
