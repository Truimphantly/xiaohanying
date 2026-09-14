"""
Hello World 测试 — 验证 DeepSeek API 连接
"""
import sys
import os

# 把项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import config
from src.llm_client import LLMClient

print("=== 验证配置 ===")
if not config.validate():
    sys.exit(1)
print("配置 OK！")

print("\n=== 流式对话测试 ===")
print("你: 用一句话介绍你自己")
print("AI: ", end="", flush=True)

client = LLMClient()
for chunk in client.chat_stream("用一句话介绍你自己"):
    print(chunk, end="", flush=True)

print("\n")
print("=== 测试通过！DeepSeek API 连接正常 ===")