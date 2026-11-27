"""展示页的「隐去名字」(纯层)。设计 §12。

示例用的是真实网站的一次运行。导出之前,把真名换成代号;换完再全文查一遍,还搜得到真名就不导出。

  auto_names(fb)          从事实库列出要换的词:官网域名、社交账号、logo、商品页地址、品牌名和别名、商品名和货号
  site_urls(texts, hosts) 文字里出现过的本站地址(整条换掉,地址里的型号、系列名就不会漏出去)
  url_names(urls, known)  给这些地址编号:page-1、page-2 …
  recode(text, names)     按对照表替换:长的先换,不分大小写,换出来的字不会再被换一次
  recode_tree(obj, names) 对一整个视图对象(字典、列表、字符串)做 recode
  leaks(text, names)      还剩哪些真名(不分大小写)

对照表 = [(真名, 代号), ...]。价格、参数、检查结果这些数不动。对照表本身不进仓库,也不随展示页导出。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from butter.contracts import FactBase
from butter.util import slugify

Names = list[tuple[str, str]]
MIN_AUTO_LEN = 3            # 自动列出的词至少这么长才换:太短的(比如两个字母的型号)会把无关的字也换掉,留给人工决定
BRAND = "Brand A"
BRAND_HOST = "brand-a.example"   # .example 结尾的网址点不开(RFC 2606)
_WORD = "A-Za-z0-9_"            # 型号这类短词要「整词」才换:前后不能紧挨着这些字符
_URL_END = r"""[^\s"'<>()\[\]{}|\\^`，。；、）】」』]"""
_JOINERS = "/-_."


def slug(code: str) -> str:
    """代号的网址写法:Brand A → brand-a。"""
    return re.sub(r"[^a-z0-9.]+", "-", code.lower()).strip("-")


def _bare_host(value: str) -> str:
    host = re.sub(r"^[a-z]+://", "", value.strip().lower()).split("/")[0].split(":")[0]
    return host.removeprefix("www.")


def whole_word_only(real: str) -> bool:
    """带数字的短词(S2、E35、C30 这类型号):只在它自己是一个完整的词时才算。

    不这样的话,S2 会把 EVIDENCE_STATS2、颜色代码、运行号里碰巧出现的同样几个字符也换掉,查的时候也会误报。
    品牌名这类长词不受这个限制:acmeofficial、acmelife 里的品牌名也要换。
    """
    return len(real) <= 4 and real.isascii() and real.isalnum() and any(ch.isdigit() for ch in real)


def _piece(real: str) -> str:
    escaped = re.escape(real)
    return f"(?<![{_WORD}]){escaped}(?![{_WORD}])" if whole_word_only(real) else escaped


def auto_names(fb: FactBase) -> tuple[Names, list[str]]:
    """从事实库列出对照表。返回(对照表, 太短没有自动换的词)。系列名这类事实库里没有的词,要人工补。"""
    names: Names = []
    skipped: list[str] = []
    seen: set[str] = set()

    def add(real: str | None, code: str) -> None:
        real = (real or "").strip()
        if not real or real.casefold() in seen:
            return
        if len(real) < MIN_AUTO_LEN and not whole_word_only(real):
            skipped.append(real)
            return
        seen.add(real.casefold())
        names.append((real, code))

    hosts = [h for h in dict.fromkeys(_bare_host(v) for v in [*(x.value for x in fb.entity.official_domain.values),
                                                              fb.host]) if h]
    for n, host in enumerate(hosts, start=1):
        add(host, BRAND_HOST if n == 1 else f"brand-a-{n}.example")
    for n, product in enumerate(fb.products, start=1):
        add(product.page_url, f"https://{BRAND_HOST}/products/model-{n}")
    for n, value in enumerate(fb.entity.same_as.values, start=1):
        add(value.value, f"https://social.example/brand-a-{n}")
    for value in fb.entity.logo.values:
        add(value.value, f"https://{BRAND_HOST}/logo.png")
    brand_names = [v.value for v in [*fb.entity.name.values, *fb.entity.name.alternatives,
                                     *fb.entity.aliases.values, *fb.entity.aliases.alternatives]]
    for n, name in enumerate(dict.fromkeys(x.strip() for x in brand_names if x and x.strip()), start=1):
        add(name, BRAND if n == 1 else f"{BRAND}-{n}")
    brands = [real for real, code in names if code == BRAND]
    for n, product in enumerate(fb.products, start=1):
        for value in [*product.name.values, *product.name.alternatives]:
            add(value.value, f"Model {n}")
            add(slugify(value.value), f"model-{n}")   # 改动包的文件名是商品名的网址写法(robot-vacuum-x1),也要换
            for brand in brands:   # 商品名常以品牌名开头;别处可能只写后半截,也要认得
                if value.value.casefold().startswith(brand.casefold() + " "):
                    rest = value.value[len(brand):].strip()
                    add(rest, f"Model {n}")
                    add(slugify(rest), f"model-{n}")
        for value in [*product.sku.values, *product.sku.alternatives]:
            add(value.value, f"SKU-{n}")
        for value in product.brand.values:
            add(value.value, BRAND)
    # 商品名里带数字的短词多半是型号(S2、E35):单独出现在标题、描述里时也要换。按整词换,不会碰到别的字
    codes: dict[str, None] = {}
    for product in fb.products:
        for value in [*product.name.values, *product.name.alternatives]:
            for word in re.findall(rf"(?<![{_WORD}])[A-Za-z0-9]+(?![{_WORD}])", value.value):
                if whole_word_only(word) and not word.isdigit():
                    codes.setdefault(word.casefold(), None)
    for n, word in enumerate(codes, start=1):
        add(word, f"X{n}")
    return names, skipped


def hosts_of(names: Names) -> list[str]:
    """对照表里的域名(代号以 .example 结尾、真名里没有斜杠的那些)。"""
    return [real for real, code in names if code.endswith(".example") and "/" not in real]


def site_urls(texts: Iterable[str], hosts: list[str]) -> list[str]:
    """文字里出现过的本站地址(含子域),按第一次出现的先后。结尾的标点不算在地址里。"""
    if not hosts:
        return []
    alt = "|".join(re.escape(h) for h in sorted(hosts, key=len, reverse=True))
    pattern = re.compile(rf"https?://(?:[a-z0-9-]+\.)*(?:{alt})(?::\d+)?(?:/{_URL_END}*)?", re.IGNORECASE)
    found: dict[str, None] = {}
    for text in texts:
        for match in pattern.finditer(text):
            found.setdefault(match.group(0).rstrip(".,;:!?"), None)
    return list(found)


def url_names(urls: list[str], known: Names) -> Names:
    """给本站地址编号。已经在对照表里的(商品页)不重复编;同一页带不带末尾斜杠算一个号。"""
    taken = {real.casefold().rstrip("/") for real, _ in known}
    numbers: dict[str, int] = {}
    out: Names = []
    for url in urls:
        key = url.casefold().rstrip("/")
        if key in taken:
            continue
        number = numbers.setdefault(key, len(numbers) + 1)
        is_home = re.fullmatch(r"https?://[^/]+", key) is not None
        out.append((url, f"https://{BRAND_HOST}/" if is_home else f"https://{BRAND_HOST}/page-{number}"))
    return out


def _pattern(names: Names) -> re.Pattern[str] | None:
    reals = sorted({real for real, _ in names if real}, key=len, reverse=True)   # 长的先换:acme.com 先于 acme
    return re.compile("|".join(_piece(r) for r in reals), re.IGNORECASE) if reals else None


def recode(text: str, names: Names) -> str:
    """替换一遍。夹在网址、文件名里的(前后是 / - _ . 或字母数字)用代号的网址写法,其余用原样的代号。"""
    pattern = _pattern(names)
    if pattern is None or not text:
        return text
    codes = {real.casefold(): code for real, code in reversed(names)}   # 同一个真名出现两次,以前面那条为准

    def swap(match: re.Match[str]) -> str:
        code = codes[match.group(0).casefold()]
        if "://" in code or code.endswith(".example"):
            return code
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end()] if match.end() < len(text) else ""
        beyond = text[match.end() + 1] if match.end() + 1 < len(text) else ""
        if after == "." and not (beyond.isascii() and beyond.isalnum()):
            after = ""   # 句子结尾的句号不算:只有 x.html、x.com 这种才是夹在文件名、网址里
        glued = any(ch and (ch in _JOINERS or (ch.isascii() and ch.isalnum())) for ch in (before, after))
        return slug(code) if glued else code

    return pattern.sub(swap, text)


def recode_tree(obj: Any, names: Names) -> Any:
    """对视图对象里的每一段文字做 recode;字典的键也换(改动包的文件名可能是键)。数字、真假值原样。"""
    if isinstance(obj, str):
        return recode(obj, names)
    if isinstance(obj, dict):
        return {recode(k, names) if isinstance(k, str) else k: recode_tree(v, names) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [recode_tree(v, names) for v in obj]
    return obj


def leaks(text: str, names: Names) -> list[str]:
    """文字里还搜得到的真名(不分大小写),按对照表的先后,不重复。"""
    low = text.casefold()
    found = []
    for real, _ in names:
        if not real or real.casefold() not in low:
            continue
        if whole_word_only(real) and not re.search(_piece(real), text, re.IGNORECASE):
            continue
        found.append(real)
    return list(dict.fromkeys(found))


def leftover_hints(texts: Iterable[str], fb: FactBase, names: Names, limit: int = 20) -> list[tuple[str, int]]:
    """给做展示页的人提个醒:商品名里的词(多半是系列名、型号),导出里还出现了几次。

    这些词不在对照表里,所以没换、也查不出来。Butter 分不清它是普通词(比如 Vacuum)还是系列名,由人来看要不要补进对照表。
    返回 [(词, 出现次数)],次数多的在前。
    """
    known = {real.casefold() for real, _ in names}
    words: dict[str, None] = {}
    sources = [v.value for p in fb.products for v in [*p.name.values, *p.name.alternatives]]
    sources += [v.value for v in [*fb.entity.name.values, *fb.entity.aliases.values]]   # 品牌名里的单个词也算
    for value in sources:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}|[A-Za-z]\d+", value):
            if word.casefold() not in known:
                words.setdefault(word, None)
    whole = "\n".join(texts)
    counted = [(word, len(re.findall(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", whole, re.IGNORECASE)))
               for word in words]
    return sorted((item for item in counted if item[1]), key=lambda item: (-item[1], item[0]))[:limit]


def parse_names(doc: Any) -> Names:
    """人工补的对照表(YAML 读出来的样子):{names: [{real: 真名, code: 代号}, ...]}。写得不对就抛 ValueError。"""
    if doc is None:
        return []
    rows = doc.get("names") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise ValueError("对照表要写成 names: 下面一行一个 {real: 真名, code: 代号}")
    out: Names = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not str(row.get("real") or "").strip() or not str(row.get("code") or "").strip():
            raise ValueError(f"对照表第 {i} 行要有 real(真名)和 code(代号)两项")
        out.append((str(row["real"]).strip(), str(row["code"]).strip()))
    return out
