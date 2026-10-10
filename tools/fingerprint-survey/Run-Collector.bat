@echo off
REM ExamPartner hardware survey collector — launcher
REM
REM Exists because Windows blocks .ps1 files from running on double-click by
REM default (execution policy). This wrapper runs the script for this one
REM invocation only and changes no machine setting.
REM
REM Read-only: nothing is installed, activated, or sent over the network.

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Collect-Fingerprint.ps1"

REM If PowerShell itself could not start, keep the window open so the message
REM is readable rather than vanishing.
if errorlevel 1 (
  echo.
  echo The collector did not run. Please send this message to Nitoni.
  pause
)
