#!/bin/bash
set -e

echo "========================================="
echo "  DialFlow Pro — Carrier Dialplan Setup "
echo "========================================="

# 1. Ensure the generated dialplan config file exists in Asterisk's config directory
touch /etc/asterisk/carriers_dialplan.conf
chown asterisk:asterisk /etc/asterisk/carriers_dialplan.conf
chmod 644 /etc/asterisk/carriers_dialplan.conf

# 2. Add an #include rule to /etc/asterisk/extensions.conf
if ! grep -q "carriers_dialplan.conf" /etc/asterisk/extensions.conf; then
    echo "" >> /etc/asterisk/extensions.conf
    echo "; Auto-generated include for DialFlow Pro Carriers" >> /etc/asterisk/extensions.conf
    echo "#include /etc/asterisk/carriers_dialplan.conf" >> /etc/asterisk/extensions.conf
    echo "  ✔  Added #include to extensions.conf."
else
    echo "  ✔  #include already present in extensions.conf."
fi

# 3. Allow user 'easyian' to execute the reload command via sudo without a password
# (Asterisk dialplan reloading requires root, but the web UI runs as 'easyian')
echo "easyian ALL=(ALL) NOPASSWD: /usr/sbin/asterisk -rx module reload pbx_config.so" > /etc/sudoers.d/dialflow_asterisk
chmod 0440 /etc/sudoers.d/dialflow_asterisk
echo "  ✔  Added sudoers rule for Asterisk updates."

# 4. Perform an initial reload
systemctl restart asterisk || asterisk -rx "module reload pbx_config.so"

echo "========================================="
echo "  Setup Complete! "
echo "  You can now write raw dialplan code in the Web UI."
echo "========================================="
