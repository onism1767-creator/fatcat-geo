"""L2 对单条运行的有效性判定与对外错误码(产品文档 15.2;架构书附录 v0.4.26 第 9 节 C6;S3)。

- 有效回答 = outcome 为 SUCCESS 且回答正文非空(只有空白也算空)。拒答、空回答、接口错误都是无效:
  不进任何比例的分母,数量单独给(7.3)。「空回答算无效」是 L2 的判定,SurfaceRun 原样不动——
  适配器照旧只翻译平台返回,不替我们判有效没效。
- 对外错误码只在这里从 outcome + error_type + http_status 映射得出,SurfaceRun 不存它(C6)。
  账户问题(我们自己的 key / 余额)不是产品文档那三类,保留我们自己的码。
"""

from __future__ import annotations

from enum import StrEnum

from engine.models import ACCOUNT_ERROR_TYPES, Outcome, SurfaceRun


class PublicErrorCode(StrEnum):
    """面向用户 / 导出文件的错误码(产品文档 15.2 错误码表 + 我方账户五类)。"""

    AI_PROVIDER_ERROR = "AI_PROVIDER_ERROR"  # 模型服务返回错误:重试耗尽或永久失败
    AI_PROVIDER_RATE_LIMITED = "AI_PROVIDER_RATE_LIMITED"  # 模型服务限流(HTTP 429)后仍失败
    AI_RESPONSE_INVALID = "AI_RESPONSE_INVALID"  # 拒答或空回答:不计入分母,单独显示数量
    # 我方账户问题(产品文档没有这一类),与 RunErrorType 里的账户类同名一一对应,各自要用户做的事不同:
    ACCOUNT_AUTH = "ACCOUNT_AUTH"  # key 无效 / 没权限:换 key
    ACCOUNT_CREDIT = "ACCOUNT_CREDIT"  # 额度不足 / 欠费 / 到了花费上限:充值或调高上限
    ACCOUNT_REGION = "ACCOUNT_REGION"  # 不对所在国家或地区开放:换 key 没用
    ACCOUNT_RESTRICTED = "ACCOUNT_RESTRICTED"  # 账户或项目被平台限制:去控制台处理
    ACCOUNT_QUOTA = "ACCOUNT_QUOTA"  # 这个周期的额度用完:等重置或申请提额


def is_empty_answer(run: SurfaceRun) -> bool:
    """SUCCESS 但回答正文为空(含只有空白):平台说成功了,却一个字没给。"""
    return run.outcome is Outcome.SUCCESS and (run.answer is None or not run.answer.answer_text.strip())


def is_valid_answer(run: SurfaceRun) -> bool:
    """比例指标分母的唯一合法成员:真正拿到了实质回答。判定器只对这样的运行产出观测。"""
    return run.outcome is Outcome.SUCCESS and not is_empty_answer(run)


def public_error_code(run: SurfaceRun) -> PublicErrorCode | None:
    """有效回答 → None;其余按产品文档三类,账户问题保留自己的码。"""
    if is_valid_answer(run):
        return None
    if run.outcome in (Outcome.SUCCESS, Outcome.REFUSED):  # SUCCESS 到这里只剩空回答
        return PublicErrorCode.AI_RESPONSE_INVALID
    error = run.error
    assert error is not None  # SurfaceRun 校验保证:非 SUCCESS 必带 error
    if error.error_type in ACCOUNT_ERROR_TYPES:  # 账户类同名一一对应
        return PublicErrorCode(error.error_type.value)
    if error.http_status == 429:
        return PublicErrorCode.AI_PROVIDER_RATE_LIMITED
    return PublicErrorCode.AI_PROVIDER_ERROR
