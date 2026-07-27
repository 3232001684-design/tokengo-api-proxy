@echo off
chcp 65001 >nul
title TokenGo Service - 一键启动
echo ========================================
echo   TokenGo 中转服务 - 一键启动
echo   (公网隧道自动连接 + 域名自动同步)
echo ========================================
echo.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1"
pause
