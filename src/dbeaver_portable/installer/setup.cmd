@echo off
rem Runs setup.ps1 with Windows PowerShell 5.1, which every supported Windows ships.
rem The working folder moves to %TEMP% so that uninstalling can delete the install folder, and
rem "& exit /b" stops cmd from reading this file again after setup.ps1 has deleted it.
cd /d "%TEMP%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %* & exit /b
