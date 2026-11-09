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

Pumpwerk-Ereignisse: Der Pumpwerk-Logger meldet jedes Ein-/Ausschalten als JSON an
  haus/pumpwerk/ereignis (nicht retained), z. B. {"typ":"start","t":1731000000,"pause_s":123.456}.
  Die Bridge sammelt sie in einer Warteschlange (auf Platte, ueberlebt Neustart und Internetausfall) und
  schickt sie gebuendelt per POST an pumpwerk.ts (Val Town); dort landet jedes Ereignis in SQLite, das
  Dashboard liest sie. Doppelte Ereignisse (Wiederholung nach einem Fehler) speichert Val Town nur einmal.
  Ohne PUMPWERK_URL/PUMPWERK_TOKEN ist das abgeschaltet. Ereignisse, die kommen, waehrend die Bridge nicht
  laeuft, gehen verloren (der Logger sendet sie ohne Zustellgarantie).
  Topic:   haus/bridge/ereignisse_offen  (retained)  Anzahl noch nicht an Val Town gesendeter Ereignisse

Die Nutzlast wird bei jedem Abruf neu publiziert (auch unveraendert), damit die Steuerung am
Eintreffen sieht, dass die Kette Val Town -> Bridge -> Broker lebt.

Konfiguration ueber Umgebungsvariablen (systemd: /etc/hausbroker/bridge.env):
  MQTT_HOST (localhost), MQTT_PORT (1883), MQTT_USER, MQTT_PASS
  HEIZUNG_URL           Web-Adresse von heizung.ts (Pflicht)
  INTERVALL_S (300)     Abrufintervall
  MAX_ALTER_MIN (120)   aeltere Prognosen gelten als unbekannt (der Waechter laeuft alle 30 min)
  PUMPWERK_URL          Web-Adresse von pumpwerk.ts (Val Town); zusammen mit PUMPWERK_TOKEN schaltet sie die
                        Weitergabe der Pumpwerk-Ereignisse ein
  PUMPWERK_TOKEN        Passwort, dasselbe wie die Umgebungsvariable PUMPWERK_TOKEN im Val
  EREIGNIS_DATEI        Warteschlange auf Platte (/var/lib/hausbroker/pumpwerk_ereignisse.jsonl)
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
TOPIC_PUMPWERK_EREIGNIS = "haus/pumpwerk/ereignis"
TOPIC_EREIGNISSE_OFFEN = "haus/bridge/ereignisse_offen"
UNBEKANNT = "-1;-1"

MIN_EPOCHE = 1_700_000_000      # kleinere Zeitstempel sind Unsinn (Logger ohne Uhr) -> Empfangszeit einsetzen
MAX_PRO_POST = 200              # wie MAX_PRO_POST in pumpwerk.ts
MAX_WARTESCHLANGE = 5000        # aelteste Ereignisse fallen weg, wenn Val Town sehr lange nicht erreichbar ist
SENDE_TAKT_S = 5                # so oft wird nachgesehen, ob Ereignisse zu senden sind
MAX_WARTEZEIT_S = 300           # laengste Pause zwischen Wiederholungen nach einem Fehler

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


def ereignis_aus_nachricht(nutzlast, jetzt_s):
    """Wandelt die MQTT-Nutzlast eines Pumpwerk-Ereignisses in das Format von pumpwerk.ts um.

    Eingabe:  {"typ":"start","t":1731000000,"pause_s":123.456} bzw. {"typ":"stopp","t":...,"lauf_s":12.345}
    Ausgabe:  {"typ":"start","t":1731000000,"t_geschaetzt":False,"dauer_s":123.456} oder None bei Unsinn.
    Fehlt "t" (Logger ohne NTP-Zeit), gilt der Empfangszeitpunkt, markiert mit t_geschaetzt.
    """
    try:
        d = json.loads(nutzlast)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or d.get("typ") not in ("start", "stopp"):
        return None
    t = d.get("t")
    geschaetzt = False
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t < MIN_EPOCHE:
        t, geschaetzt = int(jetzt_s), True
    e = {"typ": d["typ"], "t": int(t), "t_geschaetzt": geschaetzt}
    dauer = d.get("pause_s" if d["typ"] == "start" else "lauf_s")
    if not isinstance(dauer, bool) and isinstance(dauer, (int, float)) and dauer >= 0:
        e["dauer_s"] = float(dauer)
    return e


class Warteschlange:
    """Noch nicht an Val Town gesendete Ereignisse, thread-sicher und auf Platte (eine JSON-Zeile je Ereignis).

    Die Datei wird bei jeder Aenderung komplett neu geschrieben (atomar); es sind nur wenige Ereignisse
    pro Tag. Ist die Datei nicht schreibbar, laeuft die Warteschlange nur im Speicher weiter."""

    def __init__(self, pfad=None, maximum=MAX_WARTESCHLANGE):
        self._lock = threading.Lock()
        self._pfad = pfad
        self._max = maximum
        self._eintraege = []
        self._laden()

    def _laden(self):
        if not self._pfad or not os.path.exists(self._pfad):
            return
        try:
            with open(self._pfad, encoding="utf-8") as f:
                for zeile in f:
                    zeile = zeile.strip()
                    if zeile:
                        try:
                            self._eintraege.append(json.loads(zeile))
                        except ValueError:
                            log.warning("Warteschlange: Zeile uebersprungen (kein JSON)")
            del self._eintraege[:-self._max]
            if self._eintraege:
                log.info("Warteschlange: %d Ereignisse von der letzten Sitzung geladen", len(self._eintraege))
        except OSError as e:
            log.warning("Warteschlange nicht lesbar (%s) - starte leer", e)

    def _speichern(self):
        if not self._pfad:
            return
        tmp = self._pfad + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for e in self._eintraege:
                    f.write(json.dumps(e) + "\n")
            os.replace(tmp, self._pfad)
        except OSError as e:
            log.warning("Warteschlange nicht gespeichert (%s) - nur im Speicher", e)

    def hinzu(self, ereignis):
        with self._lock:
            self._eintraege.append(ereignis)
            del self._eintraege[:-self._max]
            self._speichern()

    def vorne(self, n):
        with self._lock:
            return list(self._eintraege[:n])

    def entferne(self, n):
        with self._lock:
            del self._eintraege[:n]
            self._speichern()

    def __len__(self):
        with self._lock:
            return len(self._eintraege)


def sende_ereignisse(url, token, ereignisse, timeout=20):
    """POST an pumpwerk.ts; gibt die Antwort ({neu, doppelt, ungueltig}) zurueck."""
    daten = json.dumps({"ereignisse": ereignisse}).encode("utf-8")
    req = urllib.request.Request(url, data=daten, method="POST", headers={
        "Content-Type": "application/json",
        "X-Pumpwerk-Token": token,
        "User-Agent": "HausBroker-Bridge",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sender_schleife(warteschlange, url, token, stopp, melde_offen=None, takt_s=SENDE_TAKT_S):
    """Schickt die Warteschlange in Paketen an Val Town. Bei Fehlern (Internet weg, Token falsch,
    Val Town gestoert) bleiben die Ereignisse liegen und es wird mit wachsender Pause wiederholt."""
    pause = takt_s
    zuletzt_offen = None
    while not stopp.is_set():
        paket = warteschlange.vorne(MAX_PRO_POST)
        if paket:
            try:
                antwort = sende_ereignisse(url, token, paket)
                warteschlange.entferne(len(paket))
                log.info("Pumpwerk: %d Ereignisse an Val Town gesendet (%s)", len(paket), antwort)
                pause = takt_s
                if len(warteschlange) > 0:
                    continue          # weitere Pakete sofort
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    # Val Town versteht das Paket nicht: nicht ewig wiederholen, sonst blockiert es alle folgenden
                    log.error("Pumpwerk: Val Town lehnt das Paket ab (HTTP 400) - %d Ereignisse verworfen", len(paket))
                    warteschlange.entferne(len(paket))
                else:
                    log.warning("Pumpwerk: Val Town antwortet HTTP %s (Token? PUMPWERK_TOKEN im Val gesetzt?)", e.code)
                    pause = min(max(pause * 2, takt_s), MAX_WARTEZEIT_S)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
                log.warning("Pumpwerk: Val Town nicht erreichbar: %s (%d Ereignisse warten)", e, len(warteschlange))
                pause = min(max(pause * 2, takt_s), MAX_WARTEZEIT_S)
        offen = len(warteschlange)
        if melde_offen and offen != zuletzt_offen:
            melde_offen(offen)
            zuletzt_offen = offen
        stopp.wait(pause)


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

    # Pumpwerk-Ereignisse an Val Town weitergeben (nur mit PUMPWERK_URL und PUMPWERK_TOKEN)
    pumpwerk_url, pumpwerk_token = env("PUMPWERK_URL"), env("PUMPWERK_TOKEN")
    warteschlange = None
    if pumpwerk_url and pumpwerk_token:
        warteschlange = Warteschlange(env("EREIGNIS_DATEI", "/var/lib/hausbroker/pumpwerk_ereignisse.jsonl"))
    elif pumpwerk_url or pumpwerk_token:
        log.warning("Pumpwerk-Ereignisse AUS: PUMPWERK_URL und PUMPWERK_TOKEN muessen beide gesetzt sein")

    def verbunden(c, _userdata, _flags, rc, _props=None):
        log.info("MQTT verbunden (%s)", rc)
        c.publish(TOPIC_ONLINE, "1", qos=1, retain=True)
        if warteschlange is not None:
            c.subscribe(TOPIC_PUMPWERK_EREIGNIS, qos=1)   # nach jedem Verbinden neu (clean session)

    def nachricht(_c, _userdata, msg):
        if warteschlange is None or msg.topic != TOPIC_PUMPWERK_EREIGNIS:
            return
        e = ereignis_aus_nachricht(msg.payload, time.time())
        if e is None:
            log.warning("Pumpwerk: Nachricht ignoriert (kein gueltiges Ereignis): %r", msg.payload[:100])
            return
        warteschlange.hinzu(e)
        log.info("Pumpwerk: Ereignis %s%s (Warteschlange: %d)", e["typ"],
                 f" {e['dauer_s']:.3f} s" if "dauer_s" in e else "", len(warteschlange))

    client.on_connect = verbunden
    client.on_message = nachricht
    client.reconnect_delay_set(min_delay=2, max_delay=60)
    client.connect_async(host, port, keepalive=60)
    client.loop_start()

    stopp = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopp.set())
    signal.signal(signal.SIGINT, lambda *_: stopp.set())

    if warteschlange is not None:
        def melde_offen(n):
            client.publish(TOPIC_EREIGNISSE_OFFEN, str(n), qos=1, retain=True)
        threading.Thread(target=sender_schleife, name="pumpwerk-sender", daemon=True,
                         args=(warteschlange, pumpwerk_url, pumpwerk_token, stopp, melde_offen)).start()
        log.info("Pumpwerk-Ereignisse werden an Val Town weitergegeben")

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
