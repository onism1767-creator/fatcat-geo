"""情感判定器 v2:品牌提及句的词表极性统计。

明确的 v1 局限(规则打底,LLM 复核升级为 v2,升级后全量重算):
- 只看首次提及品牌所在的单句,不做跨句归因;
- 小词表、子串计数,不理解否定转折以外的语义;
- 为避免"不推荐"同时命中正负词,先匹配并遮蔽负面词,再数正面词。

判定不了的一律 None:品牌未被提及时,谈不上"AI 对它的情感"。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from engine.detectors.matching import find_first_alias
from engine.models import BrandSpec, Sentiment, SentimentObservation, SurfaceRun
from engine.validity import is_valid_answer

DETECTOR_NAME = "sentiment_lexicon"
DETECTOR_VERSION = 2

_SENTENCE_SPLIT = re.compile(r"[。!?;\n]|[.!?;](?=\s|$)")

_NEGATIVE_TERMS = (
    "不推荐", "不建议", "避免", "缺点", "劣势", "昂贵", "价格高", "难用", "复杂",
    "落后", "较弱", "不足", "过时", "谨慎",
    "avoid", "expensive", "poor", "lacking", "outdated", "weak", "downside",
)
_POSITIVE_TERMS = (
    "推荐", "建议", "首选", "最佳", "优秀", "领先", "强大", "好用", "值得",
    "性价比", "成熟", "完善", "受欢迎",
    "recommended", "best", "excellent", "leading", "popular", "mature", "top pick",
)


def _mention_sentence(text: str, position: int) -> str:
    """含指定字符偏移的那一句。"""
    start = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        if match.start() >= position:
            return text[start:match.start()]
        start = match.end()
    return text[start:]


def _classify(sentence: str) -> Sentiment:
    lowered = sentence.lower()
    negatives = 0
    for term in _NEGATIVE_TERMS:
        hits = lowered.count(term)
        if hits:
            negatives += hits
            lowered = lowered.replace(term, "□")  # 遮蔽,防"不推荐"再命中"推荐"
    positives = sum(lowered.count(term) for term in _POSITIVE_TERMS)
    if positives and negatives:
        return Sentiment.MIXED
    if positives:
        return Sentiment.POSITIVE
    if negatives:
        return Sentiment.NEGATIVE
    return Sentiment.NEUTRAL


class SentimentDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> SentimentObservation | None:
        if not is_valid_answer(run):
            return None
        assert run.answer is not None
        found = find_first_alias(run.answer.answer_text, brand)
        if found is None:
            return None  # 未提及 → 情感不可判定,不产出观测
        sentence = _mention_sentence(run.answer.answer_text, found[0])
        return SentimentObservation(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
            sentiment=_classify(sentence),
        )
