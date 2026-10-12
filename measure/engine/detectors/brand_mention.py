"""品牌提及判定器 v2:别名表 + 大小写不敏感匹配,拉丁字母按词边界、中文按子串(D18)。

规则打底(风险表 #3),LLM 复核作为后续版本升级;升级后对全部历史
SurfaceRun 重算并落 RecomputeEvent(13.5)。
v2(v0.4.26 S3):匹配规则改词边界;空回答(SUCCESS 但正文为空)不可判定。

v1 不判定 brand_rank(回答列举中的名次):规则法对"列举结构"的解析
不可靠,宁缺毋滥,留 None(不是 0、不是猜测)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.detectors.matching import find_first_alias
from engine.models import BrandMentionObservation, BrandSpec, SurfaceRun
from engine.validity import is_valid_answer

DETECTOR_NAME = "brand_mention_rules"
DETECTOR_VERSION = 2


class BrandMentionDetector:
    name = DETECTOR_NAME
    version = DETECTOR_VERSION

    def detect(self, run: SurfaceRun, brand: BrandSpec) -> BrandMentionObservation | None:
        """回答文本中是否提及品牌。不可判定(没有有效回答:拒答 / 失败 / 空回答)时返回 None,
        不产出观测——缺席的观测就是缺席,禁止填充默认值。"""
        if not is_valid_answer(run):
            return None
        assert run.answer is not None
        found = find_first_alias(run.answer.answer_text, brand)
        common = dict(
            run_id=run.run_id,
            detector_name=self.name,
            detector_version=self.version,
            computed_at=datetime.now(UTC),
        )
        if found is None:
            return BrandMentionObservation(mentioned=False, **common)
        position, alias = found
        return BrandMentionObservation(
            mentioned=True, matched_alias=alias, first_position=position, **common
        )
