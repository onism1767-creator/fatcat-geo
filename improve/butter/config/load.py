"""读默认值文件(外壳:只读)。defaults.yaml 手写;defaults_noise.yaml 由 E0 的导出脚本生成,不手改。"""

from __future__ import annotations

from pathlib import Path

import yaml

from butter.config.core import Defaults, parse_defaults
from butter.contracts.llm import Pricing
from butter.llm.pricing import parse_pricing

HERE = Path(__file__).resolve().parent
DEFAULT_FILES = (HERE / "defaults.yaml", HERE / "defaults_noise.yaml")
PRICING_FILE = HERE / "pricing.yaml"


def load_defaults(paths: tuple[Path, ...] = DEFAULT_FILES) -> Defaults:
    values = []
    for path in paths:
        values += parse_defaults(yaml.safe_load(Path(path).read_text(encoding="utf-8")), origin=str(path))
    return Defaults(values)


def load_pricing(path: Path = PRICING_FILE) -> Pricing:
    return parse_pricing(yaml.safe_load(Path(path).read_text(encoding="utf-8")), origin=str(path))
