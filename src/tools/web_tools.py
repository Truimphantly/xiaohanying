"""
网页请求工具集
给 AI 提供 HTTP 请求、网页抓取等能力
"""
import json
import requests


def fetch_url(url: str) -> str:
    """
    获取网页内容

    参数:
        url: 网页地址

    返回:
        网页的文本内容（简化版）
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        # 返回基本信息
        content = response.text

        # 简单截取，避免返回过长
        if len(content) > 5000:
            content = content[:5000] + f"\n\n... (内容过长，已截断，原始长度 {len(content)} 字符)"

        result = {
            "status_code": response.status_code,
            "url": url,
            "content_type": response.headers.get("content-type", "unknown"),
            "content": content
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"请求失败: {str(e)}"}, ensure_ascii=False)


def api_get(url: str) -> str:
    """
    发送 GET 请求到 API 接口

    参数:
        url: API 地址

    返回:
        JSON 格式的响应
    """
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        # 尝试解析 JSON
        try:
            data = response.json()
        except json.JSONDecodeError:
            data = response.text

        return json.dumps({
            "status_code": response.status_code,
            "data": data
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"请求失败: {str(e)}"}, ensure_ascii=False)


TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "获取网页的文本内容。当需要查看某个网页或抓取网页信息时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要获取的网页地址"
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "api_get",
            "description": "向 API 接口发送 GET 请求并获取 JSON 响应。当需要调用 API 时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "API 接口地址"
                    }
                },
                "required": ["url"]
            }
        }
    }
]

TOOLS_MAP = {
    "fetch_url": fetch_url,
    "api_get": api_get,
}