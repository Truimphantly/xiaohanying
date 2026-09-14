"""
工作流引擎测试 — 让 AI 自动查看文件并操作
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.workflow_engine import WorkflowEngine

print("=" * 60)
print("测试：让 AI 查看当前目录下有什么文件")
print("=" * 60)

engine = WorkflowEngine(verbose=True)
result = engine.run("帮我看看当前目录（d:/ai工作流）下有什么文件，然后告诉我一共有几个文件、几个文件夹")

print("\n" + "=" * 60)
print("测试完成！")
print("=" * 60)