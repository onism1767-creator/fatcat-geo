"""品牌提及判定器 v1:别名表 + 大小写不敏感子串匹配。

规则打底(风险表 #3),LLM 复核作为后续版本升级;升级后对全部历史
SurfaceRun 重算并落 RecomputeEvent(13.5)。

v1 不判定 brand_rank(回答列举中的名次):规则法对"列举结构"的解析
不可靠,宁缺毋滥,留 None(不是 0、不是猜测)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.models import BrandMentionObservation, BrandSpec, Outcome, SurfaceRun

DETECTOR_NAME = "brand_mention_rules"
DETECTOR_VERSION = 1


class BrandMentionDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> BrandMentionObservation | None:
        """回答文本中是否提及品牌。不可判定(无成功回答)时返回 None,
        不产出观测——缺席的观测就是缺席,禁止填充默认值。"""
        if run.outcome is not Outcome.SUCCESS or run.answer is None:
            return None
        # 用 lower() 而非 casefold():保证位置偏移与原文一一对应
        text = run.answer.answer_text.lower()
        best: tuple[int, str] | None = None
        for alias in dict.fromkeys([brand.name, *brand.aliases]):
            needle = alias.strip().lower()
            if not needle:
                continue
            pos = text.find(needle)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, alias)
        common = dict(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
        )
        if best is None:
            return BrandMentionObservation(mentioned=False, **common)
        return BrandMentionObservation(
            mentioned=True, matched_alias=best[1], first_position=best[0], **common
        )
