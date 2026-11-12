@echo off
setlocal
rem Rebuild every generated playlist:
rem   live-ln.m3u  Liaoning Mobile IPTV (optional, needs that ISP)
rem   live.m3u     merged + speed filtered
rem   live6.m3u    IPv6 HD lines
rem Requires Python 3 on PATH (or the py launcher).

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (
  echo [FAIL] Python 3 not found in PATH.
  pause
  exit /b 1
)

%PY% "%~dp0build-live-ln.py"
%PY% "%~dp0build-live.py"
%PY% "%~dp0build-live6.py"

if errorlevel 1 pause
exit /b 0
