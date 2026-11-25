"""读 Toast 的任务和报告。

真正干活的是同目录下的 `_toast_view.py`,它在 Toast 自己的解释器里跑,用 Toast 自己的函数组句。
这里只负责把它叫起来:连接设成只读,Toast 的文件夹放进导入路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fatcat.modules import Module, ModuleError, run_readonly

_VIEW_SCRIPT = Path(__file__).with_name("_toast_view.py")
_READ_ONLY = {"PGOPTIONS": "-c default_transaction_read_only=on"}  # 数据库连接整个设成只读


def _call(module: Module, *args: str) -> Any:
    try:
        return run_readonly(module, [str(_VIEW_SCRIPT), *args], pythonpath=module.require_path(), env=_READ_ONLY)
    except ModuleError as exc:
        if "连不上数据库" in str(exc) or "StorageUnavailable" in str(exc):
            raise ModuleError(
                "Toast 的数据库没有开。在 Toast 的文件夹里运行:"
                "powershell -ExecutionPolicy Bypass -File scripts/pgdev.ps1 start"
            ) from exc
        raise


def list_jobs(module: Module) -> list[dict[str, Any]]:
    """库里的每一单:单号、状态、品牌、官网、出过几版报告。"""
    return list(_call(module, "list"))


def load_view(module: Module, job_id: str) -> dict[str, Any]:
    """一单已完成任务的全部内容,文字是 Toast 的原话,中英两套(键 text.en / text.zh)。"""
    return dict(_call(module, "view", job_id))
