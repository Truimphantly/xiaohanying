"""
工作流引擎 — 项目的核心
实现「思考 → 行动 → 观察 → 再思考」的 AI Agent 循环

通俗理解：
    你把任务交给引擎，引擎不断和 DeepSeek 对话，
    DeepSeek 说"我需要调用工具"，引擎就去执行工具，
    把结果告诉 DeepSeek，DeepSeek 再决定下一步，
    直到任务完成。

本轮升级（相对早期版本）：
    1. LLM 调用全程流式（stream=True），模型回复逐字通过 text_delta 事件发给界面
    2. 支持取消：传入 cancel_event 后尽快中止；无论以何种方式中止/断连，
       都会调用文件台账回滚，不留半成品（已完成任务不受影响）
    3. API 失败自动指数退避重试（最多 3 次），并通过 notice 事件告知界面
    4. 上下文预算：工具结果回传模型前截断、消息超长时整轮裁剪，防止长任务爆上下文
    5. 系统提示词从 src/prompts.py 按 persona 读取，不再在此处复制
"""

import json
import os
import re
import time
import uuid
import threading

from openai import OpenAI
import src.config as config
import src.file_journal as journal
from src.prompts import PERSONAS, DEFAULT_PERSONA
from src.tools.file_tools import TOOLS_DEFINITION as FILE_TOOLS, TOOLS_MAP as FILE_MAP
from src.tools.web_tools import TOOLS_DEFINITION as WEB_TOOLS, TOOLS_MAP as WEB_MAP
from src.tools.code_tools import TOOLS_DEFINITION as CODE_TOOLS, TOOLS_MAP as CODE_MAP
from src.tools.blender_tools import TOOLS_DEFINITION as BLENDER_TOOLS, TOOLS_MAP as BLENDER_MAP

# 工作区根目录（src 的上一级），台账快照与回滚的范围
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ====== 上下文预算相关常量 ======
# 单个工具结果回传给模型的最大字符数。
# 展示层截断 300 字符后，完整结果仍然可能很长（如 read_file 读回 1 万字源码），
# 全量塞回对话会让上下文迅速膨胀、烧钱且逼近窗口上限，所以在这里统一再截一道。
TOOL_RESULT_CHARS = 4000
# 发给前端的工具结果展示长度
TOOL_RESULT_DISPLAY_CHARS = 300
# 对话消息总字符预算。超过后从最老的"轮次"开始整轮删除（见 _prune_messages）
MAX_CONTEXT_CHARS = 40000

# ====== API 重试 ======
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.8, 1.6)  # 第 1、2 次失败后的等待秒数（第 3 次失败直接报错）

# ====== 无进展护栏 ======
# 模型连续这么多轮只调工具、但没有任何写入、也不给最终答复时，判定为原地打转
# （典型诱因：把"消息截断"误当成文件损坏，反复重读验证），推送一条提示让它收尾
NO_PROGRESS_PUSH_AFTER = 12


class TaskCancelled(Exception):
    """任务被用户取消（引擎内部信号）"""


def _cap(text, limit):
    """把文本截断到 limit 字符，并注明原始长度。

    备注措辞刻意把一件事说清：被截断的只是「这条消息」，磁盘上的文件、
    真实的执行结果都是完整无损的。否则模型会把截断当成文件损坏，
    反复重读同一文件、分段打印来"追回"内容，陷入验证死循环。
    """
    text = str(text)
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n…（本条消息过长，已截断展示；磁盘上的文件/结果本身完整无损，原始 {len(text)} 字符，"
        "无需重新读取或分段打印被截断的部分）"
    )


def _interruptible_sleep(seconds, cancel_event) -> bool:
    """分段睡眠，期间用户取消则提前返回。返回 True 表示已被取消"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return True
        time.sleep(0.1)
    return False


def _prune_messages(messages, budget=MAX_CONTEXT_CHARS) -> None:
    """
    控制发送给模型的 messages 总量（就地裁剪）。

    策略：从最老的一"轮"开始整轮删除。一轮 = 一条 user 消息 + 紧随其后
    的所有 assistant / tool 消息（assistant 的工具调用与其 tool 结果必须
    成对存在，乱删单条会让 API 报错，所以必须整轮删）。

    注意：当前任务的 user 消息（本次任务的指令）永远不能删——单任务内部
    只有一条 user，工具轮次都挂在它后面，如果它被误删，模型会当场失忆，
    忘了任务也忘了自己的设定。所以只有"历史轮次"（其后还有别的 user）
    才允许整轮删除。

    如果只剩当前任务这一轮仍然超预算（单轮内工具链极长），则把每条 tool
    消息的内容进一步压缩，直到放得下为止。
    """
    def total_size(msgs):
        n = 0
        for m in msgs:
            content = m.get("content")
            n += len(content) if isinstance(content, str) else len(str(content or ""))
            tool_calls = m.get("tool_calls")
            if tool_calls:
                n += len(json.dumps(tool_calls, ensure_ascii=False))
        return n

    while len(messages) > 2 and total_size(messages) > budget:
        # 找最早的一条 user 消息作为一轮的起点（index 0 是 system，永远保留）
        start = None
        for i in range(1, len(messages)):
            if messages[i]["role"] == "user":
                start = i
                break
        if start is None:
            break  # 只剩 system 了，无轮可删（理论不会发生）
        # 找这条 user 之后的下一条 user：如果不存在，说明这条 user 就是
        # 当前任务的指令（后面的 assistant/tool 都是它的执行过程），
        # 整轮删除会连任务指令一起删掉，绝不能删，交给下方压缩兜底。
        nxt = None
        for i in range(start + 1, len(messages)):
            if messages[i]["role"] == "user":
                nxt = i
                break
        if nxt is None:
            break
        # 删除这一整轮（从该 user 起到下一条 user 前），保留当前任务轮
        del messages[start:nxt]

    # 兜底：把 tool 结果压短（此操作安全，不影响任何消息配对关系）
    if total_size(messages) > budget:
        for m in messages:
            if m["role"] == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 1200:
                m["content"] = m["content"][:1200] + "\n…（结果过长，已压缩）"


# ====== 工作区越界防护 ======
# 目标：文件操作默认不能出 D:\ai工作流；确有必要访问外面时，必须先经过
# 用户同意（Web 界面弹出允许/拒绝）。CLI 没有确认通道 → 一律自动拒绝。
# 注意：下面的代码检查只是"提示性"的（拦常见写法），不是沙箱——
# 真正的隔离需要系统级限制（受限令牌/容器）。execute_python 本身能做的
# 事远超路径检查的范围，所以它只做最要紧的两种拦法：写死盘符绝对路径、
# 用 ..\ 或 ../ 相对越界。二者任一命中就要求用户同意后再执行。
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^'\s\"`\),;\]]*")
# 同意/拒绝弹窗等待超时（秒）。超时按"拒绝"处理，不让任务干等
APPROVAL_TIMEOUT_SECONDS = 300


def _path_outside_workspace(path) -> bool:
    """判断路径是否落在工作区（WORKSPACE_ROOT）之外。

    只对像样的字符串路径做判断；空值/非字符串一律视为"在工作区内"，
    交给工具本身的报错逻辑处理，不让护栏抢戏。
    """
    if not isinstance(path, str) or not path.strip():
        return False
    p = os.path.normcase(os.path.abspath(path.strip().strip("\"' ")))
    root = os.path.normcase(WORKSPACE_ROOT)
    return not (p == root or p.startswith(root + os.sep))


def _code_looks_outside(code: str) -> bool:
    """启发式检查 execute_python 的代码是否可能读写工作区之外。

    命中以下任一情况返回 True（需要用户同意）：
      1) 代码字符串里出现盘符绝对路径，且解析后在工作区外；
      2) 出现 ..\ 或 ../ 这种相对路径越界写法。
    提醒：这只是写法级提示，不是沙箱。
    """
    code = str(code or "")
    for m in _ABS_PATH_RE.finditer(code):
        cand = m.group(0).rstrip(".,;:!?")
        if _path_outside_workspace(cand):
            return True
    if re.search(r"\.\.(?:\\|/)", code):
        return True
    return False



class WorkflowEngine:
    """
    AI 工作流引擎

    使用示例:
        engine = WorkflowEngine()
        result = engine.run("帮我看看当前目录下有什么文件")
        print(result)
    """

    def __init__(self, verbose: bool = True):
        """
        初始化引擎

        参数:
            verbose: run() 时是否打印每一步的详细信息（调试用）
        """
        self.client = OpenAI(
            api_key=config.get_active_model()["api_key"],
            base_url=config.get_active_model()["base_url"],
        )
        self.model = config.get_active_model()["model_id"]
        self.temperature = config.TEMPERATURE

        # 合并所有工具
        self.all_tools = FILE_TOOLS + WEB_TOOLS + CODE_TOOLS + BLENDER_TOOLS
        self.tools_map = {}
        self.tools_map.update(FILE_MAP)
        self.tools_map.update(WEB_MAP)
        self.tools_map.update(CODE_MAP)
        self.tools_map.update(BLENDER_MAP)

        self.verbose = verbose
        # 单轮任务最大步数（在 .env 用 AGENT_MAX_STEPS 调整，默认 50）
        self.max_steps = config.MAX_STEPS

    # ------------------------------------------------------------------
    # run(): CLI / 脚本用的简化入口，完整跑完一次任务
    # ------------------------------------------------------------------
    def run(self, task: str) -> str:
        """
        执行一个任务（同步），返回 AI 的最终回复

        参数:
            task: 用户的任务描述

        内部实现：驱动 run_stream() 的事件流，把每个事件还原成终端打印，
        最后一个 complete / error 事件的内容就是返回值。
        """
        if self.verbose:
            print("=" * 60)
            print("开始任务...")
            print("=" * 60)

        final = ""
        for event in self.run_stream(task):
            if not self.verbose:
                continue
            t = event["type"]
            if t == "thinking":
                print(f"\n{'='*50}")
                print(f"步骤 {event['step']}：AI 思考中...")
                print(f"{'='*50}")
            elif t == "text_delta":
                print(event["content"], end="", flush=True)
            elif t == "tool_call":
                print(f"\n→ 调用工具: {event['name']}")
                print(f"  参数: {json.dumps(event['args'], ensure_ascii=False)}")
            elif t == "tool_result":
                print(f"  结果: {event['result']}")
            elif t == "notice":
                print(f"\n[提示] {event['message']}")
            elif t == "complete":
                final = event["result"]
            elif t == "error":
                print(f"\n任务中止：{event['message']}")
                final = f"任务中止：{event['message']}"
        return final

    # ------------------------------------------------------------------
    # run_stream(): 流式执行任务，每步通过 yield 返回事件（Web 界面使用）
    # ------------------------------------------------------------------
    def run_stream(self, task_content, history: list = None,
                   cancel_event: threading.Event = None,
                   persona_key: str = None,
                   approval_fn=None):
        """
        流式执行任务，每步通过 yield 返回事件

        参数:
            task_content: 用户的任务描述。可以是字符串，也可以是
                多模态消息片段列表（视觉模型传图片时使用），会原样作为 user 消息内容
            history: 之前的对话历史（用于连续对话），格式为
                [{"role": "user", "content": "..."}, ...]
            cancel_event: 可选。用户点「停止」时 set() 它，引擎会尽快中止任务并回滚
            persona_key: 角色预设名（见 src/prompts.py），默认 DEFAULT_PERSONA
            approval_fn: 可选，形如 fn(description, cancel_event, request_id) -> bool 的回调。
                当模型要访问工作区之外（write_file 越界路径 / execute_python
                疑似越界代码）时调用它来征求用户同意，返回 True 放行、False 拒绝。
                Web 界面传入阻塞式确认实现；CLI 不传 → 一律自动拒绝。
                注意：确认期间要自己留意 cancel_event，用户点「停止」时应尽快
                返回 False，引擎随后会按取消处理。等待超过 APPROVAL_TIMEOUT_SECONDS
                也算拒绝。

        事件类型:
            {"type": "start", "step": 0, "total_steps": -1}
            {"type": "thinking", "step": 1}
            {"type": "text_delta", "step": 1, "content": "模型回复的增量文本"}   ← 打字机流式
            {"type": "notice", "step": 1, "message": "API 重试等提示"}
            {"type": "tool_call", "step": 1, "name": "xxx", "args": {...}}
            {"type": "tool_result", "step": 1, "name": "xxx", "result": "..."}
            {"type": "approval_request", "step": 1, "id": "...", "tool": "...",
             "description": "...", "args_preview": "..."}   ← 越界操作征求同意，等待期间前端应显示允许/拒绝按钮
            {"type": "complete", "result": "..."}
            {"type": "error", "message": "..."}
        """
        persona = PERSONAS.get(persona_key) or PERSONAS[DEFAULT_PERSONA]

        # 消息列表：系统提示词（persona）→ 历史 → 当前任务
        messages = [{"role": "system", "content": persona["prompt"]}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": task_content})

        # 开始台账：任务中止时可把工作区回滚到任务前状态
        journal.begin(WORKSPACE_ROOT)
        yield {"type": "start", "step": 0, "total_steps": self.max_steps}

        step = 0
        consecutive_failures = 0  # 连续失败计数
        last_failed_action = ""   # 上次失败的操作
        rounds_no_write = 0       # 连续"只读无写入"的工具轮数（无进展护栏用）
        denied_paths = set()      # 本任务内已拒绝过的越界写入路径（不再重复弹窗）
        finished_ok = False       # 任务是否正常完成（commit 过）——决定断连时是否回滚

        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        def abort_message(reason: str) -> str:
            """统一的「中止 + 已回滚」提示文案"""
            restored, deleted = journal.rollback()
            return (
                f"{reason}\n"
                f"已回滚本次任务的文件改动：恢复 {len(restored)} 个文件，"
                f"清理 {len(deleted)} 个未完成的新文件，工作区已回到任务开始前的状态。"
            )

        try:
            while step < self.max_steps:
                # —— 发送给模型之前：控制消息总量，防止长任务爆上下文 ——
                _prune_messages(messages)

                if cancelled():
                    yield {"type": "error", "message": abort_message("⏹ 任务已由用户停止。")}
                    return

                step += 1
                yield {"type": "thinking", "step": step}

                # ================= LLM 回合（流式 + 重试 + 可取消） =================
                text = ""            # 本回合模型的流式文本（最终回复或过程说明）
                last_api_error = ""
                stream = None
                api_success = False  # 本回合是否成功拿到完整回复
                tc_by_index = {}     # OpenAI 流式工具调用按 index 分片到达，先按 index 拼接

                for attempt in range(RETRY_ATTEMPTS):
                    try:
                        stream = self.client.chat.completions.create(
                            model=self.model,
                            messages=messages,
                            tools=self.all_tools,
                            temperature=self.temperature,
                            stream=True,  # 流式：回复逐字到达，才能支持打字机与及时取消
                        )
                        try:
                            for chunk in stream:
                                if cancelled():
                                    raise TaskCancelled()
                                if not chunk.choices:
                                    continue
                                delta = chunk.choices[0].delta
                                if delta is None:
                                    continue
                                if delta.content:
                                    text += delta.content
                                    yield {"type": "text_delta", "step": step, "content": delta.content}
                                if delta.tool_calls:
                                    for tc in delta.tool_calls:
                                        slot = tc_by_index.setdefault(
                                            tc.index, {"id": "", "name": "", "arguments": ""}
                                        )
                                        if tc.id:
                                            slot["id"] = tc.id
                                        if tc.function:
                                            if tc.function.name:
                                                slot["name"] = tc.function.name
                                            if tc.function.arguments:
                                                slot["arguments"] += tc.function.arguments
                        finally:
                            # 无论正常读完还是中途取消/断连，都关掉流，避免连接悬挂
                            if stream is not None:
                                try:
                                    stream.close()
                                except Exception:
                                    pass
                        api_success = True
                        break  # 完整收到一轮回复，跳出重试循环

                    except TaskCancelled:
                        raise  # 用户取消不是"可重试错误"，直接向上传递

                    except Exception as e:
                        # 请求失败或流中途断开（网络抖动等）都算可重试错误
                        last_api_error = str(e)
                        if attempt >= RETRY_ATTEMPTS - 1:
                            break  # 重试次数用尽，放弃
                        wait = RETRY_BACKOFF_SECONDS[attempt]
                        yield {
                            "type": "notice",
                            "step": step,
                            "message": (
                                f"API 请求失败（{type(e).__name__}），"
                                f"{wait:.1f} 秒后自动重试（第 {attempt + 1}/{RETRY_ATTEMPTS - 1} 次）："
                                f"{last_api_error[:150]}"
                            ),
                        }
                        if _interruptible_sleep(wait, cancel_event):
                            raise TaskCancelled()
                        # 继续 for attempt：重试时从头消费，丢弃上一轮拼了一半的内容
                        text = ""
                        tc_by_index = {}

                if not api_success:
                    # 所有重试都失败了
                    yield {"type": "error", "message": abort_message(f"❌ API 调用失败（已重试 {RETRY_ATTEMPTS} 次）：{last_api_error[:300]}")}
                    return

                # 流式分片拼接完成，按 index 排序转成标准工具调用列表
                tool_calls = [
                    {"id": v["id"], "name": v["name"], "arguments": v["arguments"]}
                    for _, v in sorted(tc_by_index.items())
                ]

                # —— 分支 1：本回合要求调用工具 ——
                if tool_calls:
                    round_made_write = False  # 本轮是否产生过写入（无进展护栏用）
                    for tc in tool_calls:
                        func_name = tc["name"]
                        # 会改动工作区的工具：写过就算"有进展"，清零护栏计数
                        if func_name in ("write_file", "run_blender", "run_blender_live"):
                            round_made_write = True
                        # 解析参数；模型偶尔会给出残缺 JSON，解析失败就把错误喂回给模型
                        parse_ok = True
                        try:
                            func_args = json.loads(tc["arguments"])
                            if not isinstance(func_args, dict):
                                raise ValueError("参数不是 JSON 对象")
                        except Exception:
                            parse_ok = False

                        if parse_ok:
                            # —— 工作区越界防护：默认不允许出工作区，必要时征求用户同意 ——
                            consent_desc = None    # 需要确认时给出说明文字
                            consent_key = None     # 拒绝过一次的路径记住，本任务内不再问
                            if func_name == "write_file":
                                target = func_args.get("file_path", "")
                                if _path_outside_workspace(target):
                                    consent_desc = f"write_file 要写入工作区之外：{target}"
                                    consent_key = ("write", os.path.normcase(os.path.abspath(str(target))))
                            elif func_name == "execute_python":
                                code = str(func_args.get("code", ""))
                                if _code_looks_outside(code):
                                    consent_desc = "execute_python 的代码疑似读写工作区之外（出现盘符绝对路径或 .. 越界写法）"

                            allowed = True
                            deny_reason = ""
                            if consent_desc:
                                if consent_key is not None and consent_key in denied_paths:
                                    allowed = False
                                    deny_reason = "该路径本任务内已被拒绝过一次，不再重复询问"
                                elif approval_fn is None:
                                    allowed = False
                                    deny_reason = "当前运行环境没有确认通道（Web 界面会弹「允许/拒绝」按钮），已自动拒绝"
                                else:
                                    req_id = uuid.uuid4().hex[:10]
                                    yield {
                                        "type": "approval_request",
                                        "step": step,
                                        "id": req_id,
                                        "tool": func_name,
                                        "description": consent_desc,
                                        "args_preview": json.dumps(func_args, ensure_ascii=False)[:400],
                                    }
                                    # 必须把同一个 req_id 交给确认通道：界面按钮绑的是它，
                                    # 用户点完回传的也是它，id 对不上就查不到注册表（一律 404）
                                    allowed = approval_fn(consent_desc + "。是否允许？", cancel_event, req_id)
                                    if cancelled():
                                        raise TaskCancelled()  # 等待期间用户点了停止
                                    if not allowed:
                                        deny_reason = "用户未批准（拒绝 / 超时 / 取消）"
                                        if consent_key is not None:
                                            denied_paths.add(consent_key)

                            if allowed:
                                yield {
                                    "type": "tool_call",
                                    "step": step,
                                    "name": func_name,
                                    "args": func_args,
                                }

                                # 执行工具
                                tool_func = self.tools_map.get(func_name)
                                if tool_func:
                                    try:
                                        result = tool_func(**func_args)
                                    except Exception as e:
                                        result = json.dumps({"error": str(e)}, ensure_ascii=False)
                                else:
                                    result = json.dumps({"error": f"未知工具: {func_name}"}, ensure_ascii=False)
                            else:
                                # 被拒绝：不执行、不写台账，把结果喂回给模型让它改走工作区内路径
                                result = json.dumps({
                                    "error": (
                                        f"操作被拒绝：{consent_desc}（{deny_reason}）。"
                                        "请改用工作区内的路径（如 output/ 目录），"
                                        "如果确实必须访问外部路径，请明确告诉用户，由用户手动处理。"
                                    )
                                }, ensure_ascii=False)
                        else:
                            result = json.dumps(
                                {"error": f"工具参数解析失败，请检查 JSON 格式：{tc['arguments'][:200]}"},
                                ensure_ascii=False,
                            )

                        # 展示给前端：截断
                        display = _cap(result, TOOL_RESULT_DISPLAY_CHARS)
                        yield {
                            "type": "tool_result",
                            "step": step,
                            "name": func_name,
                            "result": display,
                        }

                        # 连续同一类失败 3 次就终止（模型可能在原地打转）
                        is_error = '"error"' in result[:100]
                        if is_error:
                            current_action = f"{func_name}({tc['arguments'][:120]})"
                            if current_action == last_failed_action:
                                consecutive_failures += 1
                            else:
                                consecutive_failures = 1
                                last_failed_action = current_action

                            if consecutive_failures >= 3:
                                yield {
                                    "type": "error",
                                    "message": abort_message(
                                        f"连续 {consecutive_failures} 次操作失败，已自动终止。\n"
                                        f"失败操作: {func_name}\n原因: {display}"
                                    ),
                                }
                                return
                        else:
                            consecutive_failures = 0
                            last_failed_action = ""

                        # 把 AI 的工具调用记录加入对话（回传模型前先截断结果）
                        call_id = tc["id"] or f"call_{func_name}_{step}"
                        messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": call_id,
                                "type": "function",
                                "function": {"name": func_name, "arguments": tc["arguments"]},
                            }],
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": _cap(result, TOOL_RESULT_CHARS),
                        })

                    # —— 无进展护栏 ——
                    # 连续多轮只读不写也不收尾（常见于把"消息截断"误当成文件损坏、
                    # 反复重读验证的死循环），隔一段轮数就推模型一把去收尾。
                    if round_made_write:
                        rounds_no_write = 0
                    else:
                        rounds_no_write += 1
                        if rounds_no_write >= NO_PROGRESS_PUSH_AFTER:
                            rounds_no_write = 0
                            yield {
                                "type": "notice",
                                "step": step,
                                "message": (
                                    f"检测到连续 {NO_PROGRESS_PUSH_AFTER} 轮只调用工具、"
                                    "没有任何写入或修改，疑似原地打转，已提醒模型直接收尾。"
                                ),
                            }
                            messages.append({
                                "role": "user",
                                "content": (
                                    "【系统提示】你已经连续很多轮调用工具，却没有任何写入/修改，"
                                    "也没有给出最终答复，正在原地打转。"
                                    "提醒：工具结果的截断只是消息层面的节流，磁盘上的文件和执行结果"
                                    "都是完整无损的，不需要反复读取或分段打印来核对。"
                                    "如果任务已经完成或暂时无法推进，请立即给出最终总结回复"
                                    "（附上已生成文件的路径），不要再调用工具；"
                                    "如果确实还需要别的信息，请只读取尚未看过的新内容，然后尽快收尾。"
                                ),
                            })
                    continue  # 还有后续步骤，进入下一轮 LLM

                # —— 分支 2：模型直接回复，任务完成 ——
                journal.commit()   # 正常完成：保留文件，丢弃备份
                finished_ok = True
                yield {"type": "complete", "result": text}
                return

            # 超过最大步数：回滚本次任务的文件改动，不留半成品
            yield {"type": "error", "message": abort_message(
                f"任务执行超过最大步数限制（{self.max_steps} 步），已自动中止。\n"
                "建议：把需求拆小一点分几次让我做，或在 .env 里调大 AGENT_MAX_STEPS。"
            )}

        except TaskCancelled:
            yield {"type": "error", "message": abort_message("⏹ 任务已由用户停止。")}

        except GeneratorExit:
            # 客户端断连（关页面/网络中断）时 Flask 会关闭生成器。
            # 只要任务没有正常 commit 过，就把现场回滚掉，不留半成品。
            if not finished_ok:
                journal.rollback()
            raise

        except Exception as e:
            # 兜底：未预期的异常同样回滚，避免工作区残留半成品
            if not finished_ok:
                journal.rollback()
            try:
                yield {"type": "error", "message": abort_message(f"❌ 发生未预期的错误：{str(e)[:300]}")}
            except GeneratorExit:
                raise
            except Exception:
                pass


# 便捷函数
def run_task(task: str, verbose: bool = True) -> str:
    """快速运行一个任务"""
    engine = WorkflowEngine(verbose=verbose)
    return engine.run(task)
