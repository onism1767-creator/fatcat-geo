"""错误库(S10.1;架构书附录 v0.4.27 第 4b 节):config/errors.yaml 是用户会遇到的全部错误码的唯一出处——
代码、什么时候出现、用户看到的一句话、下一步、能不能「接着跑」,zh / en 两套。

页面组句只从这里取文案(表单校验、任务页出错态、/errors 对照页);代码里不硬写给用户看的句子。
测试机械把守:每个码双语齐全;代码里用到的码(表单、执行器、单次提问、熔断、网页、核验、审计)都在表里。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from engine.models import FrozenModel

DEFAULT_ERRORS_PATH = Path(__file__).resolve().parents[1] / "config" / "errors.yaml"
SCOPES = ("form", "account", "db", "job", "ai", "judge", "breaker", "page", "verify", "access")


class ErrorEntry(FrozenModel):
    code: str
    scope: str
    resumable: bool
    trigger: dict[str, str]
    message: dict[str, str]
    next_step: dict[str, str]
    causes: dict[str, str] | None = None  # 常见原因(运行类错误必填,测试把守;表单 / 网页类由 trigger 说明即可)

    def text(self, lang: str, *, with_next_step: bool, **params: str) -> str:
        """用户看到的句子:message(+ 下一步)。缺占位符就报错——出错处必须把参数填齐,不静默吞。"""
        parts = [self.message[lang].format(**params)]
        if with_next_step:
            parts.append(self.next_step[lang].format(**params))
        return " ".join(parts)


class ErrorLibrary(FrozenModel):
    entries: list[ErrorEntry]

    def get(self, code: str) -> ErrorEntry | None:
        return next((e for e in self.entries if e.code == code), None)

    def __getitem__(self, code: str) -> ErrorEntry:
        entry = self.get(code)
        if entry is None:
            raise KeyError(f"错误码 {code} 不在 config/errors.yaml 里")
        return entry

    def by_scope(self) -> list[tuple[str, list[ErrorEntry]]]:
        return [(scope, [e for e in self.entries if e.scope == scope]) for scope in SCOPES
                if any(e.scope == scope for e in self.entries)]


def load_errors(path: Path = DEFAULT_ERRORS_PATH) -> ErrorLibrary:
    """读错误库;重码、未知 scope、缺语言启动时直接炸,不带病运行。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    library = ErrorLibrary(entries=[ErrorEntry.model_validate(item) for item in data["errors"]])
    codes = [e.code for e in library.entries]
    if len(set(codes)) != len(codes):
        raise ValueError(f"错误码重复:{[c for c in codes if codes.count(c) > 1]}")
    for entry in library.entries:
        if entry.scope not in SCOPES:
            raise ValueError(f"错误码 {entry.code} 的 scope {entry.scope!r} 不在 {SCOPES} 里")
        for field in ("trigger", "message", "next_step", "causes"):
            texts = getattr(entry, field)
            if texts is None:
                continue
            if set(texts) != {"zh", "en"} or not all(v.strip() for v in texts.values()):
                raise ValueError(f"错误码 {entry.code} 的 {field} 必须 zh / en 都有且非空")
    return library
