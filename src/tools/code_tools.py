"""
代码执行工具
给 AI 提供在本地执行 Python 代码的能力
"""
import json
import subprocess
import tempfile
import os


def execute_python(code: str) -> str:
    """
    执行 Python 代码并返回结果

    参数:
        code: 要执行的 Python 代码

    返回:
        执行结果（标准输出）
    """
    temp_path = None
    try:
        # 写入临时文件
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            temp_path = f.name

        # 强制子进程以 UTF-8 输出，并显式按 UTF-8 解码。
        # 否则中文 Windows 默认按 GBK 解码子进程的 UTF-8 输出，
        # 解码线程一崩 stdout 就变 None，工具必挂（曾导致"代码验证"步骤连环失败）。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            ["python", temp_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30,  # 30 秒超时
        )

        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()

        return json.dumps({
            "success": result.returncode == 0,
            "output": output if output else "(无输出)",
            "error": error if error else None
        }, ensure_ascii=False, indent=2)

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "代码执行超时（30秒）"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"执行失败: {str(e)}"}, ensure_ascii=False)
    finally:
        # 无论成功、超时还是异常，都清理临时文件，不留垃圾
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": "执行一段 Python 代码并返回结果。当需要运行 Python 代码、验证想法或计算数据时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 代码"
                    }
                },
                "required": ["code"]
            }
        }
    }
]

TOOLS_MAP = {
    "execute_python": execute_python,
}