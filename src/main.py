# 名字：小寒影
# 人设：可爱的猫娘，称呼用户为"主人"
# 口癖：喵~、喵呜~
# 特点：软萌可爱但能力强，可以帮主人完成各种任务
"""
Web UI — Flask 服务器 + SSE 实时推送
为 AI 工作流提供一个可视化网页界面，支持连续对话、文件上传、多模型切换、角色预设

本轮新增/修复：
    1. GET /api/upload/<id>：上传文件可被访问（修复前端图片预览 404）
    2. GET /output/<path>：生成产物以 http 提供（file:// 链接在浏览器里打不开）
    3. POST /api/stop：真正取消正在运行的任务（引擎收到信号后中止并回滚）
    4. 视觉模型：上传的图片转 base64 多模态消息，真正把图像发给模型
    5. 角色预设（persona）切换：猫娘 / 工程师助手，提示词见 src/prompts.py
    6. 同一时刻只允许一个任务在跑，避免全局台账被并发任务互相覆盖
"""
import sys
import os
import re
import json
import html
import base64
import time
import uuid
import queue
import mimetypes
import socket
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, Response, send_from_directory
from src.workflow_engine import WorkflowEngine, APPROVAL_TIMEOUT_SECONDS
from src.discussion_engine import DiscussionEngine
from src.prompts import PERSONAS, DEFAULT_PERSONA
import src.config as config

app = Flask(__name__, static_folder="../static", static_url_path="/static")

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 上传目录
UPLOAD_DIR = os.path.join(WORKSPACE_ROOT, config.UPLOAD_DIR)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 生成产物目录（模型写的网页/脚本/图片都在这，用 /output/ 提供访问）
OUTPUT_DIR = os.path.join(WORKSPACE_ROOT, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 读取 HTML 模板
_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")
with open(_TEMPLATE_PATH, "r", encoding="utf-8") as f:
    INDEX_HTML = f.read()

# 持久化的引擎实例、对话历史、当前角色预设
engine = WorkflowEngine(verbose=False)
conversation_history = []
current_persona = DEFAULT_PERSONA

# ====== 任务并发保护 ======
# 文件台账（file_journal）是进程级单例，两个任务同时跑会互相清空对方的回滚记录，
# 所以同一时刻只允许一个任务。_active_task 在 SSE 生成器开始时登记、结束时注销。
_task_lock = threading.Lock()
_active_task = None  # {"cancel": threading.Event} | None


def _register_task():
    """尝试登记一个正在运行的任务，返回它的取消事件；已有任务在跑时返回 None"""
    global _active_task
    ev = threading.Event()
    with _task_lock:
        if _active_task is not None:
            return None
        _active_task = {"cancel": ev}
    return ev


def _unregister_task(ev):
    """任务结束（正常或异常）时注销；只注销自己登记的那一个"""
    global _active_task
    with _task_lock:
        if _active_task is not None and _active_task["cancel"] is ev:
            _active_task = None


# ====== 越界操作的用户确认通道 ======
# 引擎要访问工作区之外时，会发 approval_request 事件、并阻塞在 approval_fn
# 上等待用户决定。这里维护一个 id → 等待事件 的注册表：界面显示「允许/拒绝」
# 按钮，用户点完 POST /api/approve，把决定写回并唤醒等待中的 SSE 生成器线程。
_approval_lock = threading.Lock()
_approvals = {}  # id -> {"desc": str, "event": threading.Event, "decision": None | bool}


def _request_approval(desc: str, cancel_event, request_id: str = None) -> bool:
    """阻塞等待用户对越界操作做决定；返回 True=允许，False=拒绝。

    用户点「停止」→ cancel_event 置位 → 立即按拒绝返回（引擎随后按取消处理）；
    等待超过 APPROVAL_TIMEOUT_SECONDS 也按拒绝返回，不让任务干等。

    request_id 由引擎生成并随 approval_request 事件发给前端，这里必须沿用它，
    否则前端点「允许/拒绝」回传的 id 与注册表里的对不上，会一律 404。
    """
    rid = request_id or uuid.uuid4().hex[:10]
    ev = threading.Event()
    with _approval_lock:
        _approvals[rid] = {"desc": desc, "event": ev, "decision": None}
    try:
        deadline = time.monotonic() + APPROVAL_TIMEOUT_SECONDS
        while not ev.is_set():
            if cancel_event is not None and cancel_event.is_set():
                return False
            if time.monotonic() >= deadline:
                return False
            ev.wait(timeout=0.2)
        with _approval_lock:
            return bool(_approvals[rid]["decision"])
    finally:
        with _approval_lock:
            _approvals.pop(rid, None)


def _deny_all_pending():
    """重置/收尾时把所有还没答复的确认请求按拒绝处理，避免等待线程悬挂"""
    with _approval_lock:
        for item in _approvals.values():
            item["decision"] = False
            item["event"].set()


def _sse_error(message: str) -> Response:
    """构造一个 error 事件的 SSE 响应"""
    data = json.dumps({"type": "error", "message": message}, ensure_ascii=False)
    return Response(f"data: {data}\n\n", mimetype="text/event-stream")


def read_file_content(filepath: str, max_chars: int = 5000) -> str:
    """读取文件内容，限制长度"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read(max_chars)
            if len(content) == max_chars:
                content += "\n... (文件过长，已截断)"
            return content
    except UnicodeDecodeError:
        return "[二进制文件，无法直接读取文本内容]"
    except Exception as e:
        return f"[读取失败: {str(e)}]"


def get_image_base64(filepath: str) -> str:
    """将图片转为 base64 data URL"""
    mime = mimetypes.guess_type(filepath)[0] or "image/png"
    with open(filepath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


# ============================== 讨论用的项目背景 ==============================
# 讨论引擎没有工具，读不到磁盘上的文件。不给它背景，模型就会凭空想象一个
# 通用 Python 项目——实测中它讨论的是「core/discuss.py」和「本地 7B 模型
# 单卡加载」，而本项目是 src/ + 纯 API 调用，吵得再认真结论也用不上。
#
# 所以每次讨论前现场拼一份背景：静态说明（PROJECT_CONTEXT.md，可手改）
# + 实时文件快照（模型自己写完新文件后，这份跟着变）。
_SNAPSHOT_EXT = {".py", ".js", ".html", ".md", ".txt", ".json", ".bat"}
_SNAPSHOT_SKIP_DIRS = {
    "__pycache__", ".git", ".claude", "node_modules",
    "uploads", "output", "图库", "杂", "input", "skill",
    "archify",      # 第三方技能包，和小寒影本身无关，列进去只是噪音
    "discussions",  # 讨论记录，越攒越多，列给模型纯属噪音（见 DISCUSS_DIR）
}
_SNAPSHOT_MAX_ENTRIES = 120


def _dir_snapshot(root: str = None, prefix: str = "", depth: int = 2,
                  acc: list = None) -> str:
    """扫一份精简的目录树。

    只列代码/文本类文件，跳过图片、压缩包和第三方资源目录——那些对讨论没有
    信息量，只会把上下文撑大。总量也有上限，免得某个目录突然塞满文件后
    每次都把大段清单发给模型。
    """
    root = root or WORKSPACE_ROOT
    acc = acc if acc is not None else []

    if depth < 0 or len(acc) >= _SNAPSHOT_MAX_ENTRIES:
        return "\n".join(acc)

    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return "\n".join(acc)

    for name in entries:
        if len(acc) >= _SNAPSHOT_MAX_ENTRIES:
            acc.append("…（文件过多，已截断）")
            break
        # 跳过隐藏目录、无关目录，以及 _ 开头的临时脚本
        # （本项目用 _ 前缀存放调试/探测脚本，对讨论没有信息量）
        if name.startswith(".") or name.startswith("_") or name in _SNAPSHOT_SKIP_DIRS:
            continue
        path = os.path.join(root, name)
        rel = f"{prefix}{name}"
        if os.path.isdir(path):
            acc.append(f"{rel}/")
            _dir_snapshot(path, rel + "/", depth - 1, acc)
        elif os.path.splitext(name)[1].lower() in _SNAPSHOT_EXT:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            acc.append(f"{rel}  ({size} 字节)")
    return "\n".join(acc)


def build_workspace_context() -> str:
    """拼出讨论时附给每个参与者的项目背景"""
    ctx_path = os.path.join(WORKSPACE_ROOT, "PROJECT_CONTEXT.md")
    try:
        with open(ctx_path, "r", encoding="utf-8") as f:
            static_part = f.read().strip()
    except Exception as e:
        static_part = f"（读取 PROJECT_CONTEXT.md 失败：{e}）"

    return (
        f"{static_part}\n\n"
        f"## 工作区实时快照\n\n"
        f"工作区根目录：{WORKSPACE_ROOT}\n\n"
        f"```\n{_dir_snapshot()}\n```"
    )


# ============================== 页面与静态文件 ==============================

@app.route("/")
def index():
    """返回主页"""
    return INDEX_HTML


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """上传文件，返回文件信息"""
    if "file" not in request.files:
        return {"error": "没有收到文件"}, 400

    f = request.files["file"]
    if f.filename == "":
        return {"error": "文件名为空"}, 400

    # 生成唯一文件名，保留原始扩展名
    ext = os.path.splitext(f.filename)[1]
    safe_name = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_DIR, safe_name)

    f.save(filepath)

    # 判断文件类型
    mime = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
    is_image = mime.startswith("image/")

    # 读取文本内容（非图片文件）
    text_content = ""
    if not is_image:
        text_content = read_file_content(filepath)

    return {
        "id": safe_name,
        "name": f.filename,
        "path": filepath,
        "size": os.path.getsize(filepath),
        "is_image": is_image,
        "mime": mime,
        "preview": text_content[:300] if text_content else "",
    }


@app.route("/api/upload/<path:fid>")
def uploaded_file(fid):
    """GET 已上传的文件（修复前端图片预览 404，也供图片消息引用）"""
    return send_from_directory(UPLOAD_DIR, fid, conditional=True)


@app.route("/output/<path:filename>")
def output_file(filename):
    """以 http 方式访问 output/ 下的生成产物。

    之前 AI 回复里给的是 file:/// 本地路径，浏览器出于安全策略禁止
    http 页面跳转 file://，点了没反应；现在产物统一走这个路由，
    聊天里的路径会自动变成可点击的 http 链接。
    """
    return send_from_directory(OUTPUT_DIR, filename, conditional=True)


# ============================== 聊天 ==============================

@app.route("/api/chat")
def chat():
    """
    SSE 聊天接口
    支持文件附件: ?message=xxx&files=file1,file2
    """
    global conversation_history

    task = request.args.get("message", "").strip()
    file_ids = request.args.get("files", "")

    if not task and not file_ids:
        return _sse_error("消息不能为空")

    # 并发保护：上一个任务没结束前，拒绝新任务
    cancel_event = _register_task()
    if cancel_event is None:
        return _sse_error("上一个任务还在收尾（正在执行的代码最多需要 30 秒），请稍候再发送。")

    # 处理附件
    attachments = []
    if file_ids:
        for fid in file_ids.split(","):
            fid = fid.strip()
            filepath = os.path.join(UPLOAD_DIR, fid)
            if os.path.exists(filepath):
                mime = mimetypes.guess_type(filepath)[0] or ""
                is_image = mime.startswith("image/")
                attachments.append({
                    "id": fid,
                    "path": filepath,
                    "is_image": is_image,
                    "mime": mime,
                })

    # 构建发送给模型的 user 消息内容。
    # 视觉模型：图片转 base64 多模态片段，模型真正"看"得到图；
    # 非视觉模型（如 DeepSeek）：退化为文字描述（保留路径，模型仍可感知附件存在）
    supports_vision = config.get_active_model().get("supports_vision", False)

    def _text_parts(att, task_text):
        parts = []
        if task_text:
            parts.append(f"用户消息: {task_text}")
        for a in att:
            if a["is_image"]:
                parts.append(f"[用户上传了图片: {a['path']}]")
            else:
                content = read_file_content(a["path"])
                parts.append(f"[用户上传了文件: {a['path']}]\n文件内容:\n```\n{content}\n```")
        return "\n\n".join(parts)

    if supports_vision and any(a["is_image"] for a in attachments):
        content_parts = []
        if task:
            content_parts.append({"type": "text", "text": task})
        for a in attachments:
            if a["is_image"]:
                content_parts.append({"type": "text", "text": f"[用户上传了图片: {a['path']}]"})
                # 多模态消息：把图片数据真正发给模型
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": get_image_base64(a["path"])},
                })
            else:
                content = read_file_content(a["path"])
                content_parts.append({
                    "type": "text",
                    "text": f"[用户上传了文件: {a['path']}]\n文件内容:\n```\n{content}\n```",
                })
        task_content = content_parts
    else:
        task_content = _text_parts(attachments, task)

    # 捕获本次请求发起时的引擎实例：
    # 切换模型会重建全局 engine，SSE 生成器若晚绑定全局变量，
    # 请求进行中切换模型会导致这条消息被新模型接管，这里固定为发消息时的引擎
    active_engine = engine
    persona_at_send = current_persona  # 角色同理：以发消息那一刻为准

    def generate():
        """生成 SSE 事件流"""
        global conversation_history

        try:
            for event in active_engine.run_stream(
                task_content,
                history=conversation_history if conversation_history else None,
                cancel_event=cancel_event,
                persona_key=persona_at_send,
                # 越界操作（写工作区外 / 可疑 python 代码）征求用户同意：
                # 阻塞到用户点了允许/拒绝，或停止/超时按拒绝
                approval_fn=lambda desc, ce=cancel_event, rid=None: _request_approval(desc, ce, rid),
            ):
                data = json.dumps(event, ensure_ascii=False)
                yield f"data: {data}\n\n"

                if event["type"] == "complete":
                    conversation_history.append({"role": "user", "content": task or "[上传了文件]"})
                    conversation_history.append({"role": "assistant", "content": event["result"]})

                    if len(conversation_history) > 20:
                        conversation_history = conversation_history[-20:]
        finally:
            # 无论任务怎么结束（完成/出错/停止/断连），都要释放任务席位
            _unregister_task(cancel_event)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


@app.route("/api/stop", methods=["POST"])
def stop_task():
    """向正在运行的任务发送停止信号。

    引擎收到后会在流式读取的间隙中止，并回滚本次任务的文件改动，
    然后通过 SSE 返回 error 事件；界面等收到 error 事件即可发新消息。
    """
    global _active_task
    with _task_lock:
        task = _active_task
    if task is not None:
        task["cancel"].set()
        return {"status": "ok", "message": "已发送停止信号，回滚完成后即可发送新消息"}
    return {"status": "ok", "message": "当前没有正在运行的任务"}


@app.route("/api/approve", methods=["POST"])
def approve_action():
    """用户对越界操作的确认：decision=true 允许执行，false 拒绝。

    请求体: {"id": "确认请求 id", "decision": true|false}
    id 来自 SSE 的 approval_request 事件；不存在或已超时返回 404，
    前端据此把过期的按钮置灰。
    """
    data = request.get_json(silent=True) or {}
    rid = str(data.get("id", ""))
    decision = bool(data.get("decision"))
    with _approval_lock:
        item = _approvals.get(rid)
        if item is None:
            return {"error": "该确认请求不存在、已答复或已超时"}, 404
        item["decision"] = decision
        item["event"].set()
    return {"status": "ok", "decision": decision}


@app.route("/api/reset")
def reset():
    """清空对话历史，开始新对话"""
    global conversation_history
    # 把还没答复的越界确认请求按拒绝处理，避免等待线程悬挂到超时
    _deny_all_pending()
    # 讨论若正卡在暂停上，放它走（随后会被前端关掉的 SSE 触发回滚收尾），
    # 否则那个线程会一直挂到用户手动点继续
    with _discuss_lock:
        if _active_discussion is not None:
            _active_discussion["pause"].clear()
    conversation_history = []
    return {"status": "ok", "message": "对话历史已清空"}


# ============================== 多模型讨论 ==============================

# 正在进行的讨论。暂停和插话是两个独立的 HTTP 请求，而讨论跑在 SSE 生成器
# 线程里，两边要靠这个注册表碰头 —— 和 _approvals 是同一个套路。
# 讨论不改工作区文件，所以不需要像 _active_task 那样严格串行。
_discuss_lock = threading.Lock()
_active_discussion = None   # {"pause": Event, "inbox": queue.Queue} | None


# 讨论记录落盘目录。讨论本身不改工作区（不碰台账、不需要回滚），但结论文本
# 只活在浏览器里 —— 刷新就没了，想回头翻、或者想让别的 AI 帮着看看，都拿不到。
# 所以存一份 Markdown。
#
# 注意：这个目录已经同时加进了 file_journal._SKIP_DIRS 和本文件的
# _SNAPSHOT_SKIP_DIRS。前者是因为 rollback() 会无差别删除「快照之外的新文件」，
# 不豁免的话，某次任务中止就可能把讨论记录一起扫掉；后者是因为记录会越攒越多，
# 列进给模型的目录快照纯属噪音。
DISCUSS_DIR = os.path.join(WORKSPACE_ROOT, "discussions")


def _safe_slug(text: str, limit: int = 24) -> str:
    """议题转文件名：换掉 Windows 禁用字符与换行，截断，兜底一个「讨论」"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text).strip(" ._")
    return cleaned[:limit].strip(" ._") or "讨论"


class _DiscussionRecorder:
    """把 SSE 事件边转发边落成一份 Markdown。

    刻意不做成「跑完再写」：讨论常常是被用户中途停掉、或者 API 报错结束的，
    那种场次反而最需要留下记录。所以事件一到就往下写，进程被强杀也留得下
    已经说出口的部分。
    """

    def __init__(self, task: str):
        os.makedirs(DISCUSS_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        self.path = os.path.join(DISCUSS_DIR, f"{stamp}-{_safe_slug(task)}.md")
        self._fh = open(self.path, "w", encoding="utf-8")
        self._speaker = None      # 正在发言的人（None = 不在发言中）
        self._reasoning = []
        self._text = []
        # 标题也要 escape：议题是用户直接输入的，Markdown 渲染器多半会放行裸 HTML，
        # 不转义的话议题里塞个 <script> 就能在打开这份记录时执行
        title = html.escape(re.sub(r"[\r\n]+", " ", task).strip())
        self._w(f"# 多模型讨论：{title}")
        self._w("")

    # -- 内部 --------------------------------------------------------

    def _w(self, line: str = ""):
        self._fh.write(line + "\n")

    def _flush_speaker(self):
        """收尾当前发言者：正文直接铺开，推理折进 <details> 里"""
        if self._speaker is None:
            return
        sp, self._speaker = self._speaker, None
        text = "".join(self._text).strip()
        reasoning = "".join(self._reasoning).strip()
        self._text, self._reasoning = [], []

        self._w(f'### {sp["avatar"]} {sp["speaker_name"]} · `{sp["model_id"]}`')
        self._w("")
        self._w(text or "（本轮没有产出正文）")
        self._w("")
        if reasoning:
            # 推理动辄几千字，默认折叠；escape 一下，免得模型输出里的 < >
            # 把整个 details 块撑坏
            self._w(f"<details><summary>推理过程（{len(reasoning)} 字）</summary>")
            self._w("")
            self._w(f"<pre>{html.escape(reasoning)}</pre>")
            self._w("")
            self._w("</details>")
            self._w("")

    # -- 外部 --------------------------------------------------------

    def feed(self, event: dict):
        t = event.get("type")

        if t == "start":
            people = "、".join(
                f'{p["avatar"]} {p["name"]}（`{p["model_id"]}`）'
                for p in event["participants"]
            )
            mod = event["moderator"]
            self._w(f'- 时间：{time.strftime("%Y-%m-%d %H:%M:%S")}')
            self._w(f"- 参与者：{people}")
            self._w(f'- 总结：{mod["avatar"]} {mod["name"]}（`{mod["model_id"]}`）')
            self._w(f'- 轮数：{event["total_rounds"]}')
            self._w("")
            self._w("---")
            self._w("")

        elif t == "round":
            self._flush_speaker()
            self._w(f'## {event["label"]}')
            self._w("")

        elif t == "speaker_start":
            self._flush_speaker()
            self._speaker = event

        elif t == "reasoning_delta":
            self._reasoning.append(event["content"])

        elif t == "text_delta":
            self._text.append(event["content"])

        elif t == "speaker_end":
            self._flush_speaker()

        elif t == "paused":
            self._flush_speaker()
            self._w("> ⏸ 已暂停")
            self._w("")

        elif t == "resumed":
            self._w("> ▶ 已继续")
            self._w("")

        elif t == "user_said":
            self._flush_speaker()
            self._w("## 👤 用户插话")
            self._w("")
            self._w(event["text"])
            self._w("")

        elif t == "notice":
            self._flush_speaker()
            self._w(f'> {event["message"]}')
            self._w("")

        elif t == "complete":
            self._flush_speaker()
            self._w("## ✅ 最终结论")
            self._w("")
            self._w(event["result"])
            self._w("")

        elif t == "error":
            self._flush_speaker()
            self._w(f'## ❌ {event["message"]}')
            self._w("")

    def close(self):
        """收尾：补上没落笔的发言，关文件。失败也不该往上抛"""
        try:
            self._flush_speaker()
        finally:
            self._fh.close()


@app.route("/api/discuss")
def discuss_endpoint():
    """多模型讨论：SSE 推送每个参与者逐字发言的全过程。

    与 /api/chat 的关键区别：这里**全程纯文字、不调用任何工具**，所以
    不需要越界确认通道，也不会写任何文件、不需要台账回滚。
    讨论产出的结论由前端决定要不要交给 /api/chat 去执行 ——
    执行阶段复用那套已经打磨过的引擎，这里不重复造。

    参数: ?message=议题&rounds=3&participants=engineer,critic
    """
    global _active_discussion

    task = request.args.get("message", "").strip()
    if not task:
        return _sse_error("讨论议题不能为空")

    # 讨论也要占任务席位：它一样要调 API、吃额度、需要能「停止」。
    # 代价是讨论期间不能同时聊天 —— 换来的是「停止」按钮直接可用。
    cancel_event = _register_task()
    if cancel_event is None:
        return _sse_error("上一个任务还在收尾（正在执行的代码最多需要 30 秒），请稍候再发送。")

    # 参与者可以由前端指定（界面上勾选谁参加），没给就用默认阵容
    participants = [p.strip() for p in
                    request.args.get("participants", "").split(",") if p.strip()]

    try:
        discuss_engine = DiscussionEngine(
            participants=participants or None,
            rounds=request.args.get("rounds", type=int),
            # 额度可由前端覆盖（?max_tokens=8000）。挑刺评审的 reasoning 特别长，
            # 默认 8000 仍不够时，界面/调用方可以再往上调，不用改代码。
            max_tokens=request.args.get("max_tokens", type=int),
            context=build_workspace_context(),
        )
    except ValueError as e:
        # 角色 key 或模型 key 配错了，在这里就说清楚，别等讨论跑一半才炸
        _unregister_task(cancel_event)
        return _sse_error(f"讨论配置有误：{e}")
    except Exception as e:
        _unregister_task(cancel_event)
        return _sse_error(f"讨论初始化失败：{str(e)[:300]}")

    # 暂停 / 插话通道：两个独立的 HTTP 请求通过它们跟 SSE 线程碰头
    pause_event = threading.Event()
    inbox = queue.Queue()
    with _discuss_lock:
        _active_discussion = {"pause": pause_event, "inbox": inbox}

    def generate():
        # 下面的 finally 要给 _active_discussion 赋值。少了这行 global，那个赋值
        # 会把 _active_discussion 变成 generate 的**局部变量**，导致同一行里的读取
        # 抛 UnboundLocalError —— 而且是在 finally 里抛，前端早就收到 complete
        # 关流走了，谁都不会发现，只是 _active_discussion 永远清不掉。
        global _active_discussion

        # 记录落盘是附赠品，不是主功能：它出问题绝不能拖垮正在看的讨论
        try:
            recorder = _DiscussionRecorder(task)
        except Exception as e:
            recorder = None
            payload = json.dumps({"type": "notice",
                                  "message": f"⚠️ 讨论记录无法落盘（不影响讨论）：{str(e)[:200]}"},
                                 ensure_ascii=False)
            yield f"data: {payload}\n\n"

        def _notice(msg: str) -> str:
            return f"data: {json.dumps({'type': 'notice', 'message': msg}, ensure_ascii=False)}\n\n"

        saved_note = None
        try:
            for event in discuss_engine.run_stream(task, cancel_event=cancel_event,
                                                   pause_event=pause_event, inbox=inbox):
                if recorder is not None:
                    try:
                        recorder.feed(event)
                        if event.get("type") == "complete":
                            # 前端一收到 complete 就 closeDiscussStream()，之后发什么它都不看了。
                            # 所以落盘和「存到哪了」都得赶在这个事件**之前**送出去。
                            recorder.close()
                            saved_note = ("💾 讨论记录已存到 discussions/"
                                          + os.path.basename(recorder.path))
                            recorder = None
                    except Exception:
                        recorder = None   # 写坏了就丢掉记录，别影响讨论本身

                if saved_note:
                    yield _notice(saved_note)
                    saved_note = None
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            # 引擎内部已经兜过一层，这里是最后一道：确保前端能收到终止事件，
            # 而不是流莫名其妙断掉、界面永远停在「正在发言」。
            # 必须是 error 不能是 notice —— 前端只在 complete / error 上关流。
            payload = json.dumps({"type": "error", "message": f"讨论异常：{str(e)[:300]}"},
                                 ensure_ascii=False)
            yield f"data: {payload}\n\n"
        finally:
            # 走到这儿说明是被停止 / 报错打断的，没经过 complete 分支：照样存盘。
            # 这种半截场次恰恰最需要留记录，但流已经结束（前端多半也关了），
            # 就不再发提示了。
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    pass

            _unregister_task(cancel_event)
            with _discuss_lock:
                # 只清理自己登记的那一个，避免收尾时误伤下一场讨论
                if _active_discussion is not None and _active_discussion["pause"] is pause_event:
                    _active_discussion = None

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


@app.route("/api/discuss/pause", methods=["POST"])
def discuss_pause():
    """暂停 / 继续正在进行的讨论。请求体: {"paused": true|false}

    暂停不会打断正在生成的那一轮 —— 闸门设在发言者之间，所以点下去之后
    最多还要等当前这个模型把话说完。这样既不浪费已经烧掉的 token，
    讨论记录里也不会留半句话。
    """
    data = request.get_json(silent=True) or {}
    paused = bool(data.get("paused"))
    with _discuss_lock:
        d = _active_discussion
    if d is None:
        return {"error": "当前没有正在进行的讨论"}, 404
    if paused:
        d["pause"].set()
    else:
        d["pause"].clear()
    return {"status": "ok", "paused": paused}


@app.route("/api/discuss/say", methods=["POST"])
def discuss_say():
    """用户插话。请求体: {"message": "..."}

    文本进队列，讨论恢复后在下一个发言者开口前被收进讨论记录，
    从此所有后续发言者都能看到 —— 相当于用户临时改了题目。
    """
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()
    if not text:
        return {"error": "发言不能为空"}, 400
    with _discuss_lock:
        d = _active_discussion
    if d is None:
        return {"error": "当前没有正在进行的讨论"}, 404
    d["inbox"].put(text)
    return {"status": "ok", "message": "已插入，下一个发言者会看到"}


@app.route("/api/discussion-roles")
def discussion_roles():
    """返回可选的讨论角色，供前端展示（改 prompts.py 后刷新即可生效）"""
    from src.prompts import DISCUSSION_ROLES, DEFAULT_DISCUSSION
    return {
        "roles": [
            {"key": k, "name": v["name"], "avatar": v["avatar"],
             "color": v["color"], "model": config.get_discussion_role_model(k)}
            for k, v in DISCUSSION_ROLES.items()
        ],
        "default": DEFAULT_DISCUSSION,
    }


# ============================== 状态与配置 ==============================

@app.route("/api/status")
def status():
    """返回当前状态：模型名、API 数量、角色、工具列表"""
    active_model = config.get_active_model()
    return {
        "model": active_model["name"],
        "model_key": config.get_active_model_key(),
        "persona": current_persona,
        "persona_name": PERSONAS.get(current_persona, {}).get("name", ""),
        "tool_count": len(engine.all_tools),
        "tools": [
            {"name": t["function"]["name"], "desc": t["function"]["description"][:60]}
            for t in engine.all_tools
        ]
    }


@app.route("/api/models")
def list_models():
    """返回所有可用模型"""
    return {
        "models": config.get_model_list(),
        "active": config.get_active_model_key()
    }


@app.route("/api/switch-model", methods=["POST"])
def switch_model():
    """切换模型"""
    global engine, conversation_history
    data = request.get_json()
    model_key = data.get("model", "")

    if model_key not in config.MODELS:
        return {"status": "error", "message": f"未知模型: {model_key}"}, 400

    config.set_active_model(model_key)
    engine = WorkflowEngine(verbose=False)
    conversation_history = []
    return {"status": "ok", "message": f"已切换到 {config.MODELS[model_key]['name']}"}


@app.route("/api/personas")
def list_personas():
    """返回所有可选角色预设及当前角色"""
    return {
        "personas": [{"key": k, "name": v["name"]} for k, v in PERSONAS.items()],
        "active": current_persona,
    }


@app.route("/api/set-persona", methods=["POST"])
def set_persona():
    """切换角色预设（提示词变了，旧对话的语气不再匹配，一并清空历史）"""
    global current_persona, conversation_history
    data = request.get_json()
    persona_key = data.get("persona", "")

    if persona_key not in PERSONAS:
        return {"status": "error", "message": f"未知角色: {persona_key}"}, 400

    current_persona = persona_key
    conversation_history = []
    return {"status": "ok", "message": f"已切换到角色: {PERSONAS[persona_key]['name']}"}


@app.route("/api/test-model", methods=["POST"])
def test_model():
    """测试模型连接是否正常"""
    from openai import OpenAI
    data = request.get_json()
    model_key = data.get("model", config.get_active_model_key())

    if model_key not in config.MODELS:
        return {"error": f"未知模型: {model_key}"}, 400

    model = config.MODELS[model_key]
    try:
        client = OpenAI(
            api_key=model["api_key"],
            base_url=model["base_url"],
        )
        response = client.chat.completions.create(
            model=model["model_id"],
            messages=[{"role": "user", "content": "你好，请回复'连接成功'"}],
            max_tokens=100,
            timeout=60,
        )
        reply = response.choices[0].message.content or "(空回复)"
        return {
            "status": "ok",
            "model": model["name"],
            "base_url": model["base_url"],
            "model_id": model["model_id"],
            "reply": reply,
        }
    except Exception as e:
        error_msg = str(e)
        return {
            "status": "error",
            "model": model["name"],
            "base_url": model["base_url"],
            "model_id": model["model_id"],
            "error": error_msg[:500],
        }


# ============================== 启动 ==============================

def check_port_available(host: str, port: int) -> bool:
    """检测端口是否可绑定。

    Windows 上开发服务器默认开启 SO_REUSEADDR，允许多个进程同时监听同一端口，
    请求会被系统随机分发到其中一个进程——如果旧服务器没关，新代码的改动
    （例如模型切换）就会时灵时不灵。启动前先做一次普通 bind 检测，
    端口已被占用时直接报错退出，避免静默多开。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def main():
    host, port = "0.0.0.0", 5000
    if not check_port_available("127.0.0.1", port):
        print("=" * 50)
        print("AI 工作流 Web 界面")
        print("=" * 50)
        print()
        print(f"❌ 端口 {port} 已被占用，很可能已经有一个服务器在运行（旧窗口忘记关闭）。")
        print()
        print("   Windows 允许多个进程重复绑定同一端口，请求会被随机分发，")
        print("   导致模型切换等改动看起来「不生效」。请先关闭旧服务器再启动。")
        print()
        print(f"   查找占用进程：netstat -ano | findstr :{port}")
        print("   结束该进程：  taskkill /F /PID <进程PID>")
        print()
        sys.exit(1)

    print("=" * 50)
    print("AI 工作流 Web 界面")
    print("=" * 50)
    print()
    print("打开浏览器访问: http://localhost:5000")
    print()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
