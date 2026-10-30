@echo off
"%~dp0AgentsChat.Cli.exe" start --config "%~dp0config.local.json"
exit /b %ERRORLEVEL%
