#!/bin/bash
set -e

echo "1. Installing prerequisite packages..."
apt update
apt install -y asterisk unixodbc odbc-postgresql

echo "2. Configuring ODBC in /etc/odbc.ini and /etc/odbcinst.ini..."
cat <<EOF > /etc/odbc.ini
[dialflow_db]
Driver    = PostgreSQL Unicode
Database  = dialflow_db
Servername= 127.0.0.1
Port      = 5432
Protocol  = 7.4
EOF

cat <<EOF > /etc/odbcinst.ini
[PostgreSQL Unicode]
Description = PostgreSQL ODBC driver (Unicode version)
Driver      = /usr/lib/x86_64-linux-gnu/odbc/psqlodbcw.so
Setup       = /usr/lib/x86_64-linux-gnu/odbc/libodbcpsqlS.so
Debug       = 0
CommLog     = 0
EOF

echo "3. Copying Asterisk configuration files..."
cp asterisk/pjsip.conf      /etc/asterisk/
cp asterisk/extensions.conf /etc/asterisk/
cp asterisk/ari.conf        /etc/asterisk/
cp asterisk/http.conf       /etc/asterisk/
cp asterisk/extconfig.conf  /etc/asterisk/
cp asterisk/res_odbc.conf   /etc/asterisk/

echo "4. Enabling realtime modules in /etc/asterisk/modules.conf..."
# Ensure the lines aren't duplicated if this script is run multiple times
grep -q "load => res_config_odbc.so" /etc/asterisk/modules.conf || echo "load => res_config_odbc.so" >> /etc/asterisk/modules.conf
grep -q "load => res_odbc.so" /etc/asterisk/modules.conf || echo "load => res_odbc.so" >> /etc/asterisk/modules.conf
grep -q "load => res_pjsip_config_wizard.so" /etc/asterisk/modules.conf || echo "load => res_pjsip_config_wizard.so" >> /etc/asterisk/modules.conf

echo "5. Restarting Asterisk..."
systemctl restart asterisk

echo "Asterisk setup complete! You can verify with: asterisk -rx \"pjsip show endpoints\""
