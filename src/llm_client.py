"""
DeepSeek API 客户端
封装了与 DeepSeek 大模型的所有交互逻辑
"""

import threading

from openai import OpenAI
import src.config as config


class LLMClient:
    """
    LLM 客户端，用于和 DeepSeek 对话

    使用示例:
        client = LLMClient()
        reply = client.chat("你好，请介绍一下自己")
        print(reply)
    """

    def __init__(self):
        """初始化客户端，根据当前激活模型连接对应 API"""
        active = config.get_active_model()
        self.client = OpenAI(
            api_key=active["api_key"],
            base_url=active["base_url"],
        )
        self.model = active["model_id"]
        # 对话历史，用于多轮对话
        self.history = []

    def chat(self, user_message: str, system_prompt: str = None) -> str:
        """
        发送消息给 DeepSeek，返回回复

        参数:
            user_message: 用户说的话
            system_prompt: 系统提示词（告诉 AI 扮演什么角色）
        """
        # 构建消息列表
        messages = []

        # 系统提示词：告诉 AI 它的角色和行为规范
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # 加上历史对话
        messages.extend(self.history)

        # 加上用户当前消息
        messages.append({"role": "user", "content": user_message})

        # 调用 API
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.7,  # 0=严谨, 1=创意
        )

        # 提取 AI 的回复
        reply = response.choices[0].message.content

        # 保存到历史记录
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": reply})

        return reply

    def chat_stream(self, user_message: str, system_prompt: str = None):
        """
        流式对话：一个字一个字地输出 AI 的回复

        使用示例:
            for chunk in client.chat_stream("写一首诗"):
                print(chunk, end="", flush=True)
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(self.history)
        messages.append({"role": "user", "content": user_message})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.7,
            stream=True,  # 开启流式输出
        )

        full_reply = ""
        for chunk in response:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_reply += content
                yield content

        # 保存到历史记录
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": full_reply})

    def clear_history(self):
        """清空对话历史"""
        self.history = []


# 创建一个全局客户端实例
llm_client = LLMClient()


# ======================================================================
# 讨论模式用的无状态调用接口
# ----------------------------------------------------------------------
# 上面的 LLMClient 是「一个实例 = 一个模型 + 自带一份对话历史」，适合单模型
# 连续聊天；但它的模型是构造时就定死的（读 config 的全局激活模型），
# 一轮讨论里没法让 DeepSeek 和 SenseNova 同时在场。
# 下面这几个函数不持有任何状态：模型 key 每次显式传入、消息列表由调用方维护，
# 因此可以放心地交错、交替调用多个模型。
# ======================================================================

# 讨论/纯聊天用的默认温度。
# 引擎里那个 config.TEMPERATURE（0.3）是为「决定调哪个工具、拼 JSON 参数」调的，
# 需要确定性；讨论要的是观点差异，按聊天的 0.7 走。
DEFAULT_CHAT_TEMPERATURE = 0.7

_clients = {}          # model_key -> OpenAI
_clients_lock = threading.Lock()


def get_client(model_key: str) -> OpenAI:
    """按模型 key 取（并缓存）OpenAI 客户端。

    每次调用都新建客户端会丢掉底层连接复用，讨论模式一轮要调十几次，所以
    按 key 缓存。key 对应的 base_url/api_key 在进程内不变（.env 在 import 时
    读取），改 .env 需要重启进程才生效 —— 和现有行为一致。
    """
    with _clients_lock:
        client = _clients.get(model_key)
        if client is None:
            model = config.get_model(model_key)
            client = OpenAI(api_key=model["api_key"], base_url=model["base_url"])
            _clients[model_key] = client
        return client


def call_model(model_key: str, messages: list, *, stream: bool = False,
               max_tokens: int = None, temperature: float = None,
               tools: list = None):
    """调用指定模型（不读任何全局状态），返回原始响应对象。

    stream=True 时返回可迭代的流对象，调用方负责消费完并 close()。
    """
    model = config.get_model(model_key)
    kwargs = {
        "model": model["model_id"],
        "messages": messages,
        "temperature": DEFAULT_CHAT_TEMPERATURE if temperature is None else temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = tools
    if stream:
        kwargs["stream"] = True
    return get_client(model_key).chat.completions.create(**kwargs)


class EmptyReply(Exception):
    """模型没有产出正文（只产出了推理过程，或被 max_tokens 截断）"""


def extract_text(response) -> str:
    """从响应里取出正文，取不到时给出能直接照着修的报错。

    背景：.env 里挂的 sensenova 模型是 sensenova-6.8-flash-lite，一个会先
    输出 reasoning 再输出正文的推理模型。若 max_tokens 给小了，额度会全被
    推理吃光 —— content 直接是 null、finish_reason 是 "length"。
    这时静默返回空字符串最危险：讨论模式下会变成「模型对着空气发言」，
    别人还在认真回应它，从界面上根本看不出哪里坏了，所以这里显式报错。
    """
    choice = response.choices[0]
    content = choice.message.content
    if content:
        return content
    if choice.finish_reason == "length":
        reasoning = getattr(choice.message, "reasoning", None) or ""
        raise EmptyReply(
            "被 max_tokens 截断在推理阶段，没有正文：这个模型要先输出 reasoning "
            "再输出正文，额度给小了会全部耗在推理上。把 max_tokens 调大（建议 ≥1500）。"
            f"推理片段：{reasoning[:120]}"
        )
    return ""


def ask(model_key: str, prompt: str, system: str = None,
        max_tokens: int = None, temperature: float = None) -> str:
    """一次性问一个模型，返回完整回复文本（非流式）。

    验证「两个模型能在同一个进程里互不干扰地轮流说话」用这个最省事。
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    response = call_model(model_key, messages, max_tokens=max_tokens,
                          temperature=temperature)
    return extract_text(response)