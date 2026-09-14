# -*- coding: utf-8 -*-
"""
小寒影实时建模服务 —— 在 Blender 内部运行
==========================================

【怎么用】

  方式一（推荐，能看着模型实时出现）：
    1. 打开 Blender
    2. 顶部工作区标签切到「Scripting（脚本）」
    3. 点文本编辑器里的「打开」按钮（注意：不是顶部「文件→打开」菜单，
       那个只能打开 .blend 工程，会看不到 .py 文件）
    4. 选择本文件：d:/ai工作流/scripts/blender_live_server.py
    5. 点「运行脚本」按钮（或鼠标放在编辑器里按 Alt+P）
    6. 看到「实时建模服务已启动」就成功了，回网页让 AI 建模即可

  方式二（最省事，双击即可）：
    双击桌面的「小寒影实时建模」快捷方式（或 scripts/启动实时建模.bat），
    Blender 会自动打开并运行好服务，无需任何操作。

【原理】
  在 Blender 内开一个本地 socket 服务（127.0.0.1:9876），
  AI 工作流把 bpy 代码发过来，由 Blender 主线程执行并回传结果。
  bpy 不是线程安全的，所以收到的代码一律通过 Blender 定时器
  投递到主线程执行；运行日志写入 output/blender_live.log 方便排查。
"""

import bpy
import sys
import io
import os
import json
import time
import queue
import socket
import threading
import traceback

HOST = "127.0.0.1"
PORT = int(os.environ.get("BLENDER_LIVE_PORT", "9876"))

# 运行日志（GUI 模式下 print 看不到，全部写文件方便排查）
_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "blender_live.log",
)

# 启动时固定的静态信息（socket 线程直接读，避免跨线程碰 bpy）
MODE = "background" if bpy.app.background else "gui"
VERSION = bpy.app.version_string

# 待执行任务队列：(提交时间戳, code, result_queue)
_task_q = queue.Queue()
_TASK_TTL = 120  # 任务超过 120 秒没执行（如模态对话框阻塞）就丢弃

# 主线程定时器心跳计数（list 容器包裹，函数内可直接改，无需 global 声明）
_pump_count = [0]


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass  # 控制台被关闭时 print 会抛异常，不能让日志整体失效
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# 入口日志：脚本一执行就留痕，并记录解析后的真实路径，方便定位"日志失踪"
log("脚本开始执行 __file__=%s" % os.path.abspath(__file__))


def _execute(code):
    """在 Blender 主线程执行代码，捕获标准输出与异常"""
    ns = {"bpy": bpy, "__name__": "__ai_agent__"}
    buf = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = buf
        exec(compile(code, "<ai_agent>", "exec"), ns, ns)
        return {
            "ok": True,
            "stdout": buf.getvalue()[-2000:],
            "objects": len(bpy.data.objects),
            "mode": MODE,
            "version": VERSION,
        }
    except Exception:
        return {
            "ok": False,
            "error": traceback.format_exc()[-2000:],
            "objects": len(bpy.data.objects),
            "mode": MODE,
        }
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr


def _handle_conn(conn):
    """每个连接一个线程：读一行 JSON → 投递主线程 → 回一行 JSON"""
    try:
        f = conn.makefile("rwb")
        while True:
            line = f.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            rid = req.get("id")
            if req.get("ping"):
                resp = {"id": rid, "ok": True, "pong": True,
                        "mode": MODE, "version": VERSION}
            else:
                done = queue.Queue()
                _task_q.put((time.time(), req.get("code", ""), done))
                try:
                    resp = done.get(timeout=60)  # 主线程 60 秒没处理就报错
                except queue.Empty:
                    resp = {
                        "ok": False,
                        "error": (
                            "Blender 主线程 60 秒内没有执行代码。"
                            "通常是因为 Blender 里开着模态窗口（文件浏览器/另存为/弹窗），"
                            "请关掉这些窗口后重试。"
                        ),
                    }
                resp["id"] = rid

            f.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            f.flush()
    except Exception as e:
        log("连接线程异常: %r" % e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _server_thread():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 注意：Windows 上不要设置 SO_REUSEADDR——它允许两个进程同时绑定同一
    # 端口（连接随机分发），会导致后启动的实例处于"能连上但不干活"的半死状态。
    # 不设置时，端口被占用会直接 bind 失败，走到下面的明确报错分支。
    try:
        srv.bind((HOST, PORT))
    except OSError as e:
        log("端口 %d 被占用：%s（可能已有另一个 Blender 在运行本服务，本实例不再重复启动服务）" % (PORT, e))
        return
    srv.listen(5)
    log("=" * 40)
    log("实时建模服务已启动：%s:%d（%s 模式, Blender %s）" % (HOST, PORT, MODE, VERSION))
    log("回到网页让 AI 建模即可，模型会实时出现在 Blender 里喵~")

    if MODE == "gui":
        # 看门狗：8 秒后确认主线程定时器真的在跑（不碰 bpy，只读计数器）
        def _watchdog():
            time.sleep(8)
            if _pump_count[0] == 0:
                log("【严重警告】启动 8 秒后主线程定时器仍未触发，实时指令将无法执行！")
                log("请在 Blender 的 Scripting 工作区文本编辑器里重新运行本脚本（Alt+P），或重启 Blender")
            else:
                log("看门狗确认：主线程定时器运行正常（已轮询 %d 次）" % _pump_count[0])

        threading.Thread(target=_watchdog, daemon=True).start()
    while True:
        try:
            conn, _addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=_handle_conn, args=(conn,), daemon=True).start()


def _pump_tasks():
    """主线程轮询：把队列里的代码全部执行掉（GUI 定时器回调）。
    整体包 try/except，任何异常都不让 Blender 注销本定时器。"""
    try:
        _pump_count[0] += 1
        if _pump_count[0] == 1:
            log("主线程定时器首次触发，实时指令通道就绪")
        elif _pump_count[0] % 3000 == 0:
            log("心跳：服务存活（已轮询 %d 次）" % _pump_count[0])
        while True:
            try:
                ts, code, done = _task_q.get_nowait()
            except queue.Empty:
                break
            if time.time() - ts > _TASK_TTL:
                log("丢弃过期任务（等待超过 %d 秒）" % _TASK_TTL)
                done.put({"ok": False, "error": "任务已过期丢弃，请重试"})
                continue
            done.put(_execute(code))
    except Exception as e:
        log("定时器回调异常（已忽略，服务继续）: %r" % e)
    return 0.1   # 0.1 秒后继续轮询


def _register_pump_timer():
    """注册主线程轮询定时器（幂等），返回是否成功"""
    try:
        if not bpy.app.timers.is_registered(_pump_tasks):
            bpy.app.timers.register(_pump_tasks, first_interval=0.2)
        return bpy.app.timers.is_registered(_pump_tasks)
    except Exception as e:
        log("定时器注册失败：%r\n%s" % (e, traceback.format_exc()))
        return False


from bpy.app.handlers import persistent


@persistent
def _on_file_loaded(_dummy=None):
    """关键修复：Blender 每次加载 .blend 文件（文件→打开/新建/恢复）都会
    清空所有 bpy.app.timers 定时器，本钩子（@persistent 可跨文件加载存活）
    在加载完成后自动把轮询定时器注册回来。"""
    log("检测到 .blend 文件加载，重新注册实时服务定时器")
    if _register_pump_timer():
        log("定时器已随文件加载重新注册，实时指令通道恢复")
    else:
        log("【严重警告】文件加载后定时器重新注册失败！")


def start():
    threading.Thread(target=_server_thread, daemon=True).start()

    if bpy.app.background:
        # 后台模式：脚本结束进程就会退出，主线程自己循环保持常驻
        log("后台常驻模式，关闭此窗口或按 Ctrl+C 停止服务")
        try:
            while True:
                _pump_tasks()
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
    else:
        # GUI 模式：注册 Blender 定时器在主线程轮询，不卡界面
        if _register_pump_timer():
            log("定时器注册成功，服务在后台运行中")
        else:
            log("【严重警告】定时器注册后未生效（Blender 启动时机问题），实时指令将无法执行！")
            log("请在 Blender 的 Scripting 工作区文本编辑器里重新运行本脚本（Alt+P）")

        # 关键：挂上文件加载钩子，防止「打开/新建 .blend」清空定时器后服务假死
        # （先清掉同名旧钩子：脚本被 Alt+P 重复运行时，旧函数对象可能还留在列表里）
        for _h in list(bpy.app.handlers.load_post):
            if getattr(_h, "__name__", "") == "_on_file_loaded":
                bpy.app.handlers.load_post.remove(_h)
        bpy.app.handlers.load_post.append(_on_file_loaded)
        log("已挂载文件加载钩子（加载 .blend 后定时器自动恢复）")


start()
