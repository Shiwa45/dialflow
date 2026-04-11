@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo ===========================
echo   Starting DialFlow Pro
echo ===========================

echo [0/7] Setting up WSL2 port forwarding for Asterisk...
for /f "tokens=*" %%i in ('wsl bash -c "hostname -I | awk '{print $1}'"') do set WSL_IP=%%i
echo WSL2 IP: %WSL_IP%
netsh interface portproxy delete v4tov4 listenport=8089 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy delete v4tov4 listenport=8088 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy delete v4tov4 listenport=5060 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy add v4tov4 listenport=8089 listenaddress=0.0.0.0 connectport=8089 connectaddress=%WSL_IP%
netsh interface portproxy add v4tov4 listenport=8088 listenaddress=0.0.0.0 connectport=8088 connectaddress=%WSL_IP%
netsh interface portproxy add v4tov4 listenport=5060 listenaddress=0.0.0.0 connectport=5060 connectaddress=%WSL_IP%
netsh advfirewall firewall delete rule name="DialFlow Asterisk HTTP" >nul 2>&1
netsh advfirewall firewall delete rule name="DialFlow Asterisk HTTPS" >nul 2>&1
netsh advfirewall firewall delete rule name="DialFlow SIP UDP" >nul 2>&1
netsh advfirewall firewall delete rule name="DialFlow SIP TCP" >nul 2>&1
netsh advfirewall firewall add rule name="DialFlow Asterisk HTTP" protocol=TCP dir=in localport=8088 action=allow
netsh advfirewall firewall add rule name="DialFlow Asterisk HTTPS" protocol=TCP dir=in localport=8089 action=allow
netsh advfirewall firewall add rule name="DialFlow SIP UDP" protocol=UDP dir=in localport=5060 action=allow
netsh advfirewall firewall add rule name="DialFlow SIP TCP" protocol=TCP dir=in localport=5060 action=allow
echo Port forwarding configured.

echo [1/7] Starting WSL Services (Asterisk ^& Redis)...
wsl -u root bash -c "service redis-server start; /usr/sbin/asterisk"

echo [2/7] Applying migrations and seeding beat schedule...
call .\venv\Scripts\activate
python manage.py migrate --run-syncdb --no-input
python manage.py setup_beat_schedule
echo Migrations and beat schedule OK.

echo [2b/7] Re-syncing PJSIP phone endpoints (WebRTC dtls_auto_generate_cert + rtcp_mux)...
python manage.py shell -c "from telephony.models import Phone; phones=Phone.objects.filter(is_active=True); [p.sync_to_asterisk() for p in phones]; print(f'Re-synced {phones.count()} phone endpoint(s)')"
echo PJSIP endpoints synced.

echo [3/7] Syncing Asterisk config files to WSL...
python -c "import subprocess; subprocess.run(['wsl','-u','root','tee','/etc/asterisk/extensions.conf'], input=open('asterisk/extensions.conf','rb').read(), capture_output=True)"
python -c "import subprocess; subprocess.run(['wsl','-u','root','tee','/etc/asterisk/pjsip.conf'], input=open('asterisk/pjsip.conf','rb').read(), capture_output=True)"
wsl -u root bash -c "/usr/sbin/asterisk -rx 'module reload res_pjsip.so'" 2>nul || echo (PJSIP reload skipped — Asterisk not running yet, it will load fresh config on start)
python manage.py shell -c "from telephony.models import Carrier; Carrier.rebuild_asterisk_dialplan(); print('Carrier dialplan synced + Asterisk dialplan reloaded')"
echo Asterisk config synced.

echo [4/7] Priming lead hoppers (Redis queue)...
python manage.py shell -c "from campaigns.hopper import fill_all_hoppers; r=fill_all_hoppers(); print('Hopper primed:', r)"
echo Hoppers primed.

echo [5/7] Starting ARI Worker...
start "DialFlow: ARI Worker" cmd /k "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & .\venv\Scripts\activate & python manage.py run_ari"

echo [6/7] Starting Celery Worker + Beat...
start "DialFlow: Celery Worker" cmd /k "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & .\venv\Scripts\activate & celery -A dialflow worker -l info --pool=solo"
start "DialFlow: Celery Beat" cmd /k "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & .\venv\Scripts\activate & celery -A dialflow beat -l info"

echo [7/7] Starting Django Server (Daphne HTTPS on Port 8000)...
start "DialFlow: Django Server (Daphne)" cmd /k "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & .\venv\Scripts\activate & daphne -e ssl:8000:privateKey=keys/key.pem:certKey=keys/cert.pem dialflow.asgi:application"

echo.
echo All services successfully launched!
echo.
echo Open in browser: https://192.168.1.13:8000
echo Trust Asterisk:  https://192.168.1.13:8089/httpstatus
echo.
echo NOTE: Run this script as Administrator for port forwarding to work.
echo.
echo Predictive dialer tick runs every second via Celery Beat.
echo Hopper fills every 30 seconds. Call hours: 09:00 - 21:00 IST.
