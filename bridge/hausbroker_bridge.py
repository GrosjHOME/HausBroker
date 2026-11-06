#!/usr/bin/env python3
"""HausBroker-Bridge: holt Werte aus dem Internet (HTTPS) und stellt sie lokal per MQTT bereit.

Die Arduinos mit Ethernet-Shield koennen kein TLS. Die Bridge auf dem Raspberry Pi uebernimmt
deshalb alles, was HTTPS braucht, und die Arduinos sprechen nur noch mit dem lokalen Broker.

PoC: Asche-Prognose der Holzschnitzelheizung
  Quelle:  GET <HEIZUNG_URL> (Val Town, heizung.ts) -> JSON-Feld "prognose"
  Topic:   haus/heizung/asche  (retained)  Nutzlast "<prozent>;<tage_bis_voll>"
             prozent        0..999, -1 = unbekannt
             tage_bis_voll  0 = jetzt/heute voll, 999 = ueber 120 Tage, -1 = unbekannt
           z. B. "87;2", nach einer Leerung "0;999", ohne gueltige Prognose "-1;-1"
  Topic:   haus/heizung/asche/json  (retained)  die ganze Prognose fuer Menschen und spaetere Dienste
  Topic:   haus/bridge/online  (retained)  "1" = Bridge laeuft, "0" = weg (Last Will)

Die Nutzlast wird bei jedem Abruf neu publiziert (auch unveraendert), damit die Steuerung am
Eintreffen sieht, dass die Kette Val Town -> Bridge -> Broker lebt.

Konfiguration ueber Umgebungsvariablen (systemd: /etc/hausbroker/bridge.env):
  MQTT_HOST (localhost), MQTT_PORT (1883), MQTT_USER, MQTT_PASS
  HEIZUNG_URL           Web-Adresse von heizung.ts (Pflicht)
  INTERVALL_S (300)     Abrufintervall
  MAX_ALTER_MIN (120)   aeltere Prognosen gelten als unbekannt (der Waechter laeuft alle 30 min)
"""

import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

TOPIC_ASCHE = "haus/heizung/asche"
TOPIC_ASCHE_JSON = "haus/heizung/asche/json"
TOPIC_ONLINE = "haus/bridge/online"
UNBEKANNT = "-1;-1"

log = logging.getLogger("hausbroker")


def env(name, standard=None, pflicht=False):
    wert = os.environ.get(name, standard)
    if pflicht and not wert:
        sys.exit(f"Umgebungsvariable {name} fehlt")
    return wert


def asche_nutzlast(daten, jetzt, max_alter_min):
    """Baut aus der Antwort von heizung.ts die Nutzlast fuer die Steuerung ("prozent;tage")."""
    p = daten.get("prognose") if isinstance(daten, dict) else None
    if not isinstance(p, dict):
        return UNBEKANNT, "keine Prognose (ausserhalb der Heizsaison oder noch keine Leerung?)"
    try:
        erstellt = datetime.fromisoformat(str(p["erstellt"]).replace("Z", "+00:00"))
        alter_min = (jetzt - erstellt).total_seconds() / 60
    except (KeyError, ValueError):
        return UNBEKANNT, "Prognose ohne gueltige Zeit"
    if alter_min > max_alter_min:
        return UNBEKANNT, f"Prognose {alter_min:.0f} min alt (Waechter laeuft nicht?)"
    prozent, tage = p.get("prozent"), p.get("tageBisVoll")
    prozent = int(round(prozent)) if isinstance(prozent, (int, float)) else -1
    tage = int(tage) if isinstance(tage, (int, float)) else -1
    prozent = max(-1, min(999, prozent))
    tage = max(-1, min(999, tage))
    return f"{prozent};{tage}", f"{prozent} %, voll in {tage} Tagen ({p.get('quelle', '?')}, {alter_min:.0f} min alt)"


def hole_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "HausBroker-Bridge"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def neuer_client(client_id):
    # paho-mqtt 2.x verlangt die Callback-API-Version, 1.x kennt sie nicht
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        return mqtt.Client(client_id=client_id)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = env("MQTT_HOST", "localhost")
    port = int(env("MQTT_PORT", "1883"))
    url = env("HEIZUNG_URL", pflicht=True)
    intervall = int(env("INTERVALL_S", "300"))
    max_alter = int(env("MAX_ALTER_MIN", "120"))

    client = neuer_client("hausbroker-bridge")
    if env("MQTT_USER"):
        client.username_pw_set(env("MQTT_USER"), env("MQTT_PASS", ""))
    client.will_set(TOPIC_ONLINE, "0", qos=1, retain=True)

    def verbunden(c, _userdata, _flags, rc, _props=None):
        log.info("MQTT verbunden (%s)", rc)
        c.publish(TOPIC_ONLINE, "1", qos=1, retain=True)

    client.on_connect = verbunden
    client.reconnect_delay_set(min_delay=2, max_delay=60)
    client.connect_async(host, port, keepalive=60)
    client.loop_start()

    stopp = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopp.set())
    signal.signal(signal.SIGINT, lambda *_: stopp.set())

    while not stopp.is_set():
        try:
            daten = hole_json(url)
            nutzlast, text = asche_nutzlast(daten, datetime.now(timezone.utc), max_alter)
            client.publish(TOPIC_ASCHE_JSON, json.dumps(daten.get("prognose")), qos=1, retain=True)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            # Internet weg oder Val Town gestoert: Steuerung merkt das am Alter (kein neuer Wert)
            log.warning("Val Town nicht erreichbar: %s", e)
            nutzlast, text = None, None
        if nutzlast is not None:
            client.publish(TOPIC_ASCHE, nutzlast, qos=1, retain=True)
            log.info("Asche: %s -> %s", text, nutzlast)
        stopp.wait(intervall)

    if client.is_connected():
        try:
            client.publish(TOPIC_ONLINE, "0", qos=1, retain=True).wait_for_publish(5)
        except (RuntimeError, ValueError):
            pass   # sonst setzt der Broker "0" ueber den Last Will
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
