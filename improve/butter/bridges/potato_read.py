"""读取桥:由 Potato 自己的解释器来跑,用 Potato 官方的读接口把一次运行读出来,打印成一段 JSON(设计 ADR-B03)。

    <Potato 的 python> -B potato_read.py <data-dir> <run_id>

- 这个文件不被 Butter import,也不 import Butter;它是唯一一处 import aivis 的地方,而且只在 Potato 的解释器里执行。
- 只读 Butter 自己批次目录下的 Potato 数据目录。Potato 的读接口在构造时会建目录,所以绝不拿它去碰别处。
- 提及判定用 Potato 存在 extraction.json 里的结果(只算清晰命中),Butter 不重做一遍。
- 运行没跑完时没有 manifest 和 extraction:照实返回 null / 空,不猜。
"""
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法:potato_read.py <data-dir> <run_id>", file=sys.stderr)
        return 2
    data_dir, run_id = Path(argv[0]), argv[1]
    if not (data_dir / "raw").is_dir():
        print(f"没有这个数据目录:{data_dir}", file=sys.stderr)
        return 2
    from aivis.storage import AnalysisStore, RawStore

    raw = RawStore(data_dir / "raw")
    manifest = next((m for m in raw.read_manifests() if m.run_id == run_id), None)
    lock = next((x for x in raw.read_locks() if x.run_id == run_id), None)
    responses = [{"question_id": r.question_id, "run_index": r.run_index, "finished_ok": bool(r.finished_ok),
                  "error_code": r.error_code, "search_count": int(r.search_count or 0)}
                 for r in raw.read_responses(run_id)]
    mentions, owned = [], []
    has_extraction = (data_dir / "analysis" / run_id / "extraction.json").is_file()  # 先看在不在:读接口会顺手建目录
    if has_extraction:
        extraction = AnalysisStore(data_dir / "analysis").read_extraction(run_id)
        mentions = [{"question_id": m.question_id, "run_index": m.run_index, "brand": m.brand,
                     "mentioned": bool(m.mentioned), "counted": bool(m.counted)} for m in extraction.mentions]
        owned = [{"question_id": c.question_id, "run_index": c.run_index, "brand": c.brand}
                 for c in extraction.citations if c.brand and c.counted and c.owned_or_earned == "owned"]

    def pick(obj, names):
        return None if obj is None else {name: getattr(obj, name, None) for name in names}

    out = {
        "run_id": run_id,
        "manifest": pick(manifest, ("provider", "model", "surface_id", "tool_version", "config_fingerprint", "n_runs",
                                    "locale", "region", "prompt_version", "competitor_set_version", "timestamp",
                                    "cost_usd_actual")),
        "lock": pick(lock, ("provider", "model", "tool_version", "config_fingerprint", "n_runs", "prompt_version",
                            "competitor_set_version")),
        "responses": responses, "has_extraction": has_extraction, "mentions": mentions, "owned_citations": owned,
    }
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
