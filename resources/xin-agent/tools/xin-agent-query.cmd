@echo off
setlocal
node "%~dp0xin-agent-query.js" %*
exit /b %ERRORLEVEL%
