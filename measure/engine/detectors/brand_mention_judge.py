"""品牌提及判定器·裁判版 v2 + 态度判定器·裁判版 v2(S9.3,高档;中档只判说不准的,BR4;v2 = BR5,只认登记过的写法):
把 engine/judge.py 的裁判结论翻成判定行。

两个判定器共用同一次裁判调用(裁判对象里缓存),一条回答只问一次;裁判看不到(3 轮不合格)→ 不产出判定行,
该条不进提及率分母,也不猜。裁判说信息不够(no_info)→ 判定行记说不准,同样不进分母。
裁判的理由码、一句理由、置信度和它看到的新写法随判定行存档,供单条回答页与校准(S9.4);
哪个裁判判的、是不是自家判自家也记在判定行上(BR6)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.detectors.brand_mention_fuzzy import find_mention
from engine.judge import NO_INFO, MentionJudge
from engine.models import BrandMentionObservation, BrandSpec, SentimentObservation, SurfaceRun
from engine.validity import is_valid_answer

DETECTOR_NAME = "brand_mention_judge"
DETECTOR_VERSION = 2
SENTIMENT_DETECTOR_NAME = "sentiment_judge"
SENTIMENT_DETECTOR_VERSION = 2


class JudgeBrandMentionDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def __init__(self, judge: MentionJudge) -> None:
        self._judge = judge

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> BrandMentionObservation | None:
        if not is_valid_answer(run):
            return None
        verdict = self._judge.judge(run, brand)
        if verdict is None:
            return None  # 裁判看不到:缺席就是缺席
        common = dict(run_id=run.run_id, detector_name=self.name, detector_version=self.version,
                      computed_at=datetime.now(UTC), confidence=verdict.confidence, reason=verdict.reason or None,
                      reason_code=verdict.reason_code, new_spellings=verdict.new_spellings,
                      judged_by=verdict.judged_by, own_vendor=verdict.own_vendor)
        if verdict.reason_code == NO_INFO:  # 说不准:记下字面上看到的那一处
            assert run.answer is not None
            hit = find_mention(run.answer.answer_text, brand, run.locale)
            return BrandMentionObservation(mentioned=None, matched_text=hit.text if hit else None,
                                           first_position=hit.position if hit else None, **common)
        return BrandMentionObservation(
            mentioned=verdict.mentioned, first_position=verdict.first_position,
            matched_text=verdict.spans[0] if verdict.spans else None,
            match_kind="judge" if verdict.mentioned else None, **common,
        )


class JudgeSentimentDetector:
    name = SENTIMENT_DETECTOR_NAME
    version = SENTIMENT_DETECTOR_VERSION

    def __init__(self, judge: MentionJudge) -> None:
        self._judge = judge

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> SentimentObservation | None:
        if not is_valid_answer(run):
            return None
        verdict = self._judge.judge(run, brand)
        if verdict is None or not verdict.mentioned or verdict.sentiment is None:
            return None  # 没提到 / 裁判没给态度 → 不可判定
        return SentimentObservation(
            run_id=run.run_id, detector_name=self.name, detector_version=self.version, computed_at=datetime.now(UTC),
            sentiment=verdict.sentiment,
        )
