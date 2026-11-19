"""证据注册表:Toast 的 evidence_registry.yaml + Butter 扩展,按 Toast 同样的规则校验、合并、按 ID 查(决定 7,设计 §11)。

- `ids`:条目 ID 常量(代码里引用条目一律用它,不手写字符串);
- `core`:纯逻辑(解析、检查、合并、查询);
- `load`:读文件(只读),找不到 Toast 时用自带快照。
验证状态不写进 YAML:Toast 的注册表没有状态字段,方法的验证状态由卡片库推出(设计 §11)。
"""

from butter.registry import ids
from butter.registry.core import Registry, RegistryError, check_entries, parse_entries
from butter.registry.load import (
    EXTENSION_PATH, SNAPSHOT_META_PATH, SNAPSHOT_PATH, content_sha256, default_toast_registry_path, load_registry,
    read_registry_file,
)

__all__ = [
    "EXTENSION_PATH", "SNAPSHOT_META_PATH", "SNAPSHOT_PATH", "Registry", "RegistryError", "check_entries",
    "content_sha256", "default_toast_registry_path", "ids", "load_registry", "parse_entries", "read_registry_file",
]
