#!/bin/bash
# fix_webrtc.sh — Fix WebRTC registration + PJSIP realtime DB endpoints for DialFlow Pro
# Run this in a terminal: sudo bash fix_webrtc.sh
#
# What this fixes:
#   1. Installs PostgreSQL ODBC driver (odbc-postgresql)
#   2. Configures ODBC DSN (/etc/odbc.ini + /etc/odbcinst.ini)
#   3. Asterisk res_odbc.conf — connects Asterisk to dialflow_db via ODBC
#   4. Asterisk extconfig.conf — maps ps_endpoints/auths/aors to ODBC
#   5. Asterisk sorcery.conf — tells PJSIP to use realtime
#   6. Asterisk http.conf — enables HTTP:8088 + WSS:8089 on all interfaces
#   7. Asterisk pjsip transports — adds WS/WSS/UDP transports
#   8. Asterisk ari.conf + manager.conf — correct credentials
#   9. Updates AsteriskServer.server_ip in Django DB to 192.168.1.13
#  10. Reloads all Asterisk modules

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

SERVER_IP="192.168.1.13"
DB_NAME="dialflow_db"
DB_USER="shiwansh"
DB_PASS="Shiwansh@123"
DB_HOST="127.0.0.1"
DB_PORT="5432"
CERT_FILE="/etc/asterisk/keys/cert.pem"
KEY_FILE="/etc/asterisk/keys/key.pem"
ARI_USER="asterisk"
ARI_PASS="asterisk_ari_password"
AMI_USER="admin"
AMI_PASS="asterisk_ami_password"

GREEN="\e[32m"; YELLOW="\e[33m"; BOLD="\e[1m"; RESET="\e[0m"
ok()   { echo -e "${GREEN}  ✔  $1${RESET}"; }
warn() { echo -e "${YELLOW}  ⚠  $1${RESET}"; }
step() { echo -e "\n${BOLD}▶ $1${RESET}"; }

echo ""
echo "═══════════════════════════════════════════════════"
echo "   DialFlow Pro — WebRTC + PJSIP Realtime Fix"
echo "═══════════════════════════════════════════════════"

# ── 1. Install PostgreSQL ODBC driver ─────────────────────────────────────────
step "1/9  Installing PostgreSQL ODBC driver..."

apt-get install -y odbc-postgresql > /dev/null 2>&1 && ok "odbc-postgresql installed" || warn "apt install failed — trying apt-get update first"
if ! dpkg -l | grep -q "^ii.*odbc-postgresql"; then
    apt-get update -qq && apt-get install -y odbc-postgresql > /dev/null 2>&1
    ok "odbc-postgresql installed (after update)"
fi

# ── 2. Configure ODBC DSN ──────────────────────────────────────────────────────
step "2/9  Configuring ODBC DSN..."

# odbcinst.ini — driver registration
ODBC_DRIVER_PATH=$(find /usr -name "psqlodbcw.so" 2>/dev/null | head -1)
if [ -z "$ODBC_DRIVER_PATH" ]; then
    ODBC_DRIVER_PATH=$(find /usr -name "psqlodbca.so" 2>/dev/null | head -1)
fi
if [ -z "$ODBC_DRIVER_PATH" ]; then
    warn "Could not find psqlodbc .so file — check odbc-postgresql installation"
    ODBC_DRIVER_PATH="/usr/lib/x86_64-linux-gnu/odbc/psqlodbcw.so"
fi
ok "PostgreSQL ODBC driver: ${ODBC_DRIVER_PATH}"

tee /etc/odbcinst.ini > /dev/null << CONF
[PostgreSQL]
Description=PostgreSQL ODBC Driver
Driver=${ODBC_DRIVER_PATH}
FileUsage=1
Threading=2
CONF

# odbc.ini — DSN for Asterisk
tee /etc/odbc.ini > /dev/null << CONF
[dialflow-asterisk]
Driver=PostgreSQL
Description=DialFlow Pro Asterisk DB
Servername=${DB_HOST}
Port=${DB_PORT}
Database=${DB_NAME}
Username=${DB_USER}
Password=${DB_PASS}
Protocol=17.0
ReadOnly=No
RowVersioning=No
ShowSystemTables=No
ConnSettings=
CONF
ok "ODBC DSN 'dialflow-asterisk' configured"

# Test ODBC connection
if isql -v dialflow-asterisk "${DB_USER}" "${DB_PASS}" < /dev/null 2>/dev/null | grep -q "Connected"; then
    ok "ODBC connection test: PASSED"
else
    warn "ODBC connection test failed — check credentials in /etc/odbc.ini"
fi

# ── 3. Asterisk res_odbc.conf ─────────────────────────────────────────────────
step "3/9  Configuring Asterisk ODBC connection (res_odbc.conf)..."

tee /etc/asterisk/res_odbc.conf > /dev/null << CONF
;
; Asterisk ODBC connection to DialFlow PostgreSQL
; Managed by DialFlow Pro
;
[asterisk-dialflow]
enabled => yes
dsn => dialflow-asterisk
username => ${DB_USER}
password => ${DB_PASS}
pre-connect => yes
pooling => no
limit => 1
sanitysql => select 1
logging => no
CONF
ok "res_odbc.conf updated"

# ── 4. extconfig.conf — PJSIP realtime table mappings ─────────────────────────
step "4/9  Configuring extconfig.conf (PJSIP realtime mappings)..."

tee /etc/asterisk/extconfig.conf > /dev/null << 'CONF'
;
; Asterisk Realtime Table Mappings
; Managed by DialFlow Pro
;
[settings]
ps_endpoints        => odbc,asterisk-dialflow,ps_endpoints
ps_auths            => odbc,asterisk-dialflow,ps_auths
ps_aors             => odbc,asterisk-dialflow,ps_aors
ps_contacts         => odbc,asterisk-dialflow,ps_contacts
ps_endpoint_id_ips  => odbc,asterisk-dialflow,ps_endpoint_id_ips
CONF
ok "extconfig.conf updated (ps_endpoints, ps_auths, ps_aors, ps_contacts)"

# ── 5. sorcery.conf — tell PJSIP to use realtime ──────────────────────────────
step "5/9  Configuring sorcery.conf (PJSIP → realtime)..."

tee /etc/asterisk/sorcery.conf > /dev/null << 'CONF'
;
; Asterisk Sorcery Realtime Mappings
; Managed by DialFlow Pro
;
[res_pjsip]
endpoint=realtime,ps_endpoints
auth=realtime,ps_auths
aor=realtime,ps_aors
contact=realtime,ps_contacts

[res_pjsip_endpoint_identifier_ip]
identify=realtime,ps_endpoint_id_ips
CONF
ok "sorcery.conf updated"

# ── 6. http.conf — enable HTTP + WSS ─────────────────────────────────────────
step "6/9  Configuring Asterisk HTTP server (http.conf)..."

# Copy keys so Asterisk can read them (otherwise /home permissions block it)
mkdir -p /etc/asterisk/keys
cp "${SCRIPT_DIR}/keys/cert.pem" /etc/asterisk/keys/
cp "${SCRIPT_DIR}/keys/key.pem" /etc/asterisk/keys/
chown -R asterisk:asterisk /etc/asterisk/keys

tee /etc/asterisk/http.conf > /dev/null << CONF
;
; Asterisk HTTP / WebSocket Server
; Managed by DialFlow Pro
;
[general]
enabled=yes
bindaddr=0.0.0.0
bindport=8088
servername=DialFlow-Asterisk

; WSS — required for WebRTC from HTTPS pages
tlsenable=yes
tlsbindaddr=0.0.0.0:8089
tlscertfile=${CERT_FILE}
tlsprivatekey=${KEY_FILE}
CONF
ok "http.conf updated (WS :8088, WSS :8089, bound to 0.0.0.0)"

# ── 7. PJSIP transports ───────────────────────────────────────────────────────
step "7/9  Configuring PJSIP transports..."

PJSIP_LOCAL="/etc/asterisk/pjsip_dialflow.conf"

tee ${PJSIP_LOCAL} > /dev/null << CONF
;
; DialFlow Pro PJSIP Transports + Global
; Managed by DialFlow Pro — do not edit manually
;

[global]
type=global
user_agent=DialFlow-Asterisk
endpoint_identifier_order=username,ip,anonymous
max_initial_qualify_time=4

; ── UDP transport (standard SIP / carriers) ───────────────────────────────────
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
local_net=${SERVER_IP}/255.255.255.0
external_media_address=${SERVER_IP}
external_signaling_address=${SERVER_IP}

; ── TCP transport ─────────────────────────────────────────────────────────────
[transport-tcp]
type=transport
protocol=tcp
bind=0.0.0.0:5060

; ── WS transport (dev/fallback — plain WebSocket) ────────────────────────────
[transport-ws]
type=transport
protocol=ws
bind=0.0.0.0:8088

; ── WSS transport (production WebRTC from HTTPS pages) ───────────────────────
[transport-wss]
type=transport
protocol=wss
bind=0.0.0.0:8089
local_net=${SERVER_IP}/255.255.255.0
external_media_address=${SERVER_IP}
external_signaling_address=${SERVER_IP}
CONF

# Append include to main pjsip.conf if not already there
if ! grep -q "pjsip_dialflow.conf" /etc/asterisk/pjsip.conf; then
    echo "" >> /etc/asterisk/pjsip.conf
    echo "#include pjsip_dialflow.conf" >> /etc/asterisk/pjsip.conf
    ok "pjsip.conf: #include pjsip_dialflow.conf added"
else
    ok "pjsip.conf: already includes pjsip_dialflow.conf"
fi
ok "pjsip_dialflow.conf created (UDP, TCP, WS:8088, WSS:8089)"

# ── 8. ARI + Manager configs ──────────────────────────────────────────────────
step "8/9  Configuring ARI and AMI..."

tee /etc/asterisk/ari.conf > /dev/null << CONF
;
; Asterisk REST Interface (ARI)
; Managed by DialFlow Pro
;
[general]
enabled = yes
pretty = no
allowed_origins = *

[${ARI_USER}]
type = user
read_only = no
password = ${ARI_PASS}
CONF
ok "ari.conf updated (user: ${ARI_USER})"

tee /etc/asterisk/manager.conf > /dev/null << CONF
;
; Asterisk Manager Interface (AMI)
; Managed by DialFlow Pro
;
[general]
enabled = yes
port = 5038
bindaddr = 0.0.0.0

[${AMI_USER}]
secret = ${AMI_PASS}
read = all
write = all
permit = 0.0.0.0/0.0.0.0
CONF
ok "manager.conf updated (user: ${AMI_USER})"

# ── 9. Update AsteriskServer IP in Django DB ──────────────────────────────────
step "9/9  Updating AsteriskServer IP in Django DB to ${SERVER_IP}..."

cd "$SCRIPT_DIR"
source venv/bin/activate
python manage.py shell -c "
from telephony.models import AsteriskServer
updated = AsteriskServer.objects.filter(name='Primary Asterisk').update(
    server_ip='${SERVER_IP}',
    ari_host='127.0.0.1',   # ARI stays localhost (internal)
    ami_host='127.0.0.1',   # AMI stays localhost (internal)
)
print(f'Updated {updated} AsteriskServer(s) → server_ip=${SERVER_IP}')

# Also re-sync the phone extension so PJSIP endpoint gets correct transport
from telephony.models import Phone
for p in Phone.objects.filter(is_active=True):
    try:
        p.sync_to_asterisk()
        print(f'Re-synced phone extension {p.extension} to Asterisk DB')
    except Exception as e:
        print(f'Warning syncing {p.extension}: {e}')
" 2>&1 | grep -v "^INFO"
ok "Django DB updated"
deactivate

# ── Reload Asterisk ───────────────────────────────────────────────────────────
echo ""
echo "▶  Reloading Asterisk modules..."
asterisk -rx "module reload res_odbc.so" 2>/dev/null && ok "res_odbc reloaded" || warn "res_odbc reload issue"
sleep 1
asterisk -rx "module reload res_config_odbc.so" 2>/dev/null && ok "res_config_odbc reloaded" || warn "res_config_odbc reload issue"
sleep 1
asterisk -rx "module reload res_sorcery_realtime.so" 2>/dev/null && ok "res_sorcery_realtime reloaded" || warn "res_sorcery_realtime reload issue"
asterisk -rx "module reload res_pjsip.so" 2>/dev/null && ok "res_pjsip reloaded" || warn "res_pjsip reload — try: asterisk -rx 'core restart now'"
sleep 1
asterisk -rx "module reload http.so" 2>/dev/null && ok "http module reloaded" || true
asterisk -rx "module reload manager.so" 2>/dev/null && ok "manager module reloaded" || true

sleep 2

# ── Verify ────────────────────────────────────────────────────────────────────
echo ""
echo "▶  Verification..."
echo ""
echo "  PJSIP Transports:"
asterisk -rx "pjsip show transports" 2>/dev/null | grep -E "Transport|wss|ws|udp" | head -10 || warn "Could not query PJSIP transports"

echo ""
echo "  PJSIP Endpoints (from DB):"
asterisk -rx "pjsip show endpoints" 2>/dev/null | head -20 || warn "Could not query PJSIP endpoints"

echo ""
echo "  HTTP Status:"
asterisk -rx "http show status" 2>/dev/null | head -10 || warn "Could not query HTTP status"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════"
echo "   All fixes applied!"
echo "═══════════════════════════════════════════════════"
echo ""
echo "  Asterisk WebSocket endpoints:"
echo "    WS  plain : ws://${SERVER_IP}:8088/ws"
echo "    WSS secure: wss://${SERVER_IP}:8089/ws  ← JsSIP uses this (HTTPS app)"
echo "    ARI       : http://${SERVER_IP}:8088/ari/"
echo ""
echo "  PJSIP Realtime: Asterisk reads extensions from dialflow_db"
echo "    ps_endpoints  ← ps_auths ← ps_aors (already have ext 1000)"
echo ""
echo "  SIP Extension 1000:"
echo "    URI      : sip:1000@${SERVER_IP}"
echo "    WS URI   : sip:1000@${SERVER_IP};transport=wss"
echo "    Password : see 'python manage.py shell' → Phone.objects.first().secret"
echo ""
echo "  Next: restart the app"
echo "    Ctrl+C on start.sh → ./start.sh"
echo ""
echo "  Then open: https://${SERVER_IP}/agents/dashboard/"
echo "  WebRTC dot should turn GREEN within ~5 seconds."
echo ""
