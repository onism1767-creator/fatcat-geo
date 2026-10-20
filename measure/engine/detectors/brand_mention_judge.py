"""品牌提及判定器·裁判版 v1 + 态度判定器·裁判版 v1(S9.3,高档):把 engine/judge.py 的裁判结论翻成判定行。

两个判定器共用同一次裁判调用(裁判对象里缓存),一条回答只问一次;裁判看不到(没人可换 / 3 轮不合格)→ 不产出判定行,
该条不进提及率分母,也不猜。裁判的一句理由与置信度随判定行存档,供单条回答页与校准(S9.4)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.judge import MentionJudge
from engine.models import BrandMentionObservation, BrandSpec, SentimentObservation, SurfaceRun
from engine.validity import is_valid_answer

DETECTOR_NAME = "brand_mention_judge"
DETECTOR_VERSION = 1
SENTIMENT_DETECTOR_NAME = "sentiment_judge"
SENTIMENT_DETECTOR_VERSION = 1


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
        return BrandMentionObservation(
            run_id=run.run_id, detector_name=self.name, detector_version=self.version, computed_at=datetime.now(UTC),
            mentioned=verdict.mentioned, first_position=verdict.first_position,
            matched_text=verdict.spans[0] if verdict.spans else None,
            match_kind="judge" if verdict.mentioned else None,
            confidence=verdict.confidence, reason=verdict.reason or None,
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
