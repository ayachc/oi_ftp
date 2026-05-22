@echo off
chcp 65001 >nul
cd /d "%~dp0"
python server.py --port 5000
if errorlevel 1 (
  echo.
  echo 未能启动服务。请确认已安装 Python，或使用打包后的 oi-ftp.exe。
  pause
)
