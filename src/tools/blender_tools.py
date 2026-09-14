"""
Blender 工具 — 让 Agent 可以操作 Blender 进行 3D 建模

两种工作模式：
    1. 后台模式 run_blender：
       调用 blender --background --python 执行脚本，不需要打开 Blender 界面，
       每次都是全新的空白场景，代码必须一次性建完模型并保存 .blend 文件，
       适合「帮我生成一个 XX 模型文件」这类任务。
    2. 实时模式 run_blender_live：
       通过 socket 连接到「正在运行的 Blender」（主人需先在 Blender 里运行
       scripts/blender_live_server.py 服务脚本），代码在 Blender 窗口里实时执行，
       场景状态在多次调用之间保留，可以增量修改（「再加个球」「把它变红」），
       主人能眼看着模型被造出来，适合边聊边改的交互式建模。

Blender 路径自动探测顺序：
    环境变量 BLENDER_PATH → PATH → 注册表安装记录 → 常见安装目录 / 商店版
"""

import os
import re
import json
import glob
import shutil
import socket
import tempfile
import subprocess


# 实时模式服务地址（与 scripts/blender_live_server.py 保持一致）
LIVE_HOST = "127.0.0.1"
LIVE_PORT = int(os.environ.get("BLENDER_LIVE_PORT", "9876"))


def _version_key(path: str):
    """从路径中提取 Blender 版本号，用于多版本时选最新"""
    m = re.search(r"[Bb]lender[^\d]*(\d+)[\.\_]?(\d+)?", path)
    if m:
        return (int(m.group(1)), int(m.group(2) or 0))
    return (0, 0)


def _find_blender():
    """自动探测 Blender 可执行文件路径，找不到返回 None"""
    # 1. 环境变量（可指向 blender.exe，或商店版的 blender-launcher.exe）
    env_path = os.environ.get("BLENDER_PATH", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path

    # 2. PATH 中的 blender
    found = shutil.which("blender")
    if found:
        return found

    candidates = []

    # 3. 注册表卸载记录里的 InstallLocation（常规安装版都会写）
    try:
        import winreg
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, sub in reg_paths:
            try:
                with winreg.OpenKey(hive, sub) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sk:
                                name = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                                if "blender" in name.lower():
                                    loc = str(
                                        winreg.QueryValueEx(sk, "InstallLocation")[0]
                                    )
                                    for exe in ("blender.exe", "blender-launcher.exe"):
                                        p = os.path.join(loc, exe)
                                        if os.path.exists(p):
                                            candidates.append(p)
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass  # 非 Windows 平台

    # 4. 常见安装目录（各盘符 Blender Foundation、Program Files、自定义目录）
    roots = [
        r"C:\Program Files\Blender Foundation",
        r"C:\Program Files (x86)\Blender Foundation",
    ]
    for drive in "CDEFGH":
        roots.append(os.path.join(drive + ":\\", "Blender Foundation"))
        roots.append(drive + ":\\")
    for root in roots:
        candidates.extend(glob.glob(os.path.join(root, "Blender*", "blender*.exe")))
        candidates.extend(glob.glob(
            os.path.join(root, "Blender Foundation", "Blender*", "blender*.exe")
        ))

    # 5. 微软商店版（WindowsApps 目录权限受限，能找到就用 launcher）
    candidates.extend(glob.glob(
        r"C:\Program Files\WindowsApps\BlenderFoundation.*\blender-launcher.exe"
    ))

    # 去重 → 版本高的优先；同版本 blender.exe（真身）优先于 blender-launcher.exe
    # （launcher 只是启动器，后台模式不转发 --python 脚本；商店版 UWP 权限受限才必须用它）
    candidates = [p for p in set(candidates) if os.path.exists(p)]
    candidates.sort(key=lambda p: (
        _version_key(p),
        os.path.basename(p).lower() == "blender.exe",
    ))
    return candidates[-1] if candidates else None


# 模块加载时探测一次
BLENDER_PATH = _find_blender()


def run_blender(code: str) -> str:
    """
    后台模式：把 bpy 代码交给 Blender --background 执行（无需打开 Blender 界面）

    参数:
        code: Blender Python 代码（使用 bpy 模块）

    注意:
        每次执行都是全新的空白 Blender 场景，代码必须完整：
        创建模型 → 最后用 bpy.ops.wm.save_as_mainfile() 保存到 output/ 目录
    """
    if not BLENDER_PATH or not os.path.exists(BLENDER_PATH):
        return json.dumps({
            "error": "找不到 Blender，后台模式不可用",
            "hint": "请安装 Blender: https://www.blender.org/download/",
            "hint2": "或设置环境变量 BLENDER_PATH 指向 blender.exe 的完整路径后重启服务",
            "alternative": "也可以让主人在 Blender 里运行 scripts/blender_live_server.py 后改用实时模式",
        }, ensure_ascii=False)

    # 写入临时脚本文件。
    # 包一层 try/except：Blender 后台模式下脚本报错时进程退出码仍是 0，
    # 不包的话工具会误报成功；这里捕获异常后打印失败标记并以非 0 退出。
    wrapped = (
        "import sys, traceback\n"
        "try:\n"
        + "\n".join("    " + line if line.strip() else "" for line in code.split("\n"))
        + "\n"
        "except Exception:\n"
        "    traceback.print_exc()\n"
        "    print('BLENDER_SCRIPT_FAILED')\n"
        "    sys.exit(1)\n"
    )
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(wrapped)
            temp_path = f.name
    except Exception as e:
        return json.dumps({"error": f"写入临时脚本失败: {str(e)}"}, ensure_ascii=False)

    try:
        result = subprocess.run(
            [BLENDER_PATH, "--background", "--python", temp_path],
            capture_output=True,
            text=True,
            timeout=180,  # 复杂场景可能需要更久
            cwd=os.getcwd(),
        )

        try:
            os.unlink(temp_path)
        except OSError:
            pass

        output = ""
        if result.stdout:
            # 过滤 Blender 启动日志，只留有用输出
            skip_prefixes = (
                "Read prefs:", "found bundled", "Read blend:",
                "Info:", "Fra:", "Saved:", "Time:", "Blender ",
                "Warning: ", "AL lib", "GPU ", "System",
                # 第三方插件在后台模式注册时的噪声日志
                "Registered MACHIN3tools", "Exception in module register",
                "WARNING: Keyconfig", "警告:", "信息:", "Warning: '",
            )
            useful_lines = []
            for line in result.stdout.strip().split("\n"):
                s = line.strip()
                if s and not s.startswith(skip_prefixes):
                    useful_lines.append(line)
            output = "\n".join(useful_lines[-30:])

        if result.returncode != 0 or "BLENDER_SCRIPT_FAILED" in (result.stdout or ""):
            # 脚本异常的 traceback 打印在 stdout 里（Blender 退出码也可能非 0）
            detail = "\n".join(filter(None, [
                result.stderr.strip() if result.stderr else "",
                output,
            ]))
            return json.dumps({
                "error": "Blender 脚本执行出错（建模代码有 bug，请根据 traceback 修正后重试）",
                "detail": detail[-1200:] if detail else "未知错误",
            }, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "message": f"后台 Blender 脚本执行成功（{os.path.basename(BLENDER_PATH)}）",
            "output": output[-800:] if output else "(无输出，记得在代码里 save_as_mainfile 保存 .blend)",
        }, ensure_ascii=False)

    except subprocess.TimeoutExpired:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        return json.dumps({"error": "Blender 执行超时（超过 180 秒），请简化场景或分批建模"},
                          ensure_ascii=False)
    except Exception as e:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        return json.dumps({"error": f"执行失败: {str(e)}"}, ensure_ascii=False)


def run_blender_live(code: str) -> str:
    """
    实时模式：把 bpy 代码发送到「正在运行的 Blender」中执行

    前提：主人已在 Blender 里运行 scripts/blender_live_server.py 服务脚本。
    代码在主人眼前的 Blender 场景里实时执行，场景状态连续保留，可增量修改。
    """
    try:
        with socket.create_connection((LIVE_HOST, LIVE_PORT), timeout=10) as sock:
            sock.settimeout(180)
            sock.sendall((json.dumps({"id": "1", "code": code}) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            data = json.loads(buf.decode("utf-8").strip())
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        return json.dumps({
            "error": f"连不上 Blender 实时服务（{LIVE_HOST}:{LIVE_PORT}）",
            "reason": str(e),
            "hint": "请主人先在 Blender 中启动实时服务：",
            "步骤1": "打开 Blender → 顶部工作区切到「Scripting（脚本）」",
            "步骤2": "「打开」文件 d:/ai工作流/scripts/blender_live_server.py → 点「运行脚本」(Alt+P)",
            "步骤3": "看到「实时建模服务已启动」后，回到网页重新下达建模指令",
            "备选": "或直接改用 run_blender 后台模式，无需打开 Blender 即可生成 .blend 文件",
        }, ensure_ascii=False)

    if data.get("ok"):
        mode = "GUI 界面" if data.get("mode") == "gui" else "后台常驻"
        return json.dumps({
            "success": True,
            "message": f"已在实时 Blender（{mode}）中执行，当前场景物体数：{data.get('objects')}",
            "output": (data.get("stdout") or "(无输出)")[-800:],
        }, ensure_ascii=False)

    return json.dumps({
        "error": "Blender 实时执行代码出错",
        "detail": (data.get("error") or "未知错误")[-1000:],
        "hint": "根据报错修正 bpy 代码后重试；场景状态仍保留，不需要从头重建",
    }, ensure_ascii=False)


# 工具定义（给 AI 看的说明书）
TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "run_blender",
            "description": (
                "【后台模式】在 Blender 中执行 Python(bpy) 代码进行 3D 建模，无需打开 Blender 界面。"
                "每次执行都是全新空白场景，代码必须完整：清理默认物体→创建模型/材质/灯光/相机→"
                "最后用 bpy.ops.wm.save_as_mainfile(filepath='d:/ai工作流/output/xxx.blend') 保存文件。"
                "适合一次性生成完整模型文件。注意路径用正斜杠 /。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Blender Python 代码。常用操作:\n"
                            "- 清空场景: bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()\n"
                            "- 立方体: bpy.ops.mesh.primitive_cube_add(size=2, location=(0,0,0))\n"
                            "- 球体: bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=(0,0,0))\n"
                            "- 圆柱: bpy.ops.mesh.primitive_cylinder_add(radius=1, depth=2, location=(0,0,0))\n"
                            "- 材质: mat=bpy.data.materials.new('Red'); mat.diffuse_color=(1,0,0,1); "
                            "obj.data.materials.append(mat)\n"
                            "- 保存: bpy.ops.wm.save_as_mainfile(filepath='d:/ai工作流/output/xxx.blend')\n"
                            "- 导出: bpy.ops.export_scene.gltf(filepath='d:/ai工作流/output/xxx.glb')\n"
                            "print() 的内容会作为输出返回，关键结果请 print 出来确认。"
                        )
                    }
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_blender_live",
            "description": (
                "【实时模式】在主人正打开的 Blender 窗口里实时执行 bpy 代码，模型会立刻出现在界面上，"
                "且场景状态在多次调用之间保留——可以先建主体，再逐步添加细节、改材质、调位置，"
                "适合交互式建模。需要主人已在 Blender 中运行 scripts/blender_live_server.py；"
                "若返回连接失败，把启动步骤转告主人后可改用 run_blender 后台模式。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Blender Python 代码（bpy 已可用，也可自行 import）。\n"
                            "场景会持续保留，不要每次都 delete 全部物体，只做增量修改。\n"
                            "关键结果用 print() 输出（会回传），例如 print('物体数:', len(bpy.data.objects))。\n"
                            "需要交付文件时再保存: bpy.ops.wm.save_as_mainfile(filepath='d:/ai工作流/output/xxx.blend')"
                        )
                    }
                },
                "required": ["code"]
            }
        }
    }
]

# 工具映射
TOOLS_MAP = {
    "run_blender": run_blender,
    "run_blender_live": run_blender_live,
}
