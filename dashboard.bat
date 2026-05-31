@echo off
REM ============================================================================
REM  BTC PM Arb - Dashboard launcher (double-click from Explorer)
REM
REM  The dashboard runs IN-PROCESS with the agent: one command starts the
REM  agent, the FastAPI backend, and serves the SPA at the URL below.  This
REM  script starts it, waits for /api/health, opens your default browser, and
REM  stops everything when you close this window (or press Ctrl+C).
REM
REM  Usage (double-click = paper):
REM    dashboard.bat            paper-trading dashboard (default)
REM    dashboard.bat paper      same as default
REM    dashboard.bat replay     headless deterministic replay (NO dashboard)
REM
REM  Note: "live" (real-money) is NOT a launch option -- the agent always
REM  starts in dry-run/paper.  Switching to live is a guarded runtime toggle
REM  inside the dashboard Controls tab, not a CLI flag.
REM
REM  Localhost only.  To deploy on a server later, change HOST/PORT below AND
REM  the bind in src/btc_pm_arb/main.py:_dashboard_task (currently 127.0.0.1).
REM  No other rewrite needed.
REM ============================================================================

setlocal

REM --- Config (edit these for a server deploy) --------------------------------
set "HOST=127.0.0.1"
set "PORT=8000"
set "URL=http://%HOST%:%PORT%/"
if not defined DASHBOARD_TOKEN set "DASHBOARD_TOKEN=dev-token-change-me"

REM --- Run from the repo root (this script's own directory) --------------------
cd /d "%~dp0"

REM --- Mode arg (default: paper) ----------------------------------------------
set "MODE=%~1"
if "%MODE%"=="" set "MODE=paper"

echo ============================================================
echo   BTC PM Arb dashboard launcher
echo   mode         : %MODE%
echo   url          : %URL%
echo   controls tok : %DASHBOARD_TOKEN%
echo   (close this window or press Ctrl+C to stop)
echo ============================================================
echo.

if /i "%MODE%"=="replay" goto replay

REM --- paper: start agent + dashboard, open the browser once healthy ----------
REM Background poll of /api/health, then open the default browser.  Falls back
REM to opening the URL anyway after ~30s so a slow start still surfaces the UI.
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -Command "$u='%URL%api/health'; for($i=0;$i -lt 60;$i++){ try { if((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $u).StatusCode -eq 200){ Start-Process '%URL%'; exit } } catch {}; Start-Sleep -Milliseconds 500 }; Start-Process '%URL%'"

REM Foreground (blocking): closing this window tears the agent + dashboard down.
py -3.12 -m btc_pm_arb.main
goto end

:replay
echo Replay is headless and deterministic: it streams the recorded frames
echo through the agent and exits.  No dashboard is served in replay mode.
echo.
py -3.12 -m btc_pm_arb.main --mode replay
goto end

:end
endlocal
