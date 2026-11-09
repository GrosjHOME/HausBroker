#!/usr/bin/env bash
# Einrichtung HausBroker auf dem Raspberry Pi (Raspberry Pi OS Lite 64-bit).
# Aufruf im geklonten Repo:  sudo ./setup.sh
# Kann mehrfach laufen: vorhandene Passwoerter und bridge.env bleiben erhalten.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo starten: sudo ./setup.sh"; exit 1; }
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "== Pakete"
apt-get update -q
apt-get install -y -q mosquitto mosquitto-clients python3-paho-mqtt

echo "== SD-Karte schonen: Systemjournal nur im RAM"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/hausbroker.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=30M
EOF
systemctl restart systemd-journald

echo "== Mosquitto"
install -m 644 "$REPO/mosquitto/hausbroker.conf" /etc/mosquitto/conf.d/hausbroker.conf
install -m 640 -o root -g mosquitto "$REPO/mosquitto/hausbroker.acl" /etc/mosquitto/hausbroker.acl

PASSWD=/etc/mosquitto/passwd
[ -f "$PASSWD" ] || { touch "$PASSWD"; }
chown root:mosquitto "$PASSWD"; chmod 640 "$PASSWD"

benutzer_anlegen() {   # $1 = Benutzer, $2 = Passwort (leer = nachfragen)
  if grep -q "^$1:" "$PASSWD"; then echo "   Benutzer $1 gibt es schon"; return; fi
  local pw="$2"
  if [ -z "$pw" ]; then read -r -s -p "   Passwort fuer MQTT-Benutzer $1: " pw; echo; fi
  mosquitto_passwd -b "$PASSWD" "$1" "$pw"
  echo "   Benutzer $1 angelegt"
}

mkdir -p /etc/hausbroker
ENVF=/etc/hausbroker/bridge.env
if [ ! -f "$ENVF" ]; then
  BRIDGE_PW="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
  sed "s/^MQTT_PASS=.*/MQTT_PASS=$BRIDGE_PW/" "$REPO/bridge/bridge.env.example" > "$ENVF"
  benutzer_anlegen bridge "$BRIDGE_PW"
else
  benutzer_anlegen bridge "$(sed -n 's/^MQTT_PASS=//p' "$ENVF")"
fi
benutzer_anlegen heizung ""   # dieses Passwort kommt in Secrets.h der Austragungssteuerung
benutzer_anlegen pv ""        # dieses Passwort kommt in Secrets.h der PV-Anlage
benutzer_anlegen boiler ""    # dieses Passwort kommt in Secrets.h der Boilersteuerung
benutzer_anlegen pumpwerk ""  # dieses Passwort kommt in Secrets.h des Pumpwerk-Loggers
benutzer_anlegen stefan ""    # zum Mitlesen
chmod 640 "$PASSWD"

systemctl enable mosquitto
systemctl restart mosquitto

echo "== Bridge"
id hausbroker >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hausbroker
install -d /opt/hausbroker/bridge
install -m 755 "$REPO/bridge/hausbroker_bridge.py" /opt/hausbroker/bridge/
chown root:hausbroker "$ENVF"; chmod 640 "$ENVF"
install -m 644 "$REPO/systemd/hausbroker-bridge.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable hausbroker-bridge
systemctl restart hausbroker-bridge

echo "== Weboberflaeche (Port 8080)"
install -d -m 755 /opt/hausbroker/web
install -m 644 "$REPO/web/index.html" /opt/hausbroker/web/index.html
install -m 644 "$REPO/systemd/hausbroker-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable hausbroker-web
systemctl restart hausbroker-web

echo
echo "Fertig. Pruefen:"
echo "  systemctl status mosquitto hausbroker-bridge hausbroker-web"
echo "  Browser: http://hausbroker.local:8080/"
echo "  journalctl -u hausbroker-bridge -f"
echo "  mosquitto_sub -h localhost -u stefan -P <passwort> -t 'haus/#' -v"
