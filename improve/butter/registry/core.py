"""注册表的纯逻辑:解析、按 Toast 同样的规则检查、合并、按 ID 查。不读文件(读文件在 load.py)。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from butter.contracts import (
    ACCESS_CHECK_NAMES, DATA_CONDITIONS, EVIDENCE_LEVELS, OUTPUT_LEVELS, EvidenceEntry, RegistrySource,
)


class RegistryError(ValueError):
    """注册表文件不合规,或两份文件的条目 ID 撞车。"""


def parse_entries(data: Any, *, origin: str) -> list[EvidenceEntry]:
    """YAML 解析后的对象 → 条目列表。

    与 Toast `load_evidence_registry`(recommender.py:95-111)一样:只读顶层 `entries`,逐条按 EvidenceEntry 校验
    (多余字段、缺中英文字都会在这里报错),再做文件级检查。`origin` 只用于报错信息。
    """
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise RegistryError(f"{origin}:顶层要有 entries 列表")
    try:
        entries = [EvidenceEntry(**raw) for raw in data["entries"]]
    except (TypeError, ValueError) as exc:  # pydantic 的 ValidationError 是 ValueError 的子类
        raise RegistryError(f"{origin}:{exc}") from exc
    check_entries(entries, origin=origin)
    return entries


def check_entries(entries: list[EvidenceEntry], *, origin: str) -> None:
    """Toast 的文件级检查(recommender.py:98-110),逐条照搬:重复 ID、证据等级、官网检查名、数据条件。"""
    ids = [e.id for e in entries]
    if len(ids) != len(set(ids)):
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        raise RegistryError(f"{origin}:证据注册表存在重复 ID:{duplicated}")
    for entry in entries:
        if entry.evidence_level not in EVIDENCE_LEVELS:
            raise RegistryError(f"{origin}:{entry.id}:非法证据等级 {entry.evidence_level!r}")
        bad_checks = set(entry.access_checks) - ACCESS_CHECK_NAMES
        if bad_checks:
            raise RegistryError(
                f"{origin}:{entry.id}:未知的官网检查项 {sorted(bad_checks)}(可用:{sorted(ACCESS_CHECK_NAMES)})")
        unknown = set(entry.requires_data) - DATA_CONDITIONS
        if unknown:
            raise RegistryError(f"{origin}:{entry.id}:未知的数据条件 {sorted(unknown)}(可用:{sorted(DATA_CONDITIONS)})")


class Registry:
    """合并后的注册表(Toast 的条目在前,扩展条目在后),按 ID 查。条目 ID 撞车直接报错,不覆盖。"""

    def __init__(self, groups: Iterable[tuple[RegistrySource, list[EvidenceEntry]]]) -> None:
        groups = list(groups)
        by_id: dict[str, EvidenceEntry] = {}
        source_of: dict[str, RegistrySource] = {}
        for source, entries in groups:
            for entry in entries:
                if entry.id in by_id:
                    raise RegistryError(f"条目 ID 撞车:{entry.id} 同时出现在 {source_of[entry.id].path} 和 {source.path}")
                by_id[entry.id] = entry
                source_of[entry.id] = source
        self._by_id = by_id
        self._source_of = source_of
        self.sources: tuple[RegistrySource, ...] = tuple(source for source, _ in groups)

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, entry_id: object) -> bool:
        return entry_id in self._by_id

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    @property
    def entries(self) -> tuple[EvidenceEntry, ...]:
        return tuple(self._by_id.values())

    def get(self, entry_id: str) -> EvidenceEntry:
        try:
            return self._by_id[entry_id]
        except KeyError:
            raise KeyError(f"注册表里没有条目 {entry_id}") from None

    def source_of(self, entry_id: str) -> RegistrySource:
        self.get(entry_id)
        return self._source_of[entry_id]

    def suggestable(self, entry_id: str) -> bool:
        """能不能作为建议输出:A–D 级可以;E 级只存档(供检查器挂 ID),永不作为建议(Toast 同规则)。"""
        return self.get(entry_id).evidence_level in OUTPUT_LEVELS

    def missing(self, entry_ids: Iterable[str]) -> list[str]:
        """给一串 ID,返回注册表里查不到的(排好序);全查得到就是空列表。"""
        return sorted({i for i in entry_ids if i not in self._by_id})
