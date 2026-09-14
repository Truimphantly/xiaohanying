@echo off
chcp 65001 >nul
title 小寒影实时建模启动器
setlocal

REM ============================================
REM  小寒影实时建模 - 一键启动
REM  作用：启动 Blender 并自动运行实时服务脚本，
REM  然后回到网页 http://localhost:5000 说
REM  「用实时模式建模」即可，模型会实时出现在 Blender 窗口里。
REM  （以后升级了 Blender 版本，只需改下面 BLENDER_EXE 这一行路径）
REM ============================================

REM 工作目录切到项目根目录（本 bat 在 scripts\ 里）
cd /d "%~dp0.."

set "BLENDER_EXE=D:\Blender Foundation\Blender 3.6\blender.exe"
set "SERVER_PY=%~dp0blender_live_server.py"

if not exist "%BLENDER_EXE%" (
  echo [错误] 找不到 Blender：%BLENDER_EXE%
  echo 请右键编辑本文件，把 BLENDER_EXE 改成新的 blender.exe 路径。
  pause
  exit /b 1
)

REM ---- 防双开：9876 已在监听就不再启动第二个 Blender ----
netstat -ano | findstr ":9876 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo.
  echo  [小寒影] 实时建模服务已经在运行啦，不需要重复启动喵~
  echo  直接回到网页 http://localhost:5000 开始建模即可。
  echo  如需重启服务，请先关掉正在运行服务的 Blender 窗口，再双击本启动器。
  echo.
  pause
  exit /b 0
)

echo 正在启动 Blender 实时建模服务喵...
start "" "%BLENDER_EXE%" --python "%SERVER_PY%"

REM ---- 等待并确认服务真的起来了 ----
timeout /t 10 >nul
netstat -ano | findstr ":9876 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo.
  echo  [小寒影] 服务启动成功喵！回到网页 http://localhost:5000 说
  echo  「用实时模式建个 XXX」，模型就会实时出现在 Blender 窗口里~
  echo.
) else (
  echo.
  echo  [警告] 没有检测到服务端口，Blender 里的脚本可能启动失败。
  echo  请把日志发给小寒影排查：%~dp0..\output\blender_live.log
  echo.
  pause
)
timeout /t 5 >nul
