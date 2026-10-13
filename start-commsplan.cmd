@echo off
rem Preserve the legacy launcher's installation, profile, receipt, and exit behavior.
call "%~dp0start-freqinout.cmd" %*
exit /b %errorlevel%
