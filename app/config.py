"""配置入口：读取 .env + 进程环境变量。

只需要两项：
- PLANNER_KEY / KIMI_API_KEY / MOONSHOT_API_KEY  —— LLM 网关 key（必填）
- PLANNER_BASE_URL                               —— LLM 网关地址（缺省走 Kimi）

AMAP_KEY 不再必填：本项目全程使用美团/点评 Mock API，地图与定位也走 Mock。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PLANNER_BASE_URL = "https://api.moonshot.cn"
DEFAULT_MODEL = "kimi-k2.6"
# 南京·建邺区·金陵天地（河西商圈），demo 默认用户落点
DEFAULT_LOCATION = "118.7372,32.0148"


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    amap_key: str = ""
    model: str = DEFAULT_MODEL
    default_location: str = DEFAULT_LOCATION


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_config(*, env_path: Path | None = None) -> Config:
    # 优先找仓库根 .env，其次项目内 .env
    candidates = [env_path] if env_path else [
        Path(".env"),
        Path(__file__).resolve().parents[1] / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    file_vals: dict[str, str] = {}
    for c in candidates:
        if c and c.exists():
            file_vals = _parse_env_file(c)
            break

    def pick(name: str, default: str | None = None) -> str | None:
        return os.environ.get(name) or file_vals.get(name) or default

    api_key = (
        pick("PLANNER_KEY")
        or pick("KIMI_API_KEY")
        or pick("MOONSHOT_API_KEY")
        or pick("OPENAI_NEXT_API_KEY")
    )
    amap_key = pick("AMAP_KEY", "") or ""
    base_url = pick("PLANNER_BASE_URL", DEFAULT_PLANNER_BASE_URL)
    model = pick("PLANNER_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    default_loc = pick("DEFAULT_LOCATION", DEFAULT_LOCATION) or DEFAULT_LOCATION

    if not api_key:
        raise RuntimeError(
            "missing LLM key: set PLANNER_KEY, KIMI_API_KEY, "
            "MOONSHOT_API_KEY, or OPENAI_NEXT_API_KEY "
            "in .env or process env.")
    assert base_url
    return Config(api_key=api_key, base_url=base_url, amap_key=amap_key, model=model,
                 default_location=default_loc)
