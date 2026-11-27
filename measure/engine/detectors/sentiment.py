"""情感判定器 v3:品牌提及句的词表极性统计;哪一句是「提及句」跟档位的主提及判定器走(S9.2)。

明确的 v1 局限(规则打底,LLM 复核升级为 v2,升级后全量重算):
- 只看首次提及品牌所在的单句,不做跨句归因;
- 小词表、子串计数,不理解否定转折以外的语义;
- 为避免"不推荐"同时命中正负词,先匹配并遮蔽负面词,再数正面词。

判定不了的一律 None:品牌未被提及时,谈不上"AI 对它的情感"。
v3(S9.2):提及句的定位不再自己匹配,改用传进来的主提及判定器(规则 / 模糊随档位),
两者才会对「提到了几次」「提到你的 N 句话」口径一致;配规则判定器时行为与 v2 相同。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.detectors.matching import mention_sentence
from engine.detectors.runner import Detector
from engine.models import BrandMentionObservation, BrandSpec, Sentiment, SentimentObservation, SurfaceRun
from engine.validity import is_valid_answer

DETECTOR_NAME = "sentiment_lexicon"
DETECTOR_VERSION = 3

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

    def __init__(self, mention: Detector) -> None:
        self._mention = mention  # 档位的主提及判定器:提及句按它定位(S9.2)

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> SentimentObservation | None:
        if not is_valid_answer(run):
            return None
        assert run.answer is not None
        found = self._mention.detect(run, brand)
        if not isinstance(found, BrandMentionObservation) or not found.mentioned or found.first_position is None:
            return None  # 未提及 → 情感不可判定,不产出观测
        sentence = mention_sentence(run.answer.answer_text, found.first_position)  # 取句原语在 matching.py 共用(S7)
        return SentimentObservation(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
            sentiment=_classify(sentence),
        )
