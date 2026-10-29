"""回答缓存(S11.2;架构书附录 v0.4.26 第 9 节 C8):同品牌、同一道题(规范化后同文、同语言)、同仪器 × 型号 × 检索模式,
12 小时内别的检测里已经拿到的有效回答,直接复用、不再向 AI 提问。地区也是键的一部分(用户 2026-10-28 定):同一道题指定不同
国家问,联网搜到的东西不一样;今天调度不指定地区(None 对 None),哪天指定了,不同地区的回答不会互相串用。

复用 = 把那条回答原样复制成本单的一条运行(新 run_id、本单的 prompt_id 与槽位号,花费记 0——这些回答不算本单花费),
原始层只追加照旧;复用关系进只追加表 cache_reuse;采样说明写明「N 条来自 X 小时内的缓存」。
「修好后再测」的单(fresh)不走缓存(S11.3)。键里不含方法版本:回答原文与方法无关,判定随本单方法重算。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from engine.models import Prompt, SearchMode, SurfaceRun, Target
from engine.validity import is_valid_answer

CACHE_HOURS = 12  # 产品文档给 6–24 小时,取中间(架构书默认值)
_SPACES = re.compile(r"\s+")


def normalize_prompt(text: str) -> str:
    """同一道题的判法:全角半角统一、大小写不分、空白压成一个。"""
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


class CacheStore(Protocol):
    def brand_prompts(self, brand_name: str, *, exclude_job_id: str) -> list[tuple[str, str, str]]:
        """同品牌别的任务里的题:(prompt_id, 原文, 语言)。"""
        ...

    def runs_for_prompt(
        self, prompt_id: str, surface: str | None = None, model: str | None = None,
    ) -> list[SurfaceRun]: ...

    def insert_surface_run(self, run: SurfaceRun) -> None: ...

    def insert_cache_reuse(self, *, job_id: str, run_id: str, source_run_id: str, cache_age_s: int) -> None: ...

    def cache_copies(self, run_ids: list[str]) -> set[str]:
        """这些运行里哪些是从缓存复制来的(cache_reuse 里的 run_id)。"""
        ...


def cached_answers(
    store: CacheStore,
    *,
    job_id: str,
    brand_name: str,
    prompt: Prompt,
    target: Target,
    search_mode: SearchMode,
    now: datetime,
    hours: int = CACHE_HOURS,
    region: str | None,  # 必填(第二轮审核 B8):和采样钉的地区一致;原来默认空,以后钉了地区忘传就会串用
) -> list[SurfaceRun]:
    """别的检测里可复用的有效回答,新的在前;不含本单自己的题。只拿原件:别的单从缓存复制来的那条
    (时间戳、原文都和原件一样)不再当成第二条回答,否则同一条回答会填进同一道题的两个槽位(审核 2026-10-27 黄 6)。"""
    wanted = normalize_prompt(prompt.text)
    since = now - timedelta(hours=hours)
    hits: list[SurfaceRun] = []
    for prompt_id, text, locale in store.brand_prompts(brand_name, exclude_job_id=job_id):
        if locale != prompt.locale or normalize_prompt(text) != wanted:
            continue
        for run in store.runs_for_prompt(prompt_id, surface=target.surface, model=target.model):
            if (run.search_mode is search_mode and run.region == region and run.timestamp >= since
                    and is_valid_answer(run)):
                hits.append(run)
    copies = store.cache_copies([r.run_id for r in hits]) if hits else set()
    hits = [r for r in hits if r.run_id not in copies]
    hits.sort(key=lambda r: r.timestamp, reverse=True)
    return hits


def fill_from_cache(
    store: CacheStore,
    *,
    job_id: str,
    brand_name: str,
    prompt: Prompt,
    target: Target,
    search_mode: SearchMode,
    missing_indices: list[int],
    now: datetime,
    hours: int = CACHE_HOURS,
    region: str | None,  # 必填(第二轮审核 B8):和采样钉的地区一致;原来默认空,以后钉了地区忘传就会串用
) -> list[SurfaceRun]:
    """把缓存里的回答填进本单空着的槽位(按槽位号顺序,一条只用一次),落库并记复用关系;返回填进去的运行。"""
    if not missing_indices:
        return []
    copies: list[SurfaceRun] = []
    for index, source in zip(sorted(missing_indices), cached_answers(
        store, job_id=job_id, brand_name=brand_name, prompt=prompt, target=target, search_mode=search_mode,
        now=now, hours=hours, region=region,
    ), strict=False):
        copy = source.model_copy(update={
            "run_id": str(uuid4()), "prompt_id": prompt.prompt_id, "run_index": index,
            "cost_usd": 0.0,  # 不算本单花费:钱是别的单花的
        })
        store.insert_surface_run(copy)
        store.insert_cache_reuse(job_id=job_id, run_id=copy.run_id, source_run_id=source.run_id,
                                 cache_age_s=int((now - source.timestamp).total_seconds()))
        copies.append(copy)
    return copies
