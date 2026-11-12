@echo off
rem Double-click this file, then click "Yes" on the UAC prompt.
rem It only opens TCP 8080 inbound so your TV can reach the relay.
powershell -NoProfile -Command "Start-Process -FilePath netsh -ArgumentList 'advfirewall','firewall','add','rule','name=TVBox Relay 8080','dir=in','action=allow','protocol=TCP','localport=8080' -Verb RunAs -Wait"
echo Done. If UAC was cancelled, the TV still cannot connect.
pause
