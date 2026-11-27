"""兄弟模块文件的只读形状(sources 的产物):Potato 的品牌表与题板、Toast 的官网域名。

Toast 证据注册表条目的形状在 `contracts/registry.py`。
"""

from __future__ import annotations

from butter.contracts.common import Frozen


class PotatoBrand(Frozen):
    name: str
    aliases: list[str] = []
    domains: list[str] = []
    is_focal: bool = False


class PotatoBrands(Frozen):
    path: str
    focal_domain: str | None = None
    brands: list[PotatoBrand] = []


class Question(Frozen):
    id: str
    intent: str = ""
    text: str


class QuestionBoard(Frozen):
    path: str
    prompt_version: str | None = None
    locale: str | None = None
    questions: list[Question] = []


class ToastBrandDomains(Frozen):
    path: str
    brand_name: str | None = None
    brand_aliases: list[str] = []
    primary: str | None = None
    confirmed_domains: list[str] = []
    unconfirmed_domains: list[str] = []
    official_channels: list[str] = []  # 「主机名 + 路径前缀」


class ToastLight(Frozen):
    """Toast 汇总里的一盏灯:某一步的结论。原话照抄,Butter 不重新判。"""

    label: str                       # 如"官网访问(爬虫能不能进你的网站)"
    light: str                       # green / yellow / red / white
    text: str                        # 灯后面的原话,如"🟢 良好"


class ToastStageLine(Frozen):
    section: str                     # 汇总里的小标题(通常是提问类型)
    surface: str                     # 哪家 AI;子行(第三方等)为空
    lights: list[ToastLight] = []


class ToastCitedDomain(Frozen):
    domain: str
    citations: int


class ToastExport(Frozen):
    """Toast 报告导出包里 Butter 用得上的部分(只读)。导出包里没有结构化的检查结论,只有汇总文字,所以是粗粒度的。"""

    path: str
    files_found: list[str] = []
    brand_domains: ToastBrandDomains | None = None
    stage_lines: list[ToastStageLine] = []
    cited_other: list[ToastCitedDomain] = []   # 被引用、但既不是你的也不是竞品的网站;比 Toast 自己那份名单多(近似)
    notes: list[str] = []
