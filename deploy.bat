@echo off
chcp 65001 >nul
title TokenGo 一键部署到 GitHub + Render

echo ========================================
echo   TokenGo 一键部署工具
echo ========================================
echo.

:: 检查是否安装了 gh
where gh >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] 未安装 GitHub CLI
    echo.
    echo 请先安装：https://cli.github.com/
    echo 或访问：https://github.com/cli/cli/releases
    echo.
    echo 安装后运行: gh auth login
    echo.
    pause
    exit /b 1
)

:: 检查是否已登录
gh auth status >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] 未登录 GitHub
    echo.
    echo 请运行: gh auth login
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0"

echo [1/4] 创建 GitHub 仓库...
gh repo create tokengo-api-proxy --public --source=. --push --description "TokenGo - AI API Proxy Service"

if %errorlevel% neq 0 (
    echo [!] 仓库可能已存在，跳过创建
)

echo.
echo [2/4] 仓库已创建/更新

echo.
echo [3/4] 打开 Render.com 部署页面...
echo.
echo ========================================
echo   请按以下步骤操作：
echo ========================================
echo.
echo 1. 在浏览器中打开: https://render.com
echo 2. 用 GitHub 登录
echo 3. 点击 "New +" ^> "Web Service"
echo 4. 选择 "tokengo-api-proxy" 仓库
echo 5. 设置以下配置：
echo    - Name: tokengo
echo    - Region: Singapore
echo    - Branch: main
echo    - Build Command: pip install -r requirements.txt
echo    - Start Command: python main.py
echo 6. 点击 "Create Web Service"
echo.
echo 7. 免费版数据库（可选）:
echo    - 注册 https://turso.tech
echo    - 创建数据库，获取 URL 和 Token
echo    - 在 Render 添加环境变量:
echo      TURSO_DATABASE_URL = 你的数据库URL
echo      TURSO_AUTH_TOKEN = 你的Token
echo.
echo ========================================
echo.
start https://render.com

echo [4/4] 完成！
echo.
pause
