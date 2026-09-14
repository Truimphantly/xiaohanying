"""
配置管理模块
支持通过 .env 配置任意数量的模型（OpenAI 兼容接口），
网页的「切换模型」下拉框与讨论模式的参与者会自动跟随这里变化。

用法：在 .env 里写
    MODEL_COUNT=2
    MODEL_1_KEY=...  MODEL_1_NAME=...  MODEL_1_API_KEY=...
    MODEL_1_BASE_URL=...  MODEL_1_MODEL_ID=...  MODEL_1_VISION=...
    MODEL_2_...
任何 OpenAI 兼容接口（DeepSeek / SenseNova / OpenAI / Kimi / 智谱 / 本地 vLLM 等）都能用，
只要填对 API_KEY、BASE_URL、MODEL_ID 三样，网页就会自动出现这个模型。
"""

import os
from dotenv import load_dotenv

# override=True：.env 文件为唯一权威配置来源，
# 防止终端/系统里残留的同名环境变量覆盖 .env
load_dotenv(override=True)


def _parse_bool(value) -> bool:
    """把 'true'/'1'/'yes'/'on' 解析成布尔值，其余都当 False"""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _build_models() -> dict:
    """从 .env 构建模型字典。

    优先读通用的 MODEL_1..MODEL_N 方案；一个都没写时，回退到旧的
    DEEPSEEK_*/SENSENOVA_* 两个内置模型（兼容老配置，不影响老用户）。
    """
    models = {}

    count = int(os.getenv("MODEL_COUNT", "0") or "0")
    for i in range(1, count + 1):
        p = f"MODEL_{i}_"
        key = os.getenv(p + "KEY", "").strip()
        name = os.getenv(p + "NAME", "").strip()
        model_id = os.getenv(p + "MODEL_ID", "").strip()
        # KEY / NAME / MODEL_ID 三样齐全才算配好一个模型；API_KEY 留空会在调用时报错
        if not (key and name and model_id):
            continue
        models[key] = {
            "key": key,
            "name": name,
            "api_key": os.getenv(p + "API_KEY", "").strip(),
            "base_url": os.getenv(p + "BASE_URL", "").strip() or "https://api.openai.com/v1",
            "model_id": model_id,
            "supports_vision": _parse_bool(os.getenv(p + "VISION", "false")),
        }

    # 兼容旧版 .env：没写 MODEL_* 时，回退到 DeepSeek / SenseNova 两个内置模型
    if not models:
        deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        models["deepseek"] = {
            "key": "deepseek",
            "name": "DeepSeek V3",
            "api_key": deepseek_key,
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "model_id": os.getenv("DEEPSEEK_MODEL_ID", "deepseek-chat"),
            "supports_vision": False,
        }
        models["sensenova"] = {
            "key": "sensenova",
            "name": "SenseNova",
            "api_key": os.getenv("SENSENOVA_API_KEY", "").strip(),
            "base_url": os.getenv("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
            "model_id": os.getenv("SENSENOVA_MODEL_ID", "sensenova-6.8-flash-lite"),
            "supports_vision": True,
        }
        # DeepSeek 视觉实验版，和 deepseek 共用同一个 key
        models["deepseek-vision"] = {
            "key": "deepseek-vision",
            "name": "DeepSeek V4 Flash Vision (Exp)",
            "api_key": deepseek_key,
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "model_id": os.getenv("DEEPSEEK_VISION_MODEL_ID", "deepseek-v4-flash-vision-exp"),
            "supports_vision": True,
        }

    return models


MODELS = _build_models()

# 一个模型都没配时的兜底：让服务器能正常启动、前端显示「未配置模型」，
# 而不是在 import 阶段就崩掉。真去调用时会因为 model_id 为空而报错。
_EMPTY_MODEL = {
    "key": "",
    "name": "未配置模型",
    "api_key": "未配置",
    "base_url": "https://api.openai.com/v1",
    "model_id": "",
    "supports_vision": False,
}

# 当前激活的模型 key（模块级全局变量，可动态修改）。
# 默认取 .env 的 DEFAULT_MODEL；没配就取第一个模型。
_first_key = next(iter(MODELS), "")
_current_model = os.getenv("DEFAULT_MODEL", "").strip() or _first_key

# 上传目录
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")

# 单轮任务最大执行步数（工具调用轮次）。
# 复杂任务（如生成网页游戏）很耗步数，默认 50；可在 .env 里用 AGENT_MAX_STEPS 调整
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "50"))

# Agent 循环中 LLM 调用的温度。
# 工具调用（决定调哪个工具、拼 JSON 参数）需要确定性，高温容易乱调工具，
# 所以比纯聊天的 0.7 低；可在 .env 里用 AGENT_TEMPERATURE 调整
TEMPERATURE = float(os.getenv("AGENT_TEMPERATURE", "0.3"))


def get_model(model_key: str) -> dict:
    """按 key 取模型配置，不受「当前激活模型」这个全局状态影响。

    讨论模式（多个模型同处一轮对话）必须用这个，而不是 get_active_model()：
    后者读的是模块级 _current_model，谁都能改，两个模型轮流发言时会互相打架。

    与 get_active_model() 的静默回退不同，这里未知 key 直接报错 —— 否则
    参与者名字写错一个字母，就会静默变成同一个模型，讨论退化成自己跟自己说话，
    表面上还跑得好好的。
    """
    if model_key not in MODELS:
        raise ValueError(
            f"未知模型: {model_key}（可用: {'、'.join(MODELS)}）"
        )
    return MODELS[model_key]


def get_active_model() -> dict:
    """返回当前激活模型的完整配置"""
    if _current_model in MODELS:
        return MODELS[_current_model]
    if MODELS:
        return next(iter(MODELS.values()))
    return _EMPTY_MODEL


def get_active_model_key() -> str:
    """返回当前激活模型的 key"""
    if _current_model in MODELS:
        return _current_model
    return next(iter(MODELS), "")


def set_active_model(model_key: str) -> bool:
    """切换当前激活模型，成功返回 True"""
    global _current_model
    if model_key in MODELS:
        _current_model = model_key
        return True
    return False


def get_model_list() -> list:
    """返回所有可用模型列表"""
    return [
        {"key": k, "name": v["name"], "vision": v.get("supports_vision", False)}
        for k, v in MODELS.items()
    ]


def get_discussion_role_model(role_key: str) -> str:
    """讨论角色（engineer / critic / moderator）→ 模型 key 的映射。

    优先读 .env 里的 DISCUSSION_<ROLE>_MODEL 显式指定；没指定时自动分配：
    engineer / moderator 用第 1 个模型，critic 用第 2 个（只有一个模型就都用它）。
    这样用户换成任意模型后，讨论模式也能自动跟着走，不必改 prompts.py。
    """
    keys = list(MODELS.keys())
    override = os.getenv(f"DISCUSSION_{role_key.upper()}_MODEL", "").strip()
    if override in MODELS:
        return override
    if role_key == "critic" and len(keys) > 1:
        return keys[1]
    return keys[0] if keys else ""


def validate() -> bool:
    """验证配置（预留）"""
    return True
