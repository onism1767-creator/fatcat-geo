"""站长改事实(纯层)。设计 §5.2 的 FactEdit:站长在 Butter 里改的值优先级最高,报告里写明"站长在 Butter 里改的"。

Butter 读网站可能读错(价格读成划线价、名称带了后缀)。站长改掉之后,改动包按改过的事实重出。
原来读到的值不丢:挪到"其他说法"里,报告照样看得到。
同一个字段改过几次,以最后一次为准;改成空 = "这个值不要用",字段就空着。
"""

from __future__ import annotations

import re

from butter.contracts import FactBase, FactEdit, FactField, FactValue

OWNER_SOURCE = "站长在 Butter 里改的"
ENTITY_FIELDS = {"name": "名称", "category": "品类", "description": "简介", "official_domain": "官网域名"}
PRODUCT_FIELDS = {"name": "名称", "price": "价格", "currency": "币种", "sku": "货号", "category": "品类",
                  "availability": "库存状态", "brand": "品牌", "description": "描述"}
_PRODUCT = re.compile(r"^products\[(?P<id>[^\]]+)\]\.(?P<prop>[a-z_]+)$")
MAX_VALUE = 2000


def _value(field: FactField) -> str:
    return field.values[0].value if field.values else ""


def editable_fields(fb: FactBase) -> list[dict]:
    """能改的字段,带现在的值。返回 [{field, group, label, value, edited}]。"""
    rows = [{"field": f"entity.{prop}", "group": "这个网站", "label": label,
             "value": _value(getattr(fb.entity, prop)), "edited": _edited(getattr(fb.entity, prop))}
            for prop, label in ENTITY_FIELDS.items()]
    for product in fb.products:
        group = _value(product.name) or product.product_id
        rows += [{"field": f"products[{product.product_id}].{prop}", "group": group, "label": label,
                  "value": _value(getattr(product, prop)), "edited": _edited(getattr(product, prop))}
                 for prop, label in PRODUCT_FIELDS.items()]
    return rows


def _edited(field: FactField) -> bool:
    return bool(field.values) and field.values[0].source_kind == "owner_edit"


def _override(field: FactField, edit: FactEdit) -> FactField:
    kept = [v for v in [*field.values, *field.alternatives] if v.source_kind != "owner_edit"]
    values = [FactValue(value=edit.value, source_url=OWNER_SOURCE, source_kind="owner_edit",
                        source_path=edit.note[:120])] if edit.value else []
    return field.model_copy(update={"values": values, "alternatives": kept, "conflict": False, "conflict_note": None})


def apply_edits(fb: FactBase, edits: list[FactEdit]) -> tuple[FactBase, list[str]]:
    """把站长的改动盖到事实库上。只用本站的;认不出的字段跳过并说明。返回(新的事实库, 没用上的原因)。"""
    latest: dict[str, FactEdit] = {}
    for edit in edits:
        if edit.site_host == fb.host:
            latest[edit.field] = edit
    skipped: list[str] = []
    entity = fb.entity
    products = {p.product_id: p for p in fb.products}
    for path, edit in latest.items():
        match = _PRODUCT.match(path)
        if path.startswith("entity.") and path[7:] in ENTITY_FIELDS:
            prop = path[7:]
            entity = entity.model_copy(update={prop: _override(getattr(entity, prop), edit)})
        elif match and match["prop"] in PRODUCT_FIELDS and match["id"] in products:
            product = products[match["id"]]
            products[match["id"]] = product.model_copy(
                update={match["prop"]: _override(getattr(product, match["prop"]), edit)})
        else:
            skipped.append(f"{path}:这次读到的事实里没有这个字段,这条改动没用上")
    new = fb.model_copy(update={"entity": entity, "products": [products[p.product_id] for p in fb.products]})
    return new, skipped


def changed_edits(fb: FactBase, submitted: dict[str, str], *, note: str, at: str) -> list[FactEdit]:
    """表单交上来的值里,和现在不一样的才记成改动。只认能改的字段。"""
    current = {row["field"]: row["value"] for row in editable_fields(fb)}
    out = []
    for field, raw in submitted.items():
        if field not in current:
            continue
        value = " ".join(str(raw).split())[:MAX_VALUE]
        if value != " ".join(current[field].split()):
            out.append(FactEdit(site_host=fb.host, field=field, value=value, note=" ".join(note.split())[:200], at=at))
    return out
