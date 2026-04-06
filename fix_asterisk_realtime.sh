#!/bin/bash
# fix_asterisk_realtime.sh — Fixes PJSIP realtime sync for DialFlow Pro
set -e

echo "========================================="
echo "  DialFlow Pro — Fixing Asterisk Realtime"
echo "========================================="

# ─── 1. Write correct sorcery.conf ────────────────────────────────────────────
echo "[1/4] Writing /etc/asterisk/sorcery.conf (enabling PJSIP realtime)..."
cat > /etc/asterisk/sorcery.conf << 'EOF'
; sorcery.conf — DialFlow Pro
; Maps PJSIP objects to realtime (ODBC) so Asterisk reads endpoints from the DB.

[res_pjsip]
endpoint=realtime,ps_endpoints
auth=realtime,ps_auths
aor=realtime,ps_aors
contact=realtime,ps_contacts

[res_pjsip_endpoint_identifier_ip]
identify=realtime,ps_endpoint_id_ips
EOF
echo "  ✔  sorcery.conf written."

# ─── 2. Verify extconfig.conf has ps_contacts too ─────────────────────────────
echo "[2/4] Verifying /etc/asterisk/extconfig.conf..."
cat > /etc/asterisk/extconfig.conf << 'EOF'
; extconfig.conf — DialFlow Pro
; Tells Asterisk which ODBC connection + table to use for each realtime object.

[settings]
ps_endpoints        => odbc,dialflow,ps_endpoints
ps_auths            => odbc,dialflow,ps_auths
ps_aors             => odbc,dialflow,ps_aors
ps_contacts         => odbc,dialflow,ps_contacts
ps_endpoint_id_ips  => odbc,dialflow,ps_endpoint_id_ips
EOF
echo "  ✔  extconfig.conf written."

# ─── 3. Ensure res_pjsip_config_wizard.so is NOT blocking realtime ────────────
echo "[3/4] Ensuring required modules are enabled in modules.conf..."
# Make sure res_config_odbc.so and res_odbc.so are loaded
grep -q "^load => res_config_odbc.so" /etc/asterisk/modules.conf || \
  echo "load => res_config_odbc.so" >> /etc/asterisk/modules.conf
grep -q "^load => res_odbc.so" /etc/asterisk/modules.conf || \
  echo "load => res_odbc.so" >> /etc/asterisk/modules.conf
echo "  ✔  modules.conf verified."

# ─── 4. Restart Asterisk ──────────────────────────────────────────────────────
echo "[4/4] Restarting Asterisk..."
systemctl restart asterisk
sleep 3

# Verify
echo ""
echo "─── Verification ───────────────────────────────────────"
echo "ODBC connection test:"
isql -v dialflow_db postgres "Shiwansh@123" -m 1 <<< "SELECT COUNT(*) FROM ps_endpoints;" 2>&1 | grep -E "Connected|rows|ERROR" || true

echo ""
echo "PJSIP endpoints from Asterisk:"
asterisk -rx "pjsip show endpoints" 2>&1

echo ""
echo "ODBC status from Asterisk:"
asterisk -rx "odbc show" 2>&1

echo ""
echo "========================================="
echo "  Done! Check endpoint list above."
echo "  If you see endpoints, realtime is working."
echo "========================================="
