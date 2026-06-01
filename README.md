# 小团 BuYu Demo

美团/大众点评本地生活 Agent demo。预设场景走稳定 mock 流程，自定义输入走 LLM 编排与本地 mock 工具链。

## Setup

```bash
uv sync --extra dev
cp .env.example .env
```

编辑 `.env`，填入 `PLANNER_KEY`、`PLANNER_BASE_URL` 和 `PLANNER_MODEL`。

## Run

```bash
uv run uvicorn app.server:app --host 127.0.0.1 --port 8010
```

打开 `http://127.0.0.1:8010/`。

## Test

```bash
uv run pytest -q
```
