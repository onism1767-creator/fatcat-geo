"""假 Potato 的读接口:只做 Butter 的读取桥用到的那几个方法,读的是同样的文件。

真的 Potato 里对应 `aivis.storage` 的 RawStore / AnalysisStore(构造时会建目录;这里照样建,行为一致)。
返回的对象用点号取字段,和真的一样。
"""
import json
from pathlib import Path
from types import SimpleNamespace


def _obj(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _obj(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_obj(v) for v in value]
    return value


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class RawStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def read_responses(self, run_id=None):
        return [_obj(r) for r in _lines(self._root / "raw_responses.jsonl") if run_id is None or r["run_id"] == run_id]

    def read_manifests(self):
        return [_obj(r) for r in _lines(self._root / "run_manifests.jsonl")]

    def read_locks(self):
        return [_obj(r) for r in _lines(self._root / "run_locks.jsonl")]


class AnalysisStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def read_extraction(self, run_id: str):
        return _obj(json.loads((self._root / run_id / "extraction.json").read_text(encoding="utf-8")))
