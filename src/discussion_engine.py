"""
多模型讨论引擎 — 让几个不同厂商的模型互相挑错，吵出一个结论

和 WorkflowEngine 的分工：
    DiscussionEngine  只有脑子，没有手 —— 全程纯文字，不调用任何工具
    WorkflowEngine    只有手，脑子是单个模型 —— 拿讨论的结论去调工具干活

这样切的原因：file_journal 是进程级单例（两个任务同时跑会互相清空回滚记录），
main.py 也用 _register_task() 强制同一时刻只有一个任务。让多个模型各自调工具，
就得先把这套台账改成按任务实例隔离，工程量大一个量级。而讨论阶段不碰文件，
天然绕开了这个冲突；执行阶段直接复用已经打磨过的那套引擎，一行都不用改。

流程（rounds=3 时）：
    第 1 轮 · 提议    各人独立发言，互相看不到 —— 避免被第一个开口的锚定
    第 2 轮 · 交锋    看到完整讨论记录后回应
    第 3 轮 · 交锋    同上
    收敛             总结者综合出最终结论

事件格式和 WorkflowEngine 保持同构（前端复用同一套渲染），
额外的 speaker / phase / round 字段用来区分是谁在发言。
"""

import queue
import threading
import time

import src.config as config
from src.llm_client import call_model
from src.prompts import DISCUSSION_ROLES, DEFAULT_DISCUSSION

# API 失败重试（和 WorkflowEngine 保持一致的节奏）
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.8, 1.6)

# 正文长度上限的提示。max_tokens 管的是 reasoning + 正文的总量，
# 正文本身该多长要靠提示词约束——否则推理模型会把额度花在长篇大论上。
SPEECH_HINT = "发言控制在 300 字以内，直接说观点，不要复述别人说过的话。"


# 用户插话在讨论记录里的身份。
# 讨论中途用户可能想说「别考虑性能了，先把功能做出来」——所有后续发言者
# 都得看得到这句话，否则它们会继续照着旧前提吵下去。
USER_ROLE = {
    "role_key": "user",
    "name": "用户",
    "avatar": "👤",
    "color": "#60a5fa",
    "model_id": "",
}


class DiscussionCancelled(Exception):
    """讨论被用户取消（引擎内部信号）"""


def _interruptible_sleep(seconds, cancel_event) -> bool:
    """分段睡眠，期间用户取消则提前返回。返回 True 表示已被取消"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return True
        time.sleep(0.1)
    return False


class DiscussionEngine:
    """
    多模型讨论引擎

    使用示例:
        engine = DiscussionEngine()
        for event in engine.run_stream("给助手加多模型讨论功能，先做什么？"):
            print(event)
    """

    def __init__(self, participants: list = None, moderator: str = None,
                 rounds: int = None, max_tokens: int = None,
                 context: str = None, verbose: bool = False):
        """
        参数:
            participants: 参与讨论的角 key 列表（见 prompts.DISCUSSION_ROLES），
                默认 ["engineer", "critic"]
            moderator: 收敛阶段做总结的角色 key，默认 "moderator"
            rounds: 讨论轮数（含第 1 轮提议），默认 3
            max_tokens: 单次发言的 token 上限（reasoning + 正文），默认 4000
            context: **项目背景**，会附在每个参与者每一轮的消息里。
                讨论引擎没有工具，模型无从得知项目长什么样，不给背景它就会
                凭空想象一个通用项目——实测中它会讨论「core/discuss.py」和
                「本地 7B 模型单卡加载」，而本项目是 src/ + API 调用，
                吵得再认真，结论也用不上。所以背景必须由调用方喂进来。
        """
        cfg = DEFAULT_DISCUSSION
        self.participants = [self._make_role(k) for k in (participants or cfg["participants"])]
        self.moderator = self._make_role(moderator or cfg["moderator"])
        self.rounds = max(1, int(rounds or cfg["rounds"]))
        self.max_tokens = int(max_tokens or cfg["max_tokens"])
        self.context = (context or "").strip()
        self.verbose = verbose

    @staticmethod
    def _make_role(role_key: str) -> dict:
        """把一个角色 key 展开成完整的参与者信息，顺带提前校验模型配置"""
        role = DISCUSSION_ROLES.get(role_key)
        if role is None:
            raise ValueError(
                f"未知讨论角色: {role_key}（可用: {'、'.join(DISCUSSION_ROLES)}）"
            )
        # 角色不再写死模型：按 .env 动态解析该角色用哪个模型 key，
        # 然后提前取一次模型配置——key 配错了在这里就报错，而不是讨论跑一半才炸
        model_key = config.get_discussion_role_model(role_key)
        model = config.get_model(model_key)
        return {**role, "role_key": role_key, "model": model_key, "model_id": model["model_id"]}

    @staticmethod
    def _public(role: dict) -> dict:
        """给前端的角色信息（不含提示词）"""
        return {
            "key": role["role_key"],
            "name": role["name"],
            "avatar": role["avatar"],
            "color": role["color"],
            "model": role["model"],
            "model_id": role["model_id"],
        }

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run_stream(self, task: str, cancel_event: threading.Event = None,
                   pause_event: threading.Event = None, inbox=None):
        """
        跑完一整场讨论，逐个 yield 事件

        参数:
            task: 讨论的议题（也可以直接是一个任务描述，讨论完由调用方交给执行引擎）
            cancel_event: 可选，用户点「停止」时 set() 它
            pause_event: 可选，用户点「暂停」时 set()、继续时 clear()。
                闸门设在**发言者之间**，不会打断正在生成的那一轮（见 _pause_gate）
            inbox: 可选，queue.Queue。用户在暂停期间打的字塞进这里，
                恢复讨论时会被收进讨论记录，后续所有发言者都能看到

        事件类型:
            {"type": "start", "total_rounds": 3, "participants": [...], "moderator": {...}}
            {"type": "round", "round": 1, "phase": "提议", "label": "第 1 轮 · 提议"}
            {"type": "speaker_start", "round": 1, "phase": "提议",
             "speaker": "engineer", "speaker_name": "务实工程师",
             "avatar": "🔧", "color": "#4a9eff"}
            {"type": "reasoning_delta", "round": 1, "speaker": "engineer",
             "content": "推理过程的增量文本"}      ← 推理模型的思考过程，逐字到达
            {"type": "text_delta", "round": 1, "speaker": "engineer",
             "content": "正式发言的增量文本"}      ← 打字机
            {"type": "speaker_end", "round": 1, "speaker": "engineer"}
            {"type": "paused", "round": 1}         ← 已挂起，等用户点「继续」
            {"type": "resumed", "round": 1}
            {"type": "user_said", "round": 1, "text": "用户插的话"}
            {"type": "notice", "message": "..."}
            {"type": "complete", "result": "最终结论"}
            {"type": "error", "message": "..."}
        """
        transcript = []   # 讨论记录，后续发言者都基于它思考

        yield {
            "type": "start",
            "total_rounds": self.rounds,
            "participants": [self._public(p) for p in self.participants],
            "moderator": self._public(self.moderator),
        }

        try:
            # 第 1 轮：提议。此时 transcript 还是空的，所以谁也没见过谁的发言
            yield from self._run_phase(task, transcript, round_no=1, phase="提议",
                                       cancel_event=cancel_event,
                                       pause_event=pause_event, inbox=inbox)

            # 第 2..rounds 轮：交锋
            for r in range(2, self.rounds + 1):
                yield from self._run_phase(task, transcript, round_no=r, phase="交锋",
                                           cancel_event=cancel_event,
                                           pause_event=pause_event, inbox=inbox)

            # 收敛前再过一道闸门：用户可能刚好在这时候插话，
            # 那句插话必须进得了总结者的输入，否则等于没说
            yield from self._pause_gate(pause_event, cancel_event, inbox,
                                        transcript, self.rounds)

            # 收敛：总结者读完整场记录，出一份结论
            result = yield from self._run_conclusion(task, transcript, cancel_event)
            if result is None:
                yield {"type": "error", "message": "⏹ 讨论已由用户停止。"}
                return
            yield {"type": "complete", "result": result}

        except DiscussionCancelled:
            yield {"type": "error", "message": "⏹ 讨论已由用户停止。"}

        except Exception as e:
            # 兜底：讨论不碰文件，所以不需要回滚，报错即可
            yield {"type": "error", "message": f"❌ 讨论中断：{type(e).__name__}: {str(e)[:300]}"}

    # ------------------------------------------------------------------
    # 一个阶段 = 所有参与者各发言一次
    # ------------------------------------------------------------------
    def _run_phase(self, task, transcript, round_no, phase, cancel_event,
                   pause_event=None, inbox=None):
        yield {"type": "round", "round": round_no, "phase": phase,
               "label": f"第 {round_no} 轮 · {phase}"}

        for role in self.participants:
            if self._cancelled(cancel_event):
                raise DiscussionCancelled()

            # 每个发言者开口之前过一道闸门：暂停在这里生效，用户的插话在这里入记录
            yield from self._pause_gate(pause_event, cancel_event, inbox,
                                        transcript, round_no)

            yield self._speaker_start_event(role, round_no, phase)

            messages = self._build_messages(role, task, transcript, round_no, phase)
            text, reasoning = yield from self._stream_one(
                role, messages, round_no, phase, cancel_event
            )

            yield {"type": "speaker_end", "round": round_no, "speaker": role["role_key"]}

            # 只有真说出东西了才记进记录：空发言记进去，后面的人会对着空气回应
            if text:
                transcript.append({
                    "round": round_no,
                    "phase": phase,
                    "role": role,
                    "text": text,
                    "reasoning": reasoning,
                })

    def _run_conclusion(self, task, transcript, cancel_event):
        """收敛阶段。返回结论文本；被取消时返回 None"""
        if self._cancelled(cancel_event):
            raise DiscussionCancelled()

        yield {"type": "round", "round": self.rounds + 1, "phase": "收敛",
               "label": "收敛 · 出结论"}
        yield self._speaker_start_event(self.moderator, self.rounds + 1, "收敛")

        if not transcript:
            # 整场讨论没产出任何有效发言（多半是 API 全线失败），
            # 让总结者对着一片空白编结论是最坏的选择，直接说清楚
            yield {"type": "notice", "message": "整场讨论没有产生任何有效发言，跳过总结。"}
            return "（本次讨论没有产生有效发言，可能是 API 调用失败，请重试。）"

        blocks = []
        if self.context:
            blocks.append(f"【项目背景】\n{self.context}")
        blocks.append(f"【讨论议题】\n{task}")
        blocks.append("【完整讨论记录】\n" + self._format_transcript(transcript))
        blocks.append("现在请给出最终结论。结论里的方案必须落在【项目背景】描述的真实结构上。")

        messages = [
            {"role": "system", "content": self.moderator["prompt"]},
            {"role": "user", "content": "\n\n".join(blocks)},
        ]
        text, _ = yield from self._stream_one(
            self.moderator, messages, self.rounds + 1, "收敛", cancel_event
        )
        yield {"type": "speaker_end", "round": self.rounds + 1,
               "speaker": self.moderator["role_key"]}
        return text or "（总结者没有产出结论，请重试。）"

    # ------------------------------------------------------------------
    # 单个模型的流式回合
    # ------------------------------------------------------------------
    def _stream_one(self, role, messages, round_no, phase, cancel_event):
        """
        流式跑完一个发言者的回合。

        用 yield from 的返回值把 (正文, 推理过程) 交给调用方 ——
        生成器没法既 yield 又 return 值，但 yield from 能拿到子生成器的 return。
        """
        text = ""
        reasoning = ""
        last_error = ""
        budget = self.max_tokens

        for attempt in range(RETRY_ATTEMPTS):
            text = ""
            reasoning = ""
            stream = None
            try:
                stream = call_model(role["model"], messages, stream=True,
                                    max_tokens=budget)
                try:
                    for chunk in stream:
                        if self._cancelled(cancel_event):
                            raise DiscussionCancelled()
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if delta is None:
                            continue
                        # 推理过程：字段名各家不一样（reasoning / reasoning_content），
                        # 两个都试。这一段占了输出的大头，扔掉太可惜 ——
                        # sensenova 实测一次发言 3880 字的推理只配 48 字的正文，
                        # 十几秒里用户能看到的就靠它，不然界面像卡死了。
                        piece = (getattr(delta, "reasoning", None)
                                 or getattr(delta, "reasoning_content", None))
                        if piece:
                            reasoning += piece
                            yield {"type": "reasoning_delta", "round": round_no,
                                   "phase": phase, "speaker": role["role_key"],
                                   "content": piece}
                        if delta.content:
                            text += delta.content
                            yield {"type": "text_delta", "round": round_no,
                                   "phase": phase, "speaker": role["role_key"],
                                   "content": delta.content}
                finally:
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass

                if text.strip():
                    return text.strip(), reasoning

                # 流正常结束了，但一个字正文都没有 —— 推理模型把额度全烧在
                # reasoning 上了（实测 reasoning 长度 458~3880 字浮动，无法预测）。
                # 这种失败是随机的，加倍额度重试一次通常就好了。
                if attempt < RETRY_ATTEMPTS - 1:
                    budget *= 2
                    escalated = True
                    yield {"type": "notice", "message": (
                        f"{role['name']} 这一轮只产出了推理没有正文（额度被推理吃光），"
                        f"已把上限提到 {budget} 重试。"
                    )}
                    continue
                break

            except DiscussionCancelled:
                raise

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt >= RETRY_ATTEMPTS - 1:
                    break
                wait = RETRY_BACKOFF_SECONDS[attempt]
                yield {"type": "notice", "message": (
                    f"{role['name']} 调用失败（{type(e).__name__}），"
                    f"{wait:.1f} 秒后自动重试（第 {attempt + 1}/{RETRY_ATTEMPTS - 1} 次）："
                    f"{last_error[:150]}"
                )}
                if _interruptible_sleep(wait, cancel_event):
                    raise DiscussionCancelled()

        # 重试也没救回来：这一轮按弃权处理，让讨论继续
        # （一个人掉线不该让整场讨论作废，其他人还在等）
        reason = f"最后错误：{last_error[:150]}" if last_error else "只产出了推理，没有正文"
        hint = ""
        if escalated:
            # 已经翻倍重试过还是空 —— 说明这个角色的推理量远超预期，
            # 与其让用户对着「弃权」发呆，不如直接告诉他怎么调。
            hint = (f"（该角色推理量偏大，建议把讨论额度调高到 {budget * 2} 以上，"
                    "或在 .env 里设 DISCUSSION_MAX_TOKENS）")
        yield {"type": "notice", "message": (
            f"{role['name']} 这一轮弃权（{reason}）。讨论继续，"
            f"但后面的发言者看不到这一轮的观点。{hint}"
        )}
        return "", reasoning

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _pause_gate(self, pause_event, cancel_event, inbox, transcript, round_no):
        """发言者之间的闸门：暂停时挂起，恢复时把用户的插话收进讨论记录。

        闸门刻意只设在发言者之间，不打断正在生成的那一轮 —— 硬掐流式生成
        既浪费已经烧掉的 token，讨论记录里还会留半句话给后面的人看。
        所以点「暂停」最多再等一个发言者说完。
        """
        if pause_event is not None and pause_event.is_set():
            yield {"type": "paused", "round": round_no}
            while pause_event.is_set():
                if self._cancelled(cancel_event):
                    raise DiscussionCancelled()
                time.sleep(0.15)
            yield {"type": "resumed", "round": round_no}

        if inbox is None:
            return
        # 收编用户攒下的插话。放在暂停解除之后：用户多半是「暂停 → 打字 → 继续」，
        # 这条插话要成为下一个发言者的输入
        while True:
            try:
                text = str(inbox.get_nowait() or "").strip()
            except queue.Empty:
                break
            if not text:
                continue
            transcript.append({
                "round": round_no, "phase": "用户插话",
                "role": USER_ROLE, "text": text, "reasoning": "",
            })
            yield {"type": "user_said", "round": round_no, "text": text}

    @staticmethod
    def _cancelled(cancel_event) -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _speaker_start_event(self, role, round_no, phase) -> dict:
        return {
            "type": "speaker_start",
            "round": round_no,
            "phase": phase,
            "speaker": role["role_key"],
            "speaker_name": role["name"],
            "avatar": role["avatar"],
            "color": role["color"],
            "model_id": role["model_id"],
        }

    def _build_messages(self, role, task, transcript, round_no, phase) -> list:
        """给某个参与者拼这一轮的消息。

        刻意不把讨论记录做成 assistant 消息塞进对话历史：那样模型会分不清
        哪句是自己说的、哪句是别人说的，而且连续两条 assistant 消息
        在部分接口上直接报格式错误。统一塞进一条 user 消息最稳。
        """
        blocks = []
        if self.context:
            blocks.append(f"【项目背景】\n{self.context}")
        blocks.append(f"【讨论议题】\n{task}")

        if transcript:
            blocks.append("【目前为止的讨论记录】\n" + self._format_transcript(transcript))
            instruction = (
                f"现在轮到你发言。针对上面记录里**具体的**观点表态——"
                f"哪条同意、哪条反对、为什么。{SPEECH_HINT}"
            )
        else:
            instruction = (
                f"你是第一个发言的，别人还没开口，所以不用回应谁。"
                f"直接给出你的方案。{SPEECH_HINT}"
            )

        if self.context:
            instruction += "注意：方案必须基于上面【项目背景】里真实存在的文件和技术栈，不要假设项目里有别的东西。"

        blocks.append(instruction)
        return [
            {"role": "system", "content": role["prompt"]},
            {"role": "user", "content": "\n\n".join(blocks)},
        ]

    @staticmethod
    def _format_transcript(transcript) -> str:
        """把讨论记录排成人能读、模型也好认的格式"""
        lines = []
        last_round = None
        for item in transcript:
            if item["round"] != last_round:
                last_round = item["round"]
                lines.append(f"\n— 第 {item['round']} 轮 · {item['phase']} —")
            role = item["role"]
            if role.get("role_key") == "user":
                # 用户插话单独标出来：让模型明确知道这是「需求方的临时改口」，
                # 而不是某个参与者的观点，两者权重完全不同
                lines.append(f"▸ 【用户插话】{item['text']}")
            elif role.get("model_id"):
                lines.append(f"▸ {role['name']}（{role['model_id']}）：\n{item['text']}")
            else:
                lines.append(f"▸ {role['name']}：\n{item['text']}")
        return "\n".join(lines).strip()


# 便捷函数
def discuss(task: str, context: str = None, rounds: int = None,
            verbose: bool = True) -> str:
    """快速跑一场讨论，返回最终结论"""
    engine = DiscussionEngine(context=context, rounds=rounds, verbose=verbose)
    final = ""
    for event in engine.run_stream(task):
        if not verbose:
            continue
        t = event["type"]
        if t == "round":
            print(f"\n{'='*50}\n{event['label']}\n{'='*50}")
        elif t == "speaker_start":
            print(f"\n--- {event['avatar']} {event['speaker_name']} [{event['model_id']}]")
        elif t == "text_delta":
            print(event["content"], end="", flush=True)
        elif t == "reasoning_delta":
            pass  # 推理过程默认不打印，太长了
        elif t == "notice":
            print(f"\n[提示] {event['message']}")
        elif t == "complete":
            final = event["result"]
        elif t == "error":
            print(f"\n{event['message']}")
            final = event["message"]
    return final
