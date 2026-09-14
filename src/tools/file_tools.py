"""
文件操作工具集
给 AI 提供读写文件、列出目录等能力
"""
import os
import json
from typing import Any

import src.file_journal as journal


def list_files(path: str = ".") -> str:
    """
    列出目录下的所有文件和文件夹

    参数:
        path: 目录路径，默认为当前目录

    返回:
        JSON 格式的文件列表
    """
    if not os.path.exists(path):
        return json.dumps({"error": f"路径不存在: {path}"}, ensure_ascii=False)

    if not os.path.isdir(path):
        return json.dumps({"error": f"不是目录: {path}"}, ensure_ascii=False)

    items = []
    for item in os.listdir(path):
        item_path = os.path.join(path, item)
        items.append({
            "name": item,
            "type": "dir" if os.path.isdir(item_path) else "file",
            "size": os.path.getsize(item_path) if os.path.isfile(item_path) else None
        })

    return json.dumps({"path": path, "items": items}, ensure_ascii=False, indent=2)


def read_file(file_path: str) -> str:
    """
    读取文件内容

    参数:
        file_path: 文件路径

    返回:
        文件内容
    """
    if not os.path.exists(file_path):
        return json.dumps({"error": f"文件不存在: {file_path}"}, ensure_ascii=False)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 限制返回长度，防止超长文件撑爆 AI 上下文
        if len(content) > 10000:
            content = content[:10000] + f"\n\n... (文件过长，已截断，共 {len(content)} 字符)"

        return content
    except Exception as e:
        return json.dumps({"error": f"读取失败: {str(e)}"}, ensure_ascii=False)


def write_file(file_path: str, content: str) -> str:
    """
    写入文件（会覆盖已有内容）

    参数:
        file_path: 文件路径
        content: 要写入的内容

    返回:
        操作结果
    """
    try:
        # 写入前备份到台账：任务中止时回滚，不留半成品修改
        journal.record(file_path)

        # 确保目录存在
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return json.dumps({
            "success": True,
            "path": file_path,
            "size": len(content)
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"写入失败: {str(e)}"}, ensure_ascii=False)


# 工具定义：告诉 AI 每个工具的名字、功能、参数
# 这个格式是 OpenAI Function Calling 标准格式
TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出指定目录下的所有文件和文件夹。当你需要查看某个目录里有什么文件时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要查看的目录路径，默认为当前目录"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定文件的内容。当你需要查看文件里面写了什么时使用。注意：文件很大时返回内容会在消息内截断并注明，那只代表本条消息变短了，磁盘上的文件完整无损，不要为了'看全'而反复读取同一个文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要读取的文件路径"
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "将内容写入文件（会覆盖已有内容）。当需要创建或修改文件时使用。注意：返回 success 即代表文件已完整保存，不要读回整个文件验证；单次 content 建议控制在 8000 字以内，过长建议拆成多个文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要写入的文件路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的内容"
                    }
                },
                "required": ["file_path", "content"]
            }
        }
    }
]

# 工具执行映射：AI 说要调用哪个工具，就执行对应的函数
TOOLS_MAP = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
}