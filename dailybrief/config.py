"""Load .env + config.yaml into a single Config object."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .util import ROOT


@dataclass
class Config:
    raw: dict[str, Any]
    env: dict[str, str]

    # --- convenience accessors ---
    def get(self, *keys, default=None):
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def require_env(self, name: str) -> str:
        val = (self.env.get(name) or "").strip()
        # placeholders in .env.example all end with "..." ; real keys never do
        if not val or val.endswith("...") or val in {"...", "<fill>"}:
            raise RuntimeError(
                f"Missing/placeholder env var {name}. Fill it in .env "
                f"(see .env.example)."
            )
        return val

    @property
    def tz(self) -> str:
        return self.get("general", "timezone", default="Europe/Warsaw")

    @property
    def target_minutes(self) -> int:
        return int(self.get("general", "brief_target_minutes", default=40))

    @property
    def wpm(self) -> int:
        return int(self.get("general", "words_per_minute", default=145))

    @property
    def weekend_lookback(self) -> bool:
        return bool(self.get("general", "weekend_lookback", default=True))

    @property
    def sections(self) -> list[dict]:
        return self.get("sections", default=[])


def _deep_merge(base: Any, over: Any) -> Any:
    """Recursively overlay `over` on `base`. Dicts merge key-by-key; lists and
    scalars in `over` replace those in `base`. Used by the `extends:` mechanism so
    a child config (e.g. config_weekly.yaml) only declares its overrides."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else v
        return out
    return over


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = path or (ROOT / "config.yaml")
    raw = _load_yaml(cfg_path)
    # `extends: <file>` overlays this config on a base one (relative to this file).
    base_name = raw.pop("extends", None) if isinstance(raw, dict) else None
    if base_name:
        base = _load_yaml(cfg_path.parent / base_name)
        raw = _deep_merge(base, raw)
    env = {k: v for k, v in os.environ.items()}
    return Config(raw=raw, env=env)
