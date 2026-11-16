"""档位配置(S9.1,架构书附录 v0.4.27 + 2026-10-14 拍板):三档参数从 config/tiers.yaml 读;
型号按各适配器 meta 的标记选(消费端默认 / 主力 = 最新一代同款,用户 2026-11-15 定);
新档位 = 加一段配置,代码零改动(硬边界 10 同款验收)。零网络零数据库。"""

from pathlib import Path

import pytest

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from engine.models import Target
from engine.tiers import MentionMethod, ModelRule, ModelSwap, TierChoice, load_tiers, refresh_targets, tier_targets


class FakeAdapter(SurfaceAdapter):
    def __init__(self, surface: str, models: list[str], consumer: str, main: str) -> None:
        self._meta = SurfaceMeta(
            provider=f"{surface}_vendor", surface=surface, display_name=surface.upper(), api_name=f"{surface} API",
            models=models, default_model=models[0], consumer_default_model=consumer, main_model=main,
            models_checked="test", capabilities=CapabilityRegistry(supports_citations=True),
            est_cost_per_call_usd={m: 0.01 for m in models},
        )

    def meta(self) -> SurfaceMeta:
        return self._meta

    def ask(self, request):
        raise AssertionError("测试不应触发真实采样")

    def healthcheck(self) -> bool:
        return True


ADAPTERS = {
    "a": FakeAdapter("a", ["a1", "a2", "a3"], consumer="a1", main="a3"),  # 主力 ≠ 消费端默认
    "b": FakeAdapter("b", ["b1"], consumer="b1", main="b1"),  # 只有一个型号
    "c": FakeAdapter("c", ["c1", "c2"], consumer="c2", main="c2"),  # 同一个型号,且是清单最后一个
}


def test_config_has_three_tiers_with_the_decided_numbers():
    config = load_tiers()
    assert [t.name for t in config.tiers] == ["low", "medium", "high"] and config.default == "medium"
    assert [t.runs_per_prompt for t in config.tiers] == [3, 5, 7]  # 用户拍板 T5
    assert [t.models for t in config.tiers] == [ModelRule.CONSUMER_DEFAULT, ModelRule.MAIN_AND_CONSUMER_DEFAULT,
                                                ModelRule.ALL]
    # 低档规则;中档规则 + 模糊(S9.2);高档再加 AI 裁判(S9.3)
    assert [t.mention_method for t in config.tiers] == [MentionMethod.RULES, MentionMethod.RULES_FUZZY,
                                                        MentionMethod.RULES_FUZZY_JUDGE]
    assert (config.custom_runs.min, config.custom_runs.max) == (3, 10) and config.allows_runs(10)
    assert not config.allows_runs(2) and not config.allows_runs(11)
    assert config.default_tier.name == "medium" and config.get("nope") is None


def test_tier_targets_follow_the_model_marks():
    config = load_tiers()
    low, medium, high = (config.get(n) for n in ("low", "medium", "high"))
    assert tier_targets(low, ADAPTERS) == [Target(surface="a", model="a1"), Target(surface="b", model="b1"),
                                           Target(surface="c", model="c2")]
    # 中档 = 主力 + 消费端默认;b 只有一个就一个;c 两者同一个 → 补清单里紧挨着的(c2 是最后一个 → 补前面的 c1)
    assert tier_targets(medium, ADAPTERS) == [
        Target(surface="a", model="a3"), Target(surface="a", model="a1"),
        Target(surface="b", model="b1"),
        Target(surface="c", model="c2"), Target(surface="c", model="c1"),
    ]
    assert tier_targets(high, ADAPTERS) == [Target(surface=s, model=m) for s, ms in
                                            (("a", ["a1", "a2", "a3"]), ("b", ["b1"]), ("c", ["c1", "c2"]))
                                            for m in ms]


def test_a_new_tier_is_one_config_block_and_zero_code(tmp_path: Path):
    text = Path("config/tiers.yaml").read_text(encoding="utf-8") + (
        "  - name: ultra\n    runs_per_prompt: 9\n    models: all\n    mention_method: rules_fuzzy_judge\n"
    )
    path = tmp_path / "tiers.yaml"
    path.write_text(text, encoding="utf-8")
    config = load_tiers(path)
    ultra = config.get("ultra")
    assert ultra is not None and ultra.runs_per_prompt == 9 and ultra.mention_method is MentionMethod.RULES_FUZZY_JUDGE
    assert len(tier_targets(ultra, ADAPTERS)) == 6


@pytest.mark.parametrize("broken, message", [
    ("default: nope\ncustom_runs: {min: 3, max: 10}\ntiers:\n"
     "  - {name: low, runs_per_prompt: 3, models: all, mention_method: rules}\n", "默认档位"),
    ("default: low\ncustom_runs: {min: 3, max: 10}\ntiers:\n"
     "  - {name: low, runs_per_prompt: 3, models: all, mention_method: rules}\n"
     "  - {name: low, runs_per_prompt: 5, models: all, mention_method: rules}\n", "重名"),
    ("default: low\ncustom_runs: {min: 3, max: 10}\ntiers:\n"
     "  - {name: low, runs_per_prompt: 12, models: all, mention_method: rules}\n", "超出自定义范围"),
])
def test_contradictory_config_fails_at_load(tmp_path: Path, broken: str, message: str):
    path = tmp_path / "tiers.yaml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_tiers(path)


def test_tier_choice_is_frozen_and_round_trips():
    choice = TierChoice(name="medium", runs_per_prompt=5, mention_method=MentionMethod.RULES, custom=False)
    assert TierChoice.model_validate(choice.model_dump(mode="json")) == choice


def test_retest_swaps_retired_models_for_the_tiers_current_ones():
    """再测时上一单测过、现在已移出清单的型号(工程原则 9),换成这家同一档现在的型号;还在的照旧;
    换了什么一并返回(用户 2026-11-13 定 甲)。"""
    config = load_tiers()
    low, medium, high = (config.get(n) for n in ("low", "medium", "high"))
    unchanged = [Target(surface="a", model="a3"), Target(surface="a", model="a1"), Target(surface="b", model="b1")]
    assert refresh_targets(unchanged, medium, ADAPTERS, custom=False) == (unchanged, [])
    # 中档:a 的主力 a3 还在,消费端默认那个旧型号移出了 → 补上现在的消费端默认 a1;b 原样
    targets, swaps = refresh_targets(
        [Target(surface="a", model="a3"), Target(surface="a", model="a-old"), Target(surface="b", model="b1")],
        medium, ADAPTERS, custom=False)
    assert targets == [Target(surface="a", model="a3"), Target(surface="a", model="a1"),
                       Target(surface="b", model="b1")]
    assert swaps == [ModelSwap(surface="a", retired=["a-old"], current=["a1"])]
    # 高档 = 全部当前:两个旧的移出,补上这家现在清单里还没测的全部
    targets, swaps = refresh_targets(
        [Target(surface="a", model="a-x"), Target(surface="a", model="a2"), Target(surface="a", model="a-y")],
        high, ADAPTERS, custom=False)
    assert targets == [Target(surface="a", model=m) for m in ("a2", "a1", "a3")]
    assert swaps == [ModelSwap(surface="a", retired=["a-x", "a-y"], current=["a1", "a3"])]
    # 低档:这一档现在要测的型号这单里已经有了 → 只是少测一个,current 为空
    targets, swaps = refresh_targets([Target(surface="a", model="a1"), Target(surface="a", model="a-old")],
                                     low, ADAPTERS, custom=False)
    assert targets == [Target(surface="a", model="a1")]
    assert swaps == [ModelSwap(surface="a", retired=["a-old"], current=[])]
    # 自定义的单(型号是用户挑的):一个换一个、不多测,依次挑消费端默认、主力、清单里其余的,跳过已经有的
    targets, swaps = refresh_targets([Target(surface="a", model="a-old"), Target(surface="b", model="b1")],
                                     medium, ADAPTERS, custom=True)
    assert targets == [Target(surface="a", model="a1"), Target(surface="b", model="b1")]
    assert swaps == [ModelSwap(surface="a", retired=["a-old"], current=["a1"])]
    targets, swaps = refresh_targets([Target(surface="a", model="a1"), Target(surface="a", model="a-x"),
                                      Target(surface="a", model="a-y")], high, ADAPTERS, custom=True)
    assert targets == [Target(surface="a", model=m) for m in ("a1", "a3", "a2")]
    assert swaps == [ModelSwap(surface="a", retired=["a-x", "a-y"], current=["a3", "a2"])]
