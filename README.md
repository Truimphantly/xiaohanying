# 是小寒影呀

是一只**本地运行**的猫娘 AI 助手：网页聊天 + 工具调用（Agent）。
她能读写文件、抓网页、
执行 Python 代码、用 Blender 做 3D 建模（虽然只能做简单模型）（但是有视觉！！！），还能拉几个模型「开会讨论」。
所有数据只在本地跑，有些文件你也不愿意上传吧zakozako
~
是焦虑的产物。昨晚之后人好多了喵。
---

## 顺手

- **方便**：双击 `启动小寒影.bat` 一键启动，自动检查依赖、自动开浏览器，不用配任何环境。
- **响应快**：网页聊天用 SSE 流式输出，打字机一样逐字吐字，不用等整段生成完。
- **顺手**：一个网页里就能聊天、传图、切模型、切人格、开讨论；前端是单文件原生 JS，
  改完刷新即生效，没有构建步骤。
- **讨论模式**：🗣 一键拉几个模型互相挑错、辩论，几轮后收敛出一份可执行的结论；
  中途能 ⏸ 暂停插话、▶ 继续、换个议题重开，结论能一键交给助手去执行。
- **回滚保护**：任务中途停止或报错，会把这次改动的文件自动回滚到任务前，不留半成品。
- **Blender 实时模式**：装个 Blender、在 Blender 里跑一下 `scripts/blender_live_server.py`，
  就能在网页里「边聊边建模」——你说「再加个球」「把它变红」，模型就在你眼前的 Blender
  窗口里实时改出来，场景持续保留、可增量修改；不装 Blender 也能用后台模式一次性生成 `.blend`。
- **越界保护**：助手默认只能动项目文件夹里的文件，想写外面会弹「允许一次 / 拒绝」。
- **桌宠**：一只会旁听对话、跟着思考/说话/干活切表情的猫娘分身，单击互动、双击填提示词。

## 关于模型：

内置示例是 DeepSeek，但**不绑定**。任何 **OpenAI 兼容接口**都能用：
DeepSeek、商汤 SenseNova、OpenAI、Moonshot Kimi、智谱、通义、本地 vLLM/Ollama……

只要在 `.env` 里填好「API Key + 接口地址 + 模型名」三样，网页的「切换模型」下拉框和
讨论模式的参与者就会**自动跟着变**，不用改一行代码。

> 你填什么模型、网页就显示什么模型；是否支持读图也由 `.env` 里的 `VISION` 决定。

## 快速开始（Windows）

1. 装 Python 3.10+：
   ```bat
   winget install -e --id Python.Python.3.11 --source winget
   ```
   装完重开一个 cmd，`py -3.11 --version` 能打印版本号即可。
2. 装依赖（在项目根目录）：
   ```bat
   py -3.11 -m pip install -r requirements.txt
   ```
3. 配 API：复制 `.env.example` 为 `.env`，填你自己的 Key（见下）。
4. 双击 `启动小寒影.bat`，浏览器自动打开 `http://127.0.0.1:5000`。

或者手动启动：
```bat
cd src
py -3.11 main.py
```

##  关于API：

`.env` 已加入 `.gitignore` 不会上传，仓库里只有
`.env.example` 模板。请把 `.env.example` 复制成 `.env`，填**你自己的** DeepSeek 或
其它模型 API Key。

`.env` 里这样配模型（想加几个模型就写几个）：

```env
# 默认启动用哪个模型（填下面某个 KEY）
DEFAULT_MODEL=deepseek

# 一共配了几个模型
MODEL_COUNT=2

# ---- 模型 1 ----
MODEL_1_KEY=deepseek
MODEL_1_NAME=DeepSeek
MODEL_1_API_KEY=sk-你的key
MODEL_1_BASE_URL=https://api.deepseek.com
MODEL_1_MODEL_ID=deepseek-chat
MODEL_1_VISION=false

# ---- 模型 2 ----
MODEL_2_KEY=glm
MODEL_2_NAME=智谱 GLM-4
MODEL_2_API_KEY=你的key
MODEL_2_BASE_URL=https://open.bigmodel.cn/api/paas/v4
MODEL_2_MODEL_ID=glm-4-plus
MODEL_2_VISION=true
```

字段说明：

| 字段 | 含义 |
|---|---|
| `MODEL_n_KEY` | 这个模型的英文标识（网页里识别的 key） |
| `MODEL_n_NAME` | 网页下拉框里显示的名字 |
| `MODEL_n_API_KEY` | 你的 API Key |
| `MODEL_n_BASE_URL` | 接口地址（OpenAI 兼容） |
| `MODEL_n_MODEL_ID` | 模型名，如 `deepseek-chat`、`glm-4-plus` |
| `MODEL_n_VISION` | 是否支持读图（`true`/`false`） |

讨论模式里谁用哪个模型，也能指定（不填会自动分配：第 1 个模型当工程师/总结者，第 2 个当挑刺评审）：

```env
DISCUSSION_ENGINEER_MODEL=deepseek
DISCUSSION_CRITIC_MODEL=glm
DISCUSSION_MODERATOR_MODEL=deepseek
```

改完 `.env` **重启服务**才生效。

## 关于讨论模式：

- 切到「讨论模式」，在「模型讨论」栏勾选参与的角色，输入议题。
- 第 1 轮各模型独立给方案（互相看不到），后面几轮互相挑刺，最后总结者收敛成一份结论。
- 途中 ⏸ 暂停后可以插话补充要求，▶ 继续让剩下的人看到你的话。
- 结论不满意就「换个议题重开」；结论可用后，交给聊天模式让助手去执行。

## 能干什么

-  文件操作（读写、列目录）
-  网页抓取 / 调用 API
-  执行 Python 代码
-  Blender 3D 建模（后台生成 + 实时模式）

## 注意事项

- 5000 端口是聊天界面专用，让助手生成其它网站时提醒它换 8000-8999 端口。
- Blender 实时建模需要另装 Blender，并在 Blender 里运行 `scripts/blender_live_server.py`；
  不装不影响其它功能。
- 关掉黑窗口 = 关闭助手；重启后聊天历史会清空。

## 常见问题

- 启动报端口占用：关掉旧窗口再试，或 `netstat -ano | findstr :5000` 找进程结束。
- 报 API 错误：多半是 Key 没填、填错、额度用完或网络问题。
- `py` 找不到：Python 没装成功，重装并勾选 Add to PATH。

## 目录结构

```
src/            后端（Flask + SSE + Agent 引擎 + 讨论引擎）
static/         前端 Markdown 渲染（marked.js + DOMPurify）
scripts/        Blender 实时建模服务脚本
requirements.txt
启动小寒影.bat  一键启动
.env.example    配置模板（复制成 .env 填你自己的 Key）
```

## License
