@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo ===========================
echo   Starting DialFlow Pro
echo ===========================

echo [0/5] Setting up WSL2 port forwarding for Asterisk...
for /f "tokens=*" %%i in ('wsl bash -c "hostname -I | awk '{print $1}'"') do set WSL_IP=%%i
echo WSL2 IP: %WSL_IP%
netsh interface portproxy delete v4tov4 listenport=8089 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy delete v4tov4 listenport=8088 listenaddress=0.0.0.0 >nul 2>&1
netsh interface portproxy add v4tov4 listenport=8089 listenaddress=0.0.0.0 connectport=8089 connectaddress=%WSL_IP%
netsh interface portproxy add v4tov4 listenport=8088 listenaddress=0.0.0.0 connectport=8088 connectaddress=%WSL_IP%
netsh advfirewall firewall delete rule name="DialFlow Asterisk HTTP" >nul 2>&1
netsh advfirewall firewall delete rule name="DialFlow Asterisk HTTPS" >nul 2>&1
netsh advfirewall firewall add rule name="DialFlow Asterisk HTTP" protocol=TCP dir=in localport=8088 action=allow
netsh advfirewall firewall add rule name="DialFlow Asterisk HTTPS" protocol=TCP dir=in localport=8089 action=allow
echo Port forwarding configured.

echo [1/5] Starting WSL Services (Asterisk ^& Redis)...
wsl -u root bash -c "service redis-server start; /usr/sbin/asterisk"

echo [2/5] Starting Celery Worker...
start "Dialflow: Celery Worker" cmd /k "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & .\venv\Scripts\activate & celery -A dialflow worker -l info --pool=solo"

echo [3/5] Starting Celery Beat...
start "Dialflow: Celery Beat" cmd /k "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & .\venv\Scripts\activate & celery -A dialflow beat -l info"

echo [4/5] Starting Django Server (Daphne HTTPS on Port 8000)...
start "Dialflow: Django Server (Daphne)" cmd /k "chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & .\venv\Scripts\activate & daphne -e ssl:8000:privateKey=keys/key.pem:certKey=keys/cert.pem dialflow.asgi:application"

echo.
echo All services successfully launched!
echo.
echo Open in browser: https://192.168.1.8:8000
echo Trust Asterisk:  https://192.168.1.8:8089/httpstatus
echo.
echo NOTE: Run this script as Administrator for port forwarding to work.
