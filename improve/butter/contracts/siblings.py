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
