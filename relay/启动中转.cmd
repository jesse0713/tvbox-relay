@echo off
setlocal
rem Restart the TVBox relay: free the port, then start it in the background.
rem Requires Python 3 on PATH (tick "Add python.exe to PATH" when installing).
rem No administrator rights needed.
set "PORT=8080"
set "SCRIPT=%~dp0tvbox-relay.py"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='SilentlyContinue';" ^
 "$env:TVBOX_RELAY_PORT='%PORT%';" ^
 "$py=(Get-Command pythonw.exe).Source; if(-not $py){$py=(Get-Command python.exe).Source};" ^
 "if(-not $py){Write-Host '[FAIL] Python 3 not found. Install it and tick Add to PATH.'; exit 1};" ^
 "$old=(Get-NetTCPConnection -LocalPort %PORT% -State Listen).OwningProcess;" ^
 "if($old){Stop-Process -Id $old -Force; Start-Sleep -Seconds 1};" ^
 "Start-Process -FilePath $py -ArgumentList '%SCRIPT%' -WorkingDirectory '%~dp0' -WindowStyle Hidden;" ^
 "$ok=$false; for($i=0; $i -lt 15; $i++){ Start-Sleep -Milliseconds 400; $c=New-Object Net.Sockets.TcpClient; try{ $c.Connect('127.0.0.1', %PORT%); $ok=$true; $c.Close(); break } catch { $c.Close() } };" ^
 "if($ok){Write-Host '[OK] relay is running: http://127.0.0.1:%PORT%/'}else{Write-Host '[FAIL] relay did not start. See relay.log for details.'}"

if errorlevel 1 pause
exit /b 0
