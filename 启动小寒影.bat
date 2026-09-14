@echo off
chcp 65001 >nul
title 小寒影 - 一键启动喵~
setlocal enabledelayedexpansion

REM ============================================
REM  小寒影 AI 工作流 - 一键启动器
REM  作用：检查环境 → 启动 Flask 服务(5000) → 自动打开浏览器
REM  双击本文件即可，喵~
REM ============================================

REM 切到项目根目录（本 bat 就在根目录里）
cd /d "%~dp0"

echo.
echo  ============================================================
echo    小 寒 影   AI   工 作 流
echo    一键启动器   ^(^/^=^/^)   喵呜~ 正在准备启动...
echo  ============================================================
echo.

REM ---------- 1. 检查 Python ----------
where python >nul 2>&1
if errorlevel 1 (
  echo  [错误] 没有找到 Python 喵...
  echo  请先安装 Python 3.8+ 并勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo  [1/4] 检测到 !PYVER!

REM ---------- 2. 检查 .env 配置 ----------
if not exist ".env" (
  echo.
  echo  [错误] 找不到 .env 配置文件喵...
  echo  请复制 .env.example 为 .env，并填入 API 密钥。
  echo.
  pause
  exit /b 1
)
echo  [2/4] 配置文件 .env 已就绪

REM ---------- 3. 检查依赖 ----------
python -c "import flask, openai, dotenv, requests" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [提示] 缺少依赖库，正在自动安装喵...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)
echo  [3/4] 依赖库检查通过

REM ---------- 4. 检查端口 5000 是否被占用 ----------
set "PORT_BUSY=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000 " ^| findstr "LISTENING"') do (
  set "PORT_BUSY=1"
  set "OLD_PID=%%p"
)

if "!PORT_BUSY!"=="1" (
  echo.
  echo  [提示] 端口 5000 已经在监听啦，小寒影可能已经在运行中喵~
  echo         占用进程 PID = !OLD_PID!
  echo.
  choice /c YN /m "  要强制重启服务吗（先结束旧进程再启动）"
  if !errorlevel!==2 (
    echo.
    echo  好的，直接打开浏览器喵~
    start "" "http://localhost:5000"
    timeout /t 3 >nul
    exit /b 0
  )
  echo.
  echo  正在结束旧进程 PID=!OLD_PID! ...
  taskkill /F /PID !OLD_PID! >nul 2>&1
  timeout /t 2 >nul
)
echo  [4/4] 端口 5000 可用

echo.
echo  ------------------------------------------------------------
echo   小寒影启动中... 浏览器会自动打开 http://localhost:5000
echo   关闭本窗口即可停止服务喵~
echo  ------------------------------------------------------------
echo.

REM 延迟 3 秒后自动打开浏览器（等服务先起来）
start "" cmd /c "timeout /t 3 >nul & start "" http://localhost:5000"

REM 启动主程序（前台运行，日志直接显示在本窗口）
python "src\main.py"

echo.
echo  小寒影已经下班啦，主人下次再来找我玩喵~ (^=^'^=^)
pause
