"""
通过 Web UI API 生成贪吃蛇游戏
"""
import urllib.request
import urllib.parse
import json

task = (
    "帮我写一个贪吃蛇大作战网页游戏，保存到 d:/ai工作流/output/snake.html。"
    "要求：1. 深色科技风主题 2. 用键盘方向键控制蛇 3. 有分数显示 4. 吃到食物蛇变长 "
    "5. 撞墙或撞到自己游戏结束 6. 画布至少 600x600 7. 纯 HTML 单文件，内嵌所有 CSS 和 JS "
    "8. 游戏结束后显示分数和重新开始按钮"
)

url = "http://localhost:5000/api/chat?message=" + urllib.parse.quote(task)
print("正在调用 AI 工作流...")
print(f"任务: {task[:80]}...")
print()

try:
    response = urllib.request.urlopen(url, timeout=120)

    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data: "):
            continue

        data = json.loads(line[6:])
        t = data["type"]

        if t == "thinking":
            print(f"[步骤{data['step']}] 🤔 AI 思考中...")
        elif t == "tool_call":
            print(f"[步骤{data['step']}] 🔧 调用: {data['name']}")
            args_str = json.dumps(data["args"], ensure_ascii=False)
            if len(args_str) > 100:
                args_str = args_str[:100] + "..."
            print(f"          参数: {args_str}")
        elif t == "tool_result":
            print(f"[步骤{data['step']}] ✅ 结果: {data['result'][:120]}")
        elif t == "complete":
            print(f"\n✅ 任务完成!")
            print(f"AI 回复: {data['result'][:300]}...")
        elif t == "error":
            print(f"\n❌ 错误: {data['message']}")

except Exception as e:
    print(f"请求失败: {e}")