"""
实战案例 1：让 AI 自动生成一个 HTML 网页
"""
import sys
import os
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.workflow_engine import WorkflowEngine

print("=" * 60)
print("实战案例：让 AI 写一个 HTML 网页")
print("=" * 60)

task = """
请帮我写一个漂亮的 HTML 网页，保存到 d:/ai工作流/output/demo.html。

要求：
1. 页面主题是"我的 AI 工作流"，深色科技风背景
2. 包含标题、一段介绍文字、一个功能列表（列出我们项目已有的工具：文件操作、网页请求、代码执行）
3. 使用 CSS 美化，要有渐变背景、卡片式布局、动效
4. 可以内嵌 CSS 和 JS，不需要外部文件
5. 页面底部加一个动态时钟
"""

engine = WorkflowEngine(verbose=True)
result = engine.run(task)

print("\n" + "=" * 60)
print("AI 任务完成！")
print("=" * 60)

# 检查文件是否生成
output_path = r"d:\ai工作流\output\demo.html"
if os.path.exists(output_path):
    size = os.path.getsize(output_path)
    print(f"\n网页已生成: {output_path}")
    print(f"文件大小: {size} 字节")
    print("\n正在用浏览器打开网页...")
    webbrowser.open(f"file:///{output_path.replace(chr(92), '/')}")
else:
    print("\n网页生成失败，请查看上方日志")