"""生成给 Toast 的提案文档(纯层:只出 Markdown 文本,由调用方落盘)。决定 4、决定 7;设计 §8.5。

三份:
  extension_proposal    新增类:Butter 扩展文件里的条目,Toast 采纳即合并。
  amendment_proposal    修改类:决定 7 列的那几项。条目 ID 拿 Toast 的注册表核对,对不上的照实指出。
  replication_proposal  按卡片结果:两次独立实验都变多 → 升级提案;都变少 → 降级提案。调几级由人定。

提案只是文档。Butter 不写 Toast 的任何文件;采不采纳、怎么改,都由人看过再定。
"""

from __future__ import annotations

from butter.contracts import EvidenceEntry, MethodDirection, MethodExperiment, MethodSummary
from butter.proposals.notes import ADDED_1114, AMENDMENTS, DECISION_7, SOURCE_06, WHY_ADD, Amendment
from butter.registry.core import Registry

TODO = "待补"
EXTENSION_FILE = "01-新增条目.md"
AMENDMENT_FILE = "02-修改现有条目.md"
REPLICATION_FILE = "03-按卡片结果的升级与降级.md"
README_FILE = "README.md"
LEVEL_TEXT = {"A": "官方文档 / 强共识", "B": "多研究支持", "C": "单一研究且有反证", "D": "描述性关联",
              "E": "无支持或禁止,只存档"}
CAVEATS = (
    "测到的是 AI 公司开放接口(API)上的结果,只代表那个引擎那个接口。",
    "变了,不等于是这次改动造成的(Observed change ≠ proven cause)。",
    "一批里只要有一条腿(语言 × 引擎)超出波动、且没有腿反方向超出,就记一次;几条腿一起看,碰巧超出的机会比单看一条大。",
)


def _cell(text: object) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out


def _origin_text(registry: Registry, entry_id: str) -> str:
    origin = registry.source_of(entry_id).origin
    return {"toast": "Toast 注册表", "toast_snapshot": "Toast 注册表(Butter 自带的快照)",
            "butter_extension": "Butter 扩展文件(Toast 还没有)"}[origin]


def _registry_note(registry: Registry) -> list[str]:
    lines = ["核对用的注册表:"]
    for source in registry.sources:
        what = {"toast": "Toast 本机文件", "toast_snapshot": f"Toast 快照({source.snapshot_taken_on} 取,{source.snapshot_of})",
                "butter_extension": "Butter 扩展文件"}[source.origin]
        lines.append(f"- {what}:{source.entries} 条,sha256 `{source.sha256[:12]}…`")
    return lines


# ── 一、新增类 ───────────────────────────────────────────────────────

def _why_add(entry_id: str) -> str:
    if entry_id in WHY_ADD:
        suggestion, basis, extra = WHY_ADD[entry_id]
        text = f"{SOURCE_06} 建议新增:「{suggestion}」;依据栏写的是「{basis}」"
        return text + (f"。{extra}" if extra else "")
    return ADDED_1114.get(entry_id, TODO)


def extension_proposal(registry: Registry, *, generated_on: str) -> str:
    entries = [e for e in registry.entries if registry.source_of(e.id).origin == "butter_extension"]
    toast_n = len(registry) - len(entries)
    lines = [
        "# 给 Toast 的提案(一):新增条目", "",
        f"生成日期:{generated_on}。来源:Butter 的 `butter/registry/registry_extension.yaml`({DECISION_7})。", "",
        f"一句话:建议 Toast 的证据注册表新增下面 {len(entries)} 条。格式和 Toast 的 `config/evidence_registry.yaml` "
        f"完全一样,已经按 Toast 的加载规则校验过,和 Toast 现有 {toast_n} 条没有重复的 ID;采纳时把扩展文件里的条目"
        "原样并进 `entries` 就行。Butter 不改 Toast 的文件,采不采纳由人定。", "",
        "## 一页速览", "",
    ]
    lines += _table(["#", "条目 ID", "级别", "阶段", "类型", "一句话(人话版)"],
                    [[i, f"`{e.id}`", e.evidence_level, e.stage, e.kind, e.plain["zh"]]
                     for i, e in enumerate(entries, 1)])
    lines += ["", "级别口径同 Toast:" + ";".join(f"{k} = {v}" for k, v in LEVEL_TEXT.items()) + "。",
              "E 级永不作为建议输出,只存档(Butter 的检查器拦下违规内容时挂这些 ID)。", ""]
    for i, e in enumerate(entries, 1):
        lines += [
            f"## {i}. `{e.id}`", "",
            f"- 级别:{e.evidence_level}({LEVEL_TEXT.get(e.evidence_level, TODO)});阶段:{e.stage};类型:{e.kind}",
            f"- 中文 · 主张:{e.hypothesis['zh']}",
            f"- 中文 · 人话版:{e.plain['zh']}",
            f"- English · hypothesis: {e.hypothesis['en']}",
            f"- English · plain: {e.plain['en']}",
            "- 来源:",
            *[f"  - {s}" for s in e.sources["zh"]],
            "- Sources:",
            *[f"  - {s}" for s in e.sources["en"]],
            f"- 为什么建议加:{_why_add(e.id)}",
        ]
        if e.guardrail:
            lines.append(f"- 护栏:{e.guardrail['zh']}")
        if e.contraindications.get("zh"):
            lines.append("- 不要这样用:" + ";".join(e.contraindications["zh"]))
        lines.append("")
    lines += ["---", "", *_registry_note(registry), ""]
    return "\n".join(lines)


# ── 二、修改类 ───────────────────────────────────────────────────────

def resolve_toast_id(registry: Registry, short: str) -> str | None:
    """决定 7 里的写法省了 EVIDENCE_ 前缀。只认 Toast 自己的条目(Butter 扩展里的不算 Toast 现有条目)。"""
    for candidate in (f"EVIDENCE_{short}", f"EVIDENCE_{short}_V1"):
        if candidate in registry and registry.source_of(candidate).origin != "butter_extension":
            return candidate
    return None


def _current(entry: EvidenceEntry) -> list[str]:
    lines = [
        f"  - 级别:{entry.evidence_level}",
        f"  - 主张:{entry.hypothesis['zh']}",
        "  - 来源:" + ";".join(entry.sources["zh"]),
    ]
    if entry.guardrail:
        lines.append(f"  - 护栏:{entry.guardrail['zh']}")
    if entry.contraindications.get("zh"):
        lines.append("  - 不要这样用:" + ";".join(entry.contraindications["zh"]))
    return lines


def _amendment_notes(item: Amendment, found: dict[str, EvidenceEntry], registry: Registry) -> list[str]:
    """文档里的建议和注册表现状对不上的地方,照实指出。"""
    notes = []
    if item.key == "FAQ_V1":
        for entry in found.values():
            if entry.evidence_level == "D":
                notes.append("Toast 里这条现在就是 D。06 §7 写「降为 D/E」,实际是「留在 D,或者降到 E」:"
                             "降不降、降到哪,由人定,这里不替人定")
            elif entry.evidence_level == "E":
                notes.append("Toast 里这条现在已经是 E,没有可再降的")
    if item.key.startswith("STATS_V1"):
        stats = found.get("STATS_V1")
        if stats and any("溯源" in text for text in stats.contraindications.get("zh", [])):
            notes.append("STATS_V1 的「不要这样用」里已经有「数字无法溯源」;06 §7 建议的是把它写进护栏(guardrail)")
        if "EVIDENCE_NUMERIC_TRACEABILITY_V1" in registry:
            notes.append("「数字要有出处」Butter 另立了一条 `EVIDENCE_NUMERIC_TRACEABILITY_V1`(见提案一)")
    return notes


def amendment_proposal(registry: Registry, *, generated_on: str) -> str:
    resolved = {item.key: {short: resolve_toast_id(registry, short) for short in item.applies_to}
                for item in AMENDMENTS}
    rows = []
    for item in AMENDMENTS:
        ids = resolved[item.key]
        real = "、".join(f"`{i}`" if i else "对不上" for i in ids.values())
        level = "、".join(registry.get(i).evidence_level if i else "—" for i in ids.values())
        rows.append([item.key, real, level, item.action, f"{TODO} {len(item.todo)} 处" if item.todo else "齐"])
    lines = [
        "# 给 Toast 的提案(二):修改现有条目", "",
        f"生成日期:{generated_on}。来源:{DECISION_7};{SOURCE_06}。", "",
        "一句话:决定 7 列了下面几项对 Toast 现有条目的修改建议。「建议」「依据」两栏是 06 §7 的原文;"
        "细节是在 research/02–05 里核到的数字,逐条带出处;核不到的写「待补」,没有编。"
        "Butter 不改 Toast 的文件,改不改、怎么改由人定。", "",
        "## 一页速览", "",
        *_table(["决定 7 里的写法", "Toast 里的真实 ID", "现在的级别", "建议", "内容"], rows), "",
    ]
    for item in AMENDMENTS:
        ids = resolved[item.key]
        found = {short: registry.get(i) for short, i in ids.items() if i}
        lines += [f"## {item.key}:{item.action}", ""]
        for short, real_id in ids.items():
            if real_id:
                lines += [f"- 条目 `{real_id}`(决定 7 写作 {short})现状:", *_current(registry.get(real_id))]
            else:
                family = sorted(i for i in registry.ids if short.split("_")[0] in i
                                and registry.source_of(i).origin != "butter_extension")
                lines.append(f"- 条目 ID 对不上:Toast 的注册表里没有 `EVIDENCE_{short}`(也没有带 _V1 的)。")
                if item.action == "维持不进":
                    lines.append("  - 它是 06 §7 里一条「暂不进」的候选,从来没有进过表;维持不进,Toast 不需要改任何东西。")
                if family:
                    lines.append("  - Toast 现有名字相近的条目:" + "、".join(f"`{i}`" for i in family))
        lines += [f"- 建议(06 §7 原文):{item.suggestion}", f"- 依据(06 §7 原文):{item.basis}", "- 核到的细节:"]
        lines += [f"  - {d.text}(出处:{d.source})" for d in item.details]
        for note in _amendment_notes(item, found, registry):
            lines.append(f"- 注意:{note}")
        lines.append(f"- {TODO}:" + ("无" if not item.todo else ""))
        lines += [f"  - {t}" for t in item.todo]
        lines.append("")
    lines += ["---", "", *_registry_note(registry), ""]
    return "\n".join(lines)


# ── 三、按卡片结果的升级 / 降级 ───────────────────────────────────────

def _count_text(summary: MethodSummary) -> str:
    """两个方向照实并列,不互相抵消。"""
    return f"变多 {len(summary.more.independent)} 次,变少 {len(summary.fewer.independent)} 次"


def _experiment_rows(experiments: list[MethodExperiment]) -> list[str]:
    return _table(
        ["批次", "站点", "行业", "改前测量日", "再测日", "超出波动的腿", "卡", "题从哪来", "备注"],
        [[e.batch_id, e.site_host, e.industry or "没填", e.baseline_day, e.remeasure_day, ";".join(e.legs) or "—",
          "、".join(e.card_ids), "Butter 出的题" if e.butter_panel else "站长给的题", e.note or "—"]
         for e in experiments])


def _level_line(registry: Registry, entry_id: str) -> str:
    if entry_id not in registry:
        return f"- 现在的级别:{TODO}(注册表里查不到 `{entry_id}`)"
    entry = registry.get(entry_id)
    return (f"- 现在的级别:{entry.evidence_level}({LEVEL_TEXT.get(entry.evidence_level, TODO)});"
            f"条目在:{_origin_text(registry, entry_id)};类型:{entry.kind}")


def _proposal_section(registry: Registry, summary: MethodSummary, side: MethodDirection,
                      other: MethodDirection) -> list[str]:
    up = side.direction == "变多"
    kind = "升级" if up else "降级"
    n = len(side.independent)
    lines = [
        f"## {kind}提案:`{summary.entry_id}`", "",
        f"- 结论:{n} 次独立实验都测到明显{side.direction}(方法的状态:{side.state}·{side.direction})。两个方向合起来:"
        f"{_count_text(summary)}。",
        _level_line(registry, summary.entry_id),
        f"- 请 Toast 考虑把这条的证据等级调{'高' if up else '低'}。调几级由人定,这里不替人定。",
        "", f"依据的实验({n} 次,每次是一个只改这一个条目的批次):", "",
        *_experiment_rows(side.independent), "",
    ]
    if side.not_independent:
        lines += ["同站、测量时间重叠,没有另计的:", "", *_experiment_rows(side.not_independent), ""]
    opposite = other.independent + other.not_independent
    if opposite:
        lines += ["", f"反方向({other.direction})的实验,照实并列,不互相抵消:", "", *_experiment_rows(opposite), ""]
    else:
        lines.append(f"- 反方向({other.direction})的实验:没有。")
    same = summary.same_batch_more if up else summary.same_batch_fewer
    same_other = summary.same_batch_fewer if up else summary.same_batch_more
    if same or same_other:
        lines.append(f"- 和别的条目同批一起改、分不开是谁起作用的卡(不计数):{side.direction} {len(same)} 张"
                     + (f"({'、'.join(same)})" if same else "")
                     + f",{other.direction} {len(same_other)} 张" + (f"({'、'.join(same_other)})" if same_other else ""))
    lines.append(f"- 这个条目一共 {summary.cards_total} 张卡:没看出变化或还没试的 {summary.cards_hypothesis} 张,"
                 f"正在试的 {summary.cards_running} 张。")
    if up and side.state == "已复现" and len({e.industry for e in side.independent if e.industry}) < 2:
        lines.append("- 只在一个行业(或没填行业)里复现,还不能说别的行业也这样。")
    lines.append("")
    return lines


def replicated(summaries: list[MethodSummary]) -> tuple[list[MethodSummary], list[MethodSummary]]:
    """(该出升级提案的条目, 该出降级提案的条目)。同一个条目可以两边都在。"""
    ups = [s for s in summaries if len(s.more.independent) >= 2]
    downs = [s for s in summaries if len(s.fewer.independent) >= 2]
    return ups, downs


def replication_proposal(registry: Registry, summaries: list[MethodSummary], *, generated_on: str) -> str:
    ups, downs = replicated(summaries)
    lines = [
        "# 给 Toast 的提案(三):按卡片结果的升级与降级", "",
        f"生成日期:{generated_on}。来源:Butter 卡片库(站长自己部署、自己测的结果)。", "",
        "规则:同一个条目,在两次独立实验里都测到明显变多 → 升级提案;都测到明显变少 → 降级提案。"
        "两个方向都有时照实并列,不互相抵消。「独立」的算法:一个只改这一个条目的批次算一次;"
        "不同站点算独立;同一个站点要测量时间不重叠才算。调几级由人定,提案里不替人定;Butter 不改 Toast 的文件。", "",
        f"一句话:现在有 {len(ups)} 个条目够出升级提案,{len(downs)} 个条目够出降级提案。", "",
        "## 一页速览", "",
    ]
    if summaries:
        rows = []
        for s in summaries:
            verdict = "、".join(v for v in ("升级提案" if s in ups else "", "降级提案" if s in downs else "") if v) or "不出提案"
            rows.append([f"`{s.entry_id}`", _count_text(s),
                         f"{s.more.state or '—'} / {s.fewer.state or '—'}", s.cards_total, verdict])
        lines += _table(["条目 ID", "独立实验", "方法状态(变多 / 变少)", "卡数", "结论"], rows)
    else:
        lines.append("卡片库里还没有卡。")
    lines.append("")
    for s in ups:
        lines += _proposal_section(registry, s, s.more, s.fewer)
    for s in downs:
        lines += _proposal_section(registry, s, s.fewer, s.more)
    lines += ["---", "", "读这份提案时要记住:", *[f"- {c}" for c in CAVEATS], "", *_registry_note(registry), ""]
    return "\n".join(lines)


# ── 合在一起 ─────────────────────────────────────────────────────────

def build_proposals(registry: Registry, summaries: list[MethodSummary], *, generated_on: str) -> dict[str, str]:
    """文件名 → Markdown 文本。落盘由调用方做(store.save_text_files)。"""
    ups, downs = replicated(summaries)
    readme = "\n".join([
        "# 给 Toast 的注册表提案", "",
        f"生成日期:{generated_on}。由 Butter(FatCat GEO Improve)生成;Butter 不改 Toast 的任何文件,这里只是文档,"
        "采不采纳由人看过再定。", "",
        *_table(["文件", "内容", "要点"], [
            [EXTENSION_FILE, "新增条目", f"{sum(1 for e in registry.entries if registry.source_of(e.id).origin == 'butter_extension')} 条"],
            [AMENDMENT_FILE, "修改现有条目", f"{len(AMENDMENTS)} 项(决定 7)"],
            [REPLICATION_FILE, "按卡片结果的升级与降级", f"升级 {len(ups)} 条,降级 {len(downs)} 条"],
        ]), "",
    ])
    return {
        README_FILE: readme,
        EXTENSION_FILE: extension_proposal(registry, generated_on=generated_on),
        AMENDMENT_FILE: amendment_proposal(registry, generated_on=generated_on),
        REPLICATION_FILE: replication_proposal(registry, summaries, generated_on=generated_on),
    }
