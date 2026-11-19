"""数据契约:所有跨模块传递的数据形状只在这里定义一次(按领域分文件,从这里统一导出)。

三个对外契约是 FactBase(事实库)、AuditFinding / AuditReport(审计)、ChangePackage(改动包清单);
其余是它们的零件和中间产物(站点快照、页面抽取结果、兄弟模块文件的只读形状、证据注册表条目)。
全部不可变、未知字段拒收;改这里 = 改所有下游。
"""

from butter.contracts.audit import AuditFinding, AuditReport, CheckName, FindingLevel
from butter.contracts.cards import (
    Batch, BatchFreeze, BatchPhase, BatchPlan, BatchState, Card, CardActor, CardDecision, CardEvent, CardEventType,
    CardSpec, CardsView, CardValidation, Conditions, ControlInfo, Deployment, DeployMark, Direction, EvidenceRef,
    FrozenBrands, FrozenPanel, Intervention, LegSample, MeasureDone, MeasureFailed, Measurement, MeasureStart,
    MethodDirection, MethodExperiment, MethodSummary, MethodValidation, OwnerNote, PanelSource, Reading,
    SkippedEvent, Surface,
)
from butter.contracts.changepkg import (
    ChangeAction, ChangeItem, ChangeKind, ChangePackage, FactRef, NotGenerated, PackageBundle, ValidationState,
)
from butter.contracts.common import Frozen, PageRole
from butter.contracts.compare import (
    BatchFacts, CompareResult, CompareRules, DefaultState, DefaultValue, LabelCode, LegData, LegResult, LegRule,
    Metric, NoiseTier, Outcome, Primary, QuestionObs, Thresholds,
)
from butter.contracts.draft import (
    DRAFT_FIELDS, CheckContext, CheckOutcome, CheckResult, DraftAttempt, DraftedItem, DraftFact, DraftField,
    DraftLimits, DraftOutcome, DraftProposal, DraftTask, OnFail, RuleId, Verdict, VoidedDraft,
)
from butter.contracts.extract import (
    AttrText, HiddenText, JsonLdBlock, Link, MetaTag, PageExtract, RawDate, StructuredNode, TextBlock,
)
from butter.contracts.factbase import (
    EntityFacts, FactBase, FactField, FactValue, Gap, MergedInput, PageFacts, ProductFacts, SourceKind, SpecFact,
)
from butter.contracts.jobs import Job, JobEvent, JobKind, JobState
from butter.contracts.llm import Completion, ErrorKind, ModelPrice, Pricing, Provider
from butter.contracts.questions import GeneratedQuestion, QuestionIntent, QuestionTemplates, TemplateSet
from butter.contracts.registry import (
    ACCESS_CHECK_NAMES, DATA_CONDITIONS, EVIDENCE_LEVELS, OUTPUT_LEVELS, REQUIRED_LANGS, EntryKind, EvidenceEntry,
    PromptIntent, RegistryOrigin, RegistrySource, Stage,
)
from butter.contracts.siblings import PotatoBrand, PotatoBrands, Question, QuestionBoard, ToastBrandDomains
from butter.contracts.snapshot import (
    FetchedPage, RobotsFetch, RobotsState, SitemapEntry, SiteSnapshot, SnapshotSource, StopReason,
)
from butter.contracts.tables import ExcludedVendor, Platform, UaEntry, UaPurpose

__all__ = [
    # common
    "Frozen", "PageRole",
    # snapshot
    "FetchedPage", "RobotsFetch", "RobotsState", "SitemapEntry", "SiteSnapshot", "SnapshotSource", "StopReason",
    # extract
    "AttrText", "HiddenText", "JsonLdBlock", "Link", "MetaTag", "PageExtract", "RawDate", "StructuredNode", "TextBlock",
    # siblings
    "PotatoBrand", "PotatoBrands", "Question", "QuestionBoard", "ToastBrandDomains",
    # factbase
    "EntityFacts", "FactBase", "FactField", "FactValue", "Gap", "MergedInput", "PageFacts", "ProductFacts",
    "SourceKind", "SpecFact",
    # audit
    "AuditFinding", "AuditReport", "CheckName", "FindingLevel",
    # changepkg
    "ChangeAction", "ChangeItem", "ChangeKind", "ChangePackage", "FactRef", "NotGenerated", "PackageBundle",
    "ValidationState",
    # tables
    "ExcludedVendor", "Platform", "UaEntry", "UaPurpose",
    # compare / defaults
    "BatchFacts", "CompareResult", "CompareRules", "DefaultState", "DefaultValue", "LabelCode", "LegData",
    "LegResult", "LegRule", "Metric", "NoiseTier", "Outcome", "Primary", "QuestionObs", "Thresholds",
    # draft / check
    "DRAFT_FIELDS", "CheckContext", "CheckOutcome", "CheckResult", "DraftAttempt", "DraftFact", "DraftField",
    "DraftLimits", "DraftOutcome", "DraftProposal", "DraftTask", "DraftedItem", "OnFail", "RuleId", "Verdict",
    "VoidedDraft",
    # jobs
    "Job", "JobEvent", "JobKind", "JobState",
    # llm
    "Completion", "ErrorKind", "ModelPrice", "Pricing", "Provider",
    # cards(批次、实验卡、事件)
    "Batch", "BatchFreeze", "BatchPhase", "BatchPlan", "BatchState", "Card", "CardActor", "CardDecision",
    "CardEvent", "CardEventType", "CardSpec", "CardsView", "CardValidation", "Conditions", "ControlInfo",
    "Deployment", "DeployMark", "Direction", "EvidenceRef", "FrozenBrands", "FrozenPanel", "Intervention",
    "LegSample", "MeasureDone", "MeasureFailed", "Measurement", "MeasureStart", "MethodDirection",
    "MethodExperiment", "MethodSummary", "MethodValidation", "OwnerNote", "PanelSource", "Reading", "SkippedEvent",
    "Surface",
    # questions
    "GeneratedQuestion", "QuestionIntent", "QuestionTemplates", "TemplateSet",
    # registry
    "ACCESS_CHECK_NAMES", "DATA_CONDITIONS", "EVIDENCE_LEVELS", "OUTPUT_LEVELS", "REQUIRED_LANGS", "EntryKind",
    "EvidenceEntry", "PromptIntent", "RegistryOrigin", "RegistrySource", "Stage",
]
