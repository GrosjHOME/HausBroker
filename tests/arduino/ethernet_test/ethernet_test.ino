// Testsketch: Arduino UNO R4 Minima (oder UNO R3) + Ethernet Shield
//
// - holt per DHCP eine IP-Adresse und gibt Zustand und Werte alle 2 s seriell
//   (USB, 115200 Baud) an den PC aus
// - verbindet sich mit dem HausBroker (MQTT, Port 1883, mit Benutzer und Passwort),
//   abonniert MQTT_TOPIC und gibt jeden empfangenen Wert sofort seriell aus
//   (der Broker schickt beim Abonnieren zuerst den zuletzt gespeicherten Wert, "retained")
// - antwortet im Browser unter http://<IP des Arduino>/ mit denselben Werten
// - Test-LED (extern, Pin 7 -> Vorwiderstand 220-330 Ohm -> LED -> GND):
//     langsam blinken (1 Hz)  = DHCP ok und Link da
//     schnell blinken (5 Hz)  = keine IP oder kein Link
//
// Zugangsdaten: secrets.example.h nach secrets.h kopieren und Benutzer/Passwort eintragen
// (secrets.h ist in .gitignore und kommt nie ins Repo). Der Benutzer "stefan" darf haus/#
// lesen, der Benutzer "heizung" nur haus/heizung/asche (siehe mosquitto/hausbroker.acl).
//
// Warum nicht die eingebaute LED an Pin 13? Pin 13 ist auch SCK. Das Shield haengt
// ueber den ICSP-Anschluss am selben SPI-Bus, Blinken auf Pin 13 wuerde die
// Kommunikation mit dem Ethernet-Chip stoeren. Die LEDs auf dem Shield (PWR, LINK,
// 100M, RX, TX, COL) sind fest verdrahtet und nicht per Programm schaltbar.
//
// Belegte Pins des Shields: 10 (CS Ethernet), 4 (CS SD), 11/12/13 (SPI) - nicht verwenden.
//
// MQTT ist hier von Hand auf dem Minimum implementiert (CONNECT, SUBSCRIBE, PUBLISH
// empfangen, PINGREQ, MQTT 3.1.1, QoS 0), damit keine Zusatzbibliothek und wenig RAM
// noetig ist. Texte stehen mit F() im Flash, weil der Uno R3 nur 2 KB RAM hat.

#include <SPI.h>
#include <Ethernet.h>

#include "secrets.h"  // fehlt die Datei: secrets.example.h nach secrets.h kopieren

const uint8_t LED_PIN = 7;

// Selbst vergebene MAC (Bit 1 im ersten Byte = lokal verwaltet). Im Router ggf. reservieren.
byte mac[] = {0x02, 0x4B, 0x53, 0x32, 0x00, 0x01};

// HausBroker (Raspberry Pi mit Mosquitto)
const IPAddress BROKER_IP(192, 168, 0, 50);
const uint16_t BROKER_PORT = 1883;
const char MQTT_TOPIC[] = "haus/heizung/asche";  // mit Benutzer "stefan" geht auch "haus/#"
const char MQTT_ID[] = "r4eth-test";

EthernetServer server(80);

bool dhcpOk = false;
uint32_t zaehler = 0;
uint32_t tDhcp = 0, tAusgabe = 0;

// ---------------------------------------------------------------- MQTT
EthernetClient mq;
enum : uint8_t { MQ_UNGEPRUEFT, MQ_KEIN_LOGIN, MQ_DATEN_ZU_LANG, MQ_TCP_FEHLER, MQ_ANTWORT_FEHLT, MQ_LOGIN_FALSCH, MQ_OK, MQ_GETRENNT };
uint8_t mqStatus = MQ_UNGEPRUEFT;
uint8_t mqRc = 0;  // Rueckgabecode des Brokers (4 = falsches Passwort, 5 = nicht erlaubt)
char mqTopic[40] = "";
char mqWert[64] = "";
bool mqHatWert = false;
uint32_t tMqVersuch = 0, tMqSend = 0, tMqEmpfang = 0;

const __FlashStringHelper* mqText() {
  switch (mqStatus) {
    case MQ_OK:            return F("ok");
    case MQ_KEIN_LOGIN:    return F("KEIN LOGIN (MQTT_USER leer)");
    case MQ_DATEN_ZU_LANG: return F("Benutzer/Passwort zu lang");
    case MQ_TCP_FEHLER:    return F("NICHT erreichbar");
    case MQ_ANTWORT_FEHLT: return F("keine MQTT-Antwort");
    case MQ_LOGIN_FALSCH:  return F("Login abgelehnt");
    case MQ_GETRENNT:      return F("getrennt");
    default:               return F("?");
  }
}

void mqString(const char* s) {
  uint16_t n = strlen(s);
  mq.write((uint8_t)(n >> 8));
  mq.write((uint8_t)(n & 0xFF));
  mq.write((const uint8_t*)s, n);
}

// Ein Byte lesen, hoechstens 1 s warten. false bei Zeitueberschreitung oder Verbindungsende.
bool mqByte(uint8_t& b) {
  uint32_t ende = millis() + 1000;
  while (!mq.available()) {
    if (!mq.connected() || millis() > ende) return false;
  }
  b = mq.read();
  return true;
}

bool mqVerbinden() {
  tMqVersuch = millis();
  mq.stop();
  if (MQTT_USER[0] == 0) { mqStatus = MQ_KEIN_LOGIN; return false; }
  uint16_t laenge = 10 + 2 + strlen(MQTT_ID) + 2 + strlen(MQTT_USER) + 2 + strlen(MQTT_PASS);
  if (laenge > 127) { mqStatus = MQ_DATEN_ZU_LANG; return false; }

  mq.setConnectionTimeout(2000);
  if (!mq.connect(BROKER_IP, BROKER_PORT)) { mqStatus = MQ_TCP_FEHLER; return false; }

  // CONNECT: Protokoll "MQTT" Version 4, Flags Benutzer+Passwort+CleanSession, KeepAlive 60 s
  static const uint8_t kopf[] = {0, 4, 'M', 'Q', 'T', 'T', 4, 0xC2, 0, 60};
  mq.write((uint8_t)0x10);
  mq.write((uint8_t)laenge);
  mq.write(kopf, sizeof kopf);
  mqString(MQTT_ID);
  mqString(MQTT_USER);
  mqString(MQTT_PASS);

  // CONNACK: 0x20 0x02 <Flags> <Code>
  uint8_t a[4];
  uint8_t n = 0;
  uint32_t ende = millis() + 3000;
  while (n < 4 && millis() < ende) {
    if (mq.available()) a[n++] = mq.read();
  }
  if (n < 4 || a[0] != 0x20) { mqStatus = MQ_ANTWORT_FEHLT; mq.stop(); return false; }
  mqRc = a[3];
  if (mqRc != 0) { mqStatus = MQ_LOGIN_FALSCH; mq.stop(); return false; }

  // SUBSCRIBE mit Paket-Nr. 1, QoS 0
  uint8_t t = strlen(MQTT_TOPIC);
  mq.write((uint8_t)0x82);
  mq.write((uint8_t)(2 + 2 + t + 1));
  mq.write((uint8_t)0);
  mq.write((uint8_t)1);
  mqString(MQTT_TOPIC);
  mq.write((uint8_t)0);

  mqStatus = MQ_OK;
  tMqSend = millis();
  return true;
}

// Ein ankommendes Paket lesen (die Laenge ist 1 bis 2 Bytes) und bei PUBLISH Topic und Wert merken.
void mqPaketLesen() {
  uint8_t kopf, b;
  if (!mqByte(kopf)) return;
  uint16_t rest = 0;
  for (uint8_t i = 0, shift = 0; i < 2; i++, shift += 7) {
    if (!mqByte(b)) return;
    rest |= (uint16_t)(b & 0x7F) << shift;
    if (!(b & 0x80)) break;
  }
  uint8_t buf[100];
  uint16_t gespeichert = 0;
  for (uint16_t i = 0; i < rest; i++) {
    if (!mqByte(b)) return;
    if (i < sizeof buf) buf[gespeichert++] = b;  // zu lange Pakete werden gekuerzt
  }
  if ((kopf >> 4) != 3 || gespeichert < 2) return;  // nur PUBLISH interessiert

  uint8_t qos = (kopf >> 1) & 3;
  uint16_t tl = ((uint16_t)buf[0] << 8) | buf[1];
  uint16_t p = 2 + tl + (qos ? 2 : 0);
  if (p > gespeichert) return;
  uint16_t tn = tl < sizeof mqTopic - 1 ? tl : sizeof mqTopic - 1;
  memcpy(mqTopic, buf + 2, tn);
  mqTopic[tn] = 0;
  uint16_t wn = gespeichert - p < sizeof mqWert - 1 ? gespeichert - p : sizeof mqWert - 1;
  memcpy(mqWert, buf + p, wn);
  mqWert[wn] = 0;
  mqHatWert = true;
  tMqEmpfang = millis();

  Serial.print(F("MQTT >> "));
  Serial.print(mqTopic);
  Serial.print(F(" = "));
  Serial.println(mqWert);
}

void mqttPflege() {
  if (mqStatus != MQ_OK || !mq.connected()) {
    if (mqStatus == MQ_OK) { mqStatus = MQ_GETRENNT; Serial.println(F("MQTT: Verbindung weg")); }
    if (mqStatus == MQ_UNGEPRUEFT || millis() - tMqVersuch > 10000) {
      bool ok = mqVerbinden();
      Serial.print(F("MQTT: "));
      if (ok) {
        Serial.print(F("verbunden, abonniert: "));
        Serial.println(MQTT_TOPIC);
      } else {
        Serial.print(mqText());
        if (mqStatus == MQ_LOGIN_FALSCH) { Serial.print(F(" (Code ")); Serial.print(mqRc); Serial.print(')'); }
        Serial.println();
      }
    }
    return;
  }
  while (mq.available()) mqPaketLesen();
  if (millis() - tMqSend > 30000) {  // KeepAlive: PINGREQ
    mq.write((uint8_t)0xC0);
    mq.write((uint8_t)0x00);
    tMqSend = millis();
  }
}

// ---------------------------------------------------------------- Ethernet
const __FlashStringHelper* hardwareText() {
  switch (Ethernet.hardwareStatus()) {
    case EthernetNoHardware: return F("KEINE (Shield nicht erkannt)");
    case EthernetW5100:      return F("W5100");
    case EthernetW5200:      return F("W5200");
    case EthernetW5500:      return F("W5500");
    default:                 return F("?");
  }
}

// Der W5100 (Ethernet Shield Rev3) kann den Link nicht melden -> "unbekannt".
const __FlashStringHelper* linkText() {
  switch (Ethernet.linkStatus()) {
    case LinkON:  return F("ON");
    case LinkOFF: return F("OFF");
    default:      return F("unbekannt");
  }
}

bool linkOk() { return Ethernet.linkStatus() != LinkOFF; }

// Schreibt die Werte auf ein beliebiges Ziel (Serial oder Browser-Client).
void werte(Print& p) {
  p.print(F("t="));       p.print(millis() / 1000);
  p.print(F("s n="));     p.print(zaehler);
  p.print(F(" link="));   p.print(linkText());
  p.print(F(" ip="));     p.print(Ethernet.localIP());
  p.print(F(" broker=")); p.print(mqText());
  if (mqHatWert) {
    p.print(F(" ")); p.print(mqTopic);
    p.print(F("="));  p.print(mqWert);
    p.print(F(" (vor ")); p.print((millis() - tMqEmpfang) / 1000); p.print(F(" s)"));
  }
  p.print(F(" A0="));     p.print(analogRead(A0));
}

void dhcpVersuch() {
  tDhcp = millis();
  Serial.print(F("DHCP ... "));
  // Zeitlimit 8 s, Antwort-Timeout 2 s (Standard waere 60 s und blockiert lange)
  dhcpOk = Ethernet.begin(mac, 8000, 2000) == 1;
  if (dhcpOk) {
    server.begin();
    Serial.print(F("ok, IP "));      Serial.print(Ethernet.localIP());
    Serial.print(F(" Gateway "));    Serial.print(Ethernet.gatewayIP());
    Serial.print(F(" Netzmaske ")); Serial.println(Ethernet.subnetMask());
  } else {
    Serial.println(F("FEHLER (kein DHCP-Server erreichbar? Kabel? Shield?)"));
  }
}

void browserBedienen() {
  EthernetClient c = server.available();
  if (!c) return;
  // Anfrage bis zur Leerzeile lesen (max. 1 s), Inhalt interessiert nicht
  int zeilenLaenge = 0;
  uint32_t ende = millis() + 1000;
  while (c.connected() && millis() < ende) {
    if (!c.available()) continue;
    char z = c.read();
    if (z == '\n') { if (zeilenLaenge == 0) break; zeilenLaenge = 0; }
    else if (z != '\r') zeilenLaenge++;
  }
  c.println(F("HTTP/1.1 200 OK"));
  c.println(F("Content-Type: text/plain; charset=utf-8"));
  c.println(F("Connection: close"));
  c.println();
  werte(c);
  c.println();
  delay(1);
  c.stop();
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  Serial.begin(115200);
  // R4: USB-Seriell; kurz auf den PC warten, aber nicht ewig (laeuft auch ohne PC)
  for (uint32_t t = millis(); !Serial && millis() - t < 3000;) {}

  Serial.println();
  Serial.println(F("=== Arduino + Ethernet Shield - Test ==="));
  Ethernet.init(10);  // CS-Pin des Shields
  // Einmal ohne IP starten, damit hardwareStatus() gueltig ist
  Ethernet.begin(mac, IPAddress(0, 0, 0, 0));
  Serial.print(F("Ethernet-Chip: ")); Serial.println(hardwareText());
  Serial.print(F("MAC: "));
  for (uint8_t i = 0; i < 6; i++) {
    if (mac[i] < 0x10) Serial.print('0');
    Serial.print(mac[i], HEX);
    if (i < 5) Serial.print(':');
  }
  Serial.println();
  Serial.print(F("Build: ")); Serial.println(F(__DATE__ " " __TIME__));  // zeigt, welche Version wirklich laeuft
  Serial.print(F("Broker: ")); Serial.print(BROKER_IP); Serial.print(':'); Serial.println(BROKER_PORT);
  Serial.print(F("Login: Benutzer \"")); Serial.print(MQTT_USER);
  Serial.print(F("\", Passwort ")); Serial.print(strlen(MQTT_PASS)); Serial.println(F(" Zeichen"));
  dhcpVersuch();
}

void loop() {
  uint32_t jetzt = millis();

  if (dhcpOk) {
    int r = Ethernet.maintain();  // Lease erneuern; 1 und 3 = fehlgeschlagen
    if (r == 1 || r == 3) { dhcpOk = false; Serial.println(F("DHCP-Lease verloren")); }
  } else if (jetzt - tDhcp > 10000) {
    dhcpVersuch();
  }

  if (dhcpOk) {
    mqttPflege();
    browserBedienen();
  }

  if (jetzt - tAusgabe >= 2000) {
    tAusgabe = jetzt;
    zaehler++;
    werte(Serial);
    Serial.println();
  }

  // LED: 1 Hz wenn alles ok, sonst 5 Hz
  uint32_t halbePeriode = (dhcpOk && linkOk()) ? 500 : 100;
  digitalWrite(LED_PIN, (millis() / halbePeriode) & 1);
}
