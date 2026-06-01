# 小团 BuYu · 美团 AI Hackathon 2026 赛题06

这是提交给 **美团 AI Hackathon 2026 赛题06「本地探索 · 周末闲时活动规划」** 的完整实现代码。

小团面向「周末半天不知道去哪儿」这一高频本地生活场景，把一句含糊需求转化为一条 **可确认、可解释、可执行** 的本地生活方案。系统不是只生成推荐文案，而是把 LLM 放在规划中枢：在真实商户候选、实时可用性与用户反馈之间持续权衡，并把推荐、确认与执行连成闭环。

- 项目设计文档：[小团 · 美团 AI Hackathon 设计文档](https://jxtse.github.io/projects/meituan-ai-hackathon/)
- 在线交互 Demo：[https://dan-newest-grant-headlines.trycloudflare.com/](https://dan-newest-grant-headlines.trycloudflare.com/)
- GitHub 仓库：[https://github.com/jxtse/buyu-meituan](https://github.com/jxtse/buyu-meituan)

## 核心能力

### 1. 渐进式 Planning

规划不是一次性吐出整张行程，而是 LLM 作为 tools orchestrator，在 ReAct 循环中分段、软固定、渐进式确认地推进：

1. `locate`：读取当前位置与商圈，作为距离计算原点。
2. `intent`：自然语言转结构化约束，包括场景、人群、饮食、时间窗、预算与偏好。
3. `plan`：软固定三段「玩 → 吃 → 额外」，按约束灵活增删。
4. `search + reason`：逐段检索真实候选，结合 memory 权衡，只出一张可执行推荐卡。
5. `execute`：用户确认完整方案后一键执行，逐站调用下单工具。

默认框架是 `play → eat → extra`，但会按场景自适应：情侣约会可以把 extra 设计成鲜花惊喜，家庭带娃可以是亲子手作或饭后散步，朋友聚会可以是续摊收尾。每段只给用户一张卡，用户通过「接受 / 换一个 / 看详情 / 补充需求」渐进式推进。

### 2. Mock 美团/大众点评工具链

本项目不接真实美团交易接口，使用本地 mock API 模拟完整本地生活工具链，保证评审现场可复现、低延迟、无真实交易风险。

| 阶段 | 工具 | 作用 |
| --- | --- | --- |
| 定位 | `meituan_locate` | 返回当前坐标、城区与商圈 |
| 检索 | `dianping_search` | 按段、场景、排除集召回地点候选 |
| 详情 | `dianping_detail` | 返回商户详情、团购、评论、种草帖和图集 |
| 查位 | `meituan_query_queue` | 模拟实时排队与可订桌状态 |
| 执行 | `meituan_book_table` / `meituan_buy_ticket` / `meituan_order_delivery` | 模拟订座、购票、外卖惊喜下单 |
| 展示 | `show_recommendation_card` | 把 Agent 决策结果弹成手机内推荐卡 |
| 对话 | `agent_speak` | Agent 主动向用户追问或解释 |
| 记忆 | `agent_memory_write` / `agent_memory_read` | 写入并读取本轮会话中的偏好、选择和约束 |

右侧 Agent 面板通过 `thinking / stage / tool_call / tool_result / card / execute_done` 事件展示推理链路。公网环境下，为避免 SSE 被代理缓冲，关键预设和用户点击动作会随接口响应返回 `agent_events`，由前端按时间线回放，保证链路与卡片生成同步。

### 3. 低负担需求收集

自定义输入不是一次性强制用户写完整需求。用户可以先说「hi」「散步」「找个地方坐一会儿」这类模糊表达，Agent 会进入 intake 阶段：

- 通过 `agent_memory_write` 记录用户已经表达的目标、同行人、时间、区域和偏好。
- 缺信息时用 `agent_speak` 追问。
- 同时在手机界面弹出紧凑选择卡，让用户直接点击「散步」「和朋友」「现在去」「哪里都行」等按钮。
- 用户也可以绕过按钮，继续自由输入。

当 goal、companions、time、area 足够形成可执行约束后，Agent 进入 LLM 意图解析和 mock 点评检索，并弹出第一张推荐卡。

### 4. 实时调整机制

规划与执行中遇到落地障碍时，目标是不中断流程、当场自动调整，并把每一次调整展示给用户。

| 类型 | 触发条件 | 处理策略 |
| --- | --- | --- |
| 模型降级 | LLM 调用失败或返回非法 JSON | 切换启发式规则兜底，流程继续 |
| 无座 | 查位无可订桌或等待过长 | 出卡前降权，执行期自动换同段候选 |
| 无票 | 购票工具返回售罄 | 自动替换同段候选并重试 |
| 时间冲突 | 同一时段已有预约 | 自动错峰重排 |
| 无候选 | 严格场景下检索为空 | 按兼容场景放宽检索，仍为空则跳过该段 |

例如家庭晚饭段会先考虑距离近的 Green Bowl，但查位发现无桌或等待过长后，Agent 会改选 Sprout Table，因为 5 岁孩子刚完成高参与度活动后继续等位会增加行程失控风险。这个过程会以工具调用和 thinking 事件呈现在右侧链路中。

### 5. 记忆与技能内化设计

小团的记忆分两层：

- **会话记忆**：运行时记录用户每次选择、拒绝、补充需求和偏好，让这一次出行越聊越懂。
- **参数化技能内化方向**：将跨场景复用的通用规划技能沉淀为模型的认知基础，例如分段规划范式、动线和体力常识、实时可用性检查、自动调整策略。

设计依据是：通用技能适合通过 LoRA + RL 内化为稳定能力，而实时排队、库存、价格等精确且易变的事实应该留给外部工具实时检索。结构化事实记忆可被 LoRA 以较少参数高效承载，但无规律精确记忆不适合硬塞进参数。

## 项目结构

```text
app/
  server.py                 FastAPI 路由与 SSE 事件流
  session.py                核心 Agent 状态机、intake memory、Planning、执行逻辑
  llm.py                    LLM 网关封装，支持 chat/completions 与 responses
  prompts.py                Intent / Recommendation / Chat prompt
  mock/
    meituan.py              美团/大众点评 mock API
    data/merchants.json     本地生活商户库
  static/                   前端 JS/CSS 与地点图片资源
  templates/index.html      Demo 页面
tests/                      回归测试
```

## 本地运行

### 1. 安装依赖

```bash
uv sync --extra dev
cp .env.example .env
```

编辑 `.env`：

```bash
PLANNER_KEY=your-key
PLANNER_BASE_URL=http://127.0.0.1:18150
PLANNER_MODEL=claude-opus-4.8
```

`AMAP_KEY` 可以留空。本项目地图、定位、地点检索和交易执行均走 mock API。

### 2. 启动服务

```bash
uv run uvicorn app.server:app --host 127.0.0.1 --port 8010
```

打开：

```text
http://127.0.0.1:8010/
```

### 3. 运行测试

```bash
uv run pytest -q
```

当前提交验证结果：

```text
45 passed
```

## 评审说明

- 首页 4 个预设场景使用稳定 mock-only 路径，保证首卡快速出现，右侧 Agent 链路同步回放。
- 用户在输入框自由输入时才调用真实 LLM API。
- 自定义 query 会进入 intake Agent，先通过选择卡和对话补齐需求，再进入工具检索和弹卡。
- 所有交易动作均为 mock，不会产生真实订单。
