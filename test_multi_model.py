"""验证多模型讨论的第一步：两个模型能在同一个进程里互不干扰地轮流说话。

跑法（在项目根目录）:
    python test_multi_model.py

验证三件事：
    1. config.get_model(key) 按 key 取配置，不依赖「当前激活模型」这个全局
    2. llm_client.call_model / ask 可以交错调用不同厂商的模型，
       并且能把 A 说过的话原样喂给 B（讨论引擎的核心机制）
    3. 全程跑完，全局激活模型没被动过 —— 现有单模型流程不受影响

顺带把每次调用的 token 用量打出来：讨论模式是 N 个模型 × M 轮，
这个数字乘起来就是「一次讨论要花多少钱」的底数。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import src.config as config
from src.llm_client import ask, call_model, extract_text, EmptyReply

TOPIC = "给一个本地 AI 助手加「多模型讨论」功能，最该先解决的一个问题是什么？"

# 发言长度上限。
# 注意别贪小：sensenova 挂的是推理模型，额度先喂给 reasoning，给小了正文是空的。
MAX_TOKENS = 1500

# (模型 key, 这个参与者的角色设定)
# 刻意给两个模型强对立的立场：不同厂商的模型互怼才有价值，
# 如果都在说「我同意」，说明角色提示词没起作用，得改。
PARTICIPANTS = [
    ("deepseek", "你是务实的工程师，只讲最要紧的一点，40 字以内，不要客套话。"),
    ("sensenova", "你是爱挑刺的评审。必须针对对方刚才那句话，指出一个具体的漏洞或风险，"
                  "禁止说「同意」「有道理」这类附和的话。40 字以内。"),
]


def usage_of(response) -> str:
    """把一次调用的 token 用量压成一行（含推理 token，那部分最容易被忽略）"""
    u = getattr(response, "usage", None)
    if u is None:
        return "用量未知"
    details = getattr(u, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details else None
    s = f"输入 {u.prompt_tokens} / 输出 {u.completion_tokens}"
    if reasoning:
        s += f"（其中推理 {reasoning}）"
    return s


before = config.get_active_model_key()
print(f"全局激活模型（调用前）= {before}\n")

# ---------- 第 1 轮：各自独立表态 ----------
print("=" * 60)
print(f"议题：{TOPIC}")
print("=" * 60)

speeches = []
for key, system in PARTICIPANTS:
    model = config.get_model(key)          # 按 key 取，不碰全局
    t0 = time.time()
    try:
        response = call_model(
            key,
            [{"role": "system", "content": system},
             {"role": "user", "content": TOPIC}],
            max_tokens=MAX_TOKENS,
            temperature=0.7,
        )
        reply = extract_text(response).strip()
    except EmptyReply as e:
        print(f"\n[FAIL] {key} ({model['name']}): {e}")
        continue
    except Exception as e:
        print(f"\n[FAIL] {key} ({model['name']}): {type(e).__name__}: {e}")
        continue
    speeches.append((key, model["name"], reply))
    print(f"\n--- {model['name']} [{key}]  实际模型 {model['model_id']}  用时 {time.time() - t0:.1f}s")
    print(f"    token: {usage_of(response)}")
    print(f"    {reply}")

# ---------- 第 2 轮：让评审看到对方原话后再发言 ----------
# 讨论引擎要做的事，本质就是「手工拼消息列表」：
# 把别人的发言当作 user 消息塞进去，模型分不出这是人说的还是另一个模型说的。
if len(speeches) >= 2:
    (first_key, first_name, first_reply), (second_key, second_name, _) = speeches[:2]
    print("\n" + "=" * 60)
    print(f"第 2 轮：把 {first_name} 的原话交给 {second_name}，请它挑刺")
    print("=" * 60)

    messages = [
        {"role": "system", "content": PARTICIPANTS[1][1]},
        {"role": "user", "content": f"议题：{TOPIC}"},
        {"role": "user", "content": f"{first_name} 的观点是：「{first_reply}」"},
    ]
    try:
        response = call_model(second_key, messages, max_tokens=MAX_TOKENS, temperature=0.7)
        critique = extract_text(response).strip()
        print(f"\n--- {second_name} [{second_key}]")
        print(f"    token: {usage_of(response)}")
        print(f"    {critique}")
    except EmptyReply as e:
        print(f"\n[FAIL] {second_name}: {e}")
    except Exception as e:
        print(f"\n[FAIL] {second_name}: {type(e).__name__}: {e}")

# ---------- 收尾：确认全局状态没被动过 ----------
after = config.get_active_model_key()
print("\n" + "=" * 60)
print(f"全局激活模型（调用后）= {after}")
if after == before:
    print("✓ 未被改动：讨论用的调用链路和现有单模型流程完全隔离")
else:
    print(f"✗ 被改动了（{before} → {after}）：有代码在偷偷写全局状态，需要排查")
