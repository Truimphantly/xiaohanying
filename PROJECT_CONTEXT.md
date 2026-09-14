# 项目背景（多模型讨论时自动附给每个参与者）

讨论引擎没有工具，读不到磁盘上的文件，只能靠这份说明了解项目。不给它背景，
模型就会凭空想象一个通用 Python 项目（实测中它讨论的是「core/discuss.py」和
「本地 7B 模型单卡加载」——本项目里这些全都不存在），吵得再认真结论也用不上。

**改动项目结构后记得同步更新这里**，否则模型会基于过期的描述给方案。

## 这是什么

「小寒影」是一个**本地运行**的 AI 助手：Flask 网页聊天界面 + 工具调用的 Agent。
所有数据只在本机跑。模型全部走 API（不跑本地模型），由 `.env` 里的 `MODEL_*` 配置，
任何 OpenAI 兼容接口都能用（DeepSeek / 商汤 SenseNova / OpenAI / Kimi / 智谱 / 本地 vLLM 等）。
默认示例挂了两个：

- **DeepSeek**：`deepseek-chat`，便宜快，不支持读图
- **商汤 SenseNova**：`sensenova-6.8-flash-lite`，
  这是一个**推理模型**（先输出 reasoning 再输出正文），支持读图

模型是否支持读图由 `.env` 里每个模型的 `MODEL_n_VISION` 决定；网页的「切换模型」
下拉框和讨论模式参与者会自动跟随 `.env` 变化，不必改代码。

## 目录结构

```
D:\ai工作流\
├─ src\                          后端
│  ├─ main.py                    Flask 服务器 + 所有 HTTP 路由 + SSE 推送
│  ├─ workflow_engine.py         核心：单模型 Agent 循环（思考→调工具→观察→再思考）
│  ├─ discussion_engine.py       多模型讨论引擎（纯文字，不调工具）
│  ├─ config.py                  模型配置 + 全局激活模型 + 上下文预算等常量
│  ├─ llm_client.py              LLM 调用封装（含无状态的多模型调用接口）
│  ├─ prompts.py                 所有系统提示词：人设、讨论角色
│  ├─ file_journal.py            文件台账：任务中止时把工作区回滚到任务前
│  ├─ tools\                     工具实现：文件读写 / 网页抓取 / 执行 Python / Blender
│  ├─ templates\index.html       前端单页（约 100KB，原生 JS，无框架，含桌宠）
│  └─ .claude\
├─ static\                       marked.js + DOMPurify（前端 Markdown 渲染与消毒）
├─ output\                       生成的产物，通过 /output/ 路由用 http 访问
├─ uploads\                      用户上传的文件
├─ .env                          密钥与配置（MODEL_* 模型列表），不入库；模板见 .env.example
├─ PROJECT_CONTEXT.md            本文件
└─ requirements.txt
```

## 技术栈

- Python 3.10 及以上都能跑（本机实际跑在 3.14），Flask，openai SDK（两个厂商都是 OpenAI 兼容接口）
- 前端：**单文件原生 JS，没有构建步骤**，没有 React/Vue，改完直接刷新即可
- 通信用 SSE（Server-Sent Events），不是 WebSocket

## 桌宠（小寒影的分身）

`src/templates/index.html` 里的纯前端角色，落在**左侧聊天面板内部**（`.pet-layer` → `#pet`，
相对 `.chat-panel` 做 absolute 定位）。它只**旁听**已经推出来的 SSE 事件来切表情：
不改后端、不改 SSE 格式、不新增接口。

| 事件 | 桌宠表现 |
|---|---|
| `thinking` / `notice` | 思考（眼神上移、歪头、飘省略号） |
| `text_delta` | 说话（嘴部开合） |
| `tool_call` / `tool_result` | 干活（星光、尾巴加速，气泡显示「翻目录 / 写文件…」） |
| `approval_request` | 干活 + 琥珀色呼吸警示，气泡提示「等你点允许」 |
| `complete` | 眯眼笑一下，然后回空闲 |
| `error` | 抖一下 + 难过气泡，然后回空闲 |
| 讨论流 `round` / `speaker_start` / `reasoning_delta` / … | 思考；`paused` → 空闲；`complete` → 庆祝 |

挂点：`sendMessage()` 与 `startDiscussion()` 里各一行 `es.addEventListener('message', …)`，
与页面自己的 `es.onmessage` 并行触发、互不干扰。另有对 `setStatus()` 的包装做粗粒度兜底，
保证「忙 → 空闲」一定回得来。

交互：单击互动（随机动作 + 台词）、**双击**把一条预设提示词填进输入框（可改可删，不自动发送）、
按住可拖动并记忆位置（`localStorage: xiaohanying_pet_pos`）、空闲时随机眨眼/抖耳/蹦跳/伸懒腰/说话。

排查提示：`main.py` 是 `debug=False`，Jinja 会缓存模板 —— **改完 `index.html` 必须重启服务**才生效。
造型源文件与验收工具在 `_pet_preview\`：`preview.html` 出图、`ascii_view.py` 把渲染图转成彩色 ASCII
（便于无图形环境下看造型）、`measure.py` 量几何、`review.py` 调视觉模型看图点评、
`resync_svg.py` 把改好的造型同步回页面。

## 必须知道的几条约束

1. **单任务锁**：`main.py` 用 `_register_task()` 保证同一时刻只有一个任务在跑。
   原因是 `file_journal` 是**进程级单例**，两个任务同时跑会互相清空回滚记录。
2. **多模型不能依赖全局激活模型**：`config.set_active_model()` 改的是模块级全局，
   会影响到所有正在跑的任务。多模型场景一律用 `config.get_model(key)` 显式取值。
3. **推理模型的 max_tokens 陷阱**：reasoning 长度极不稳定（同一个问题实测
   458~3880 字都出现过），额度给小了会随机性地只拿到推理、拿不到正文。
4. **端口约定**：5000 是聊天界面专用，生成的其它服务必须用 8000-8999，否则互相顶掉。
5. **越界保护**：Agent 默认只能动项目文件夹内的文件，写外面要用户点「允许」。
