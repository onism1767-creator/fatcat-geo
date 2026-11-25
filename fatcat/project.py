"""一个项目 = 一个品牌从头到尾走一遍的记录:填的信息、每一步做到哪、各步的结果放在哪。

存成 `runs/<编号>/project.json`。品牌信息只在这里填一次,后面每一步都从这里取。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fatcat.modules import ModuleError

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
LOCALES = ("en-US", "zh-CN")
# 改项目文件一律在这把锁里「重读、改、写」。网页的几个操作和后台的读网站会同时改同一个文件,
# 各自拿着先前读到的整份内容写回去,后写的就把先写的盖掉(2026-11-24 真跑时丢过一次证据包,见错误日志)。
LOCK = threading.RLock()
_FIXED = ("id", "created_at")


@dataclass
class Project:
    id: str
    brand: str
    aliases: str
    website: str
    category: str
    locale: str  # 出题和提问用的语言
    created_at: str
    quick_check: str | None = None  # 证据包的文件名(在项目文件夹里)
    toast_job: str | None = None  # 测量与诊断那一单的单号
    toast_pending: str | None = None  # 从这里交过去、还在测量工具里没做完的那一单(给用户一条回去的路)
    improve_state: str = "none"  # none / running / done / failed
    improve_run: str | None = None  # 读网站那次运行的文件夹
    improve_note: str = ""  # 失败时的原话
    deploy_day: str | None = None  # 改动放上网站的日子(用户记的,ISO 日期)
    baseline_job: str | None = None  # 改之前测的那一单(点「再测」那一刻用着的那一单,之后不跟着变)
    retest_pending: str | None = None  # 已经开始、还没挂上结果的那一单再测
    retest_job: str | None = None  # 挂上了结果的那一单再测
    compare_file: str | None = None  # 改前改后对比结果的文件名(在项目文件夹里)
    compare_note: str = ""  # 对比没做成时的原话

    @property
    def host(self) -> str:
        return (urlsplit(self.website).hostname or "").lower()


def normalize_website(text: str) -> str:
    """用户只写域名时补上 https://;不像网址就直说。"""
    text = text.strip()
    if not text:
        raise ModuleError("官网地址没有填")
    url = text if "://" in text else "https://" + text
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise ModuleError(f"这不像一个网址:{text}")
    return url


def folder(root: Path, project_id: str) -> Path:
    if not _ID.match(project_id):  # 编号会拼进路径,只认自己生成的那种写法
        raise ModuleError(f"没有这个项目:{project_id}")
    return Path(root) / project_id


def create(root: Path, *, brand: str, aliases: str, website: str, category: str, locale: str,
           now: datetime | None = None) -> Project:
    brand, category = brand.strip(), category.strip()
    if not brand or not category:
        raise ModuleError("品牌名和品类都要填")
    if locale not in LOCALES:
        raise ModuleError(f"语言只有 {' / '.join(LOCALES)}")
    website = normalize_website(website)
    now = now or datetime.now().astimezone()
    host = re.sub(r"[^a-z0-9]+", "-", (urlsplit(website).hostname or "site").lower()).strip("-")
    project = Project(id=f"{host}-{now.strftime('%Y%m%d-%H%M%S')}"[:80], brand=brand, aliases=aliases.strip(),
                      website=website, category=category, locale=locale, created_at=now.isoformat(timespec="seconds"))
    target = folder(root, project.id)
    if target.exists():
        raise ModuleError("同一秒里已经建过一个同名项目,再点一次")
    target.mkdir(parents=True)
    save(root, project)
    return project


def save(root: Path, project: Project) -> None:
    path = folder(root, project.id) / "project.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(asdict(project), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)  # 先写临时文件再换名:写到一半断电也不会留下半个文件


def update(root: Path, project_id: str, **changes: object) -> Project:
    """只改点名的那几项:锁住,重读,改,写。别的操作刚写进去的其他项不会被盖掉。"""
    known = {f.name for f in fields(Project)} - set(_FIXED)
    unknown = set(changes) - known
    if unknown:
        raise ValueError(f"项目里没有这几项,或者不许改:{sorted(unknown)}")
    with LOCK:
        project = load(root, project_id)
        for key, value in changes.items():
            setattr(project, key, value)
        save(root, project)
        return project


def load(root: Path, project_id: str) -> Project:
    path = folder(root, project_id) / "project.json"
    if not path.is_file():
        raise ModuleError(f"没有这个项目:{project_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Project)}
    return Project(**{key: value for key, value in data.items() if key in known})


def list_all(root: Path) -> list[Project]:
    projects = []
    for path in Path(root).glob("*/project.json"):
        try:
            projects.append(load(root, path.parent.name))
        except (ModuleError, json.JSONDecodeError, TypeError):
            continue
    return sorted(projects, key=lambda p: p.created_at, reverse=True)
