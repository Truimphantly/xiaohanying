"""命令行跑一场多模型讨论 —— 不开浏览器也能调提示词、看效果。

用法（项目根目录）:
    python test_discussion.py                          # 用默认议题
    python test_discussion.py "议题"                    # 指定议题
    python test_discussion.py "议题" 2                  # 指定轮数
    python test_discussion.py "议题" 2 engineer,critic  # 指定参与者

默认不打印推理过程（sensenova 一次发言能喷几千字推理，刷屏），
想看的话把下面 SHOW_REASONING 改成 True。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.discussion_engine import DiscussionEngine

SHOW_REASONING = False


def load_context() -> str:
    """读 PROJECT_CONTEXT.md 当讨论背景。

    这条路径和 Web 端不完全一样（Web 端还会附一份实时目录快照，
    见 main.build_workspace_context），但足够命令行调提示词用了。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PROJECT_CONTEXT.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def main():
    args = sys.argv[1:]
    task = args[0] if args else "给这个 AI 助手加「多模型讨论」功能，第一版应该做成什么样？"
    rounds = int(args[1]) if len(args) > 1 else 3
    participants = [p.strip() for p in args[2].split(",")] if len(args) > 2 else None

    engine = DiscussionEngine(rounds=rounds, participants=participants,
                              context=load_context())
    final = ""
    for event in engine.run_stream(task):
        t = event["type"]
        if t == "round":
            print(f"\n{'=' * 60}\n  {event['label']}\n{'=' * 60}")
        elif t == "speaker_start":
            print(f"\n--- {event['avatar']} {event['speaker_name']} [{event['model_id']}]")
        elif t == "text_delta":
            print(event["content"], end="", flush=True)
        elif t == "reasoning_delta" and SHOW_REASONING:
            print(event["content"], end="", flush=True)
        elif t == "notice":
            print(f"\n  [提示] {event['message']}")
        elif t == "complete":
            final = event["result"]
        elif t == "error":
            print(f"\n  [错误] {event['message']}")
            final = event["message"]

    print(f"\n\n{'=' * 60}\n最终结论：\n{'=' * 60}\n{final}")


if __name__ == "__main__":
    main()
