"""业务合并逻辑：按发布时间合并原公告与更正公告。

规则（对应需求）：
* 按发布时间从早到晚依次应用公告；
* 发布时间带时区偏移的公告按**实际时刻**（换算到同一时间基准后）排序，
  不同时区发布的更正不会因字面时间先后被排错；
* 同一项目混有“带时区”与“不带时区”公告时：
  - 请求提供了 IANA 站点时区（site_timezone）→ 无时区时间按站点时区解释后参与排序；
  - 未提供站点时区 → 无法确定真实先后顺序，整体标记“待核实”，有效期限置空；
* 全部公告均不带时区时，沿用原有本地时间口径直接比较；
* 更正公告未提及截止时间时，沿用此前已经生效的截止时间；
* 更正公告给出新截止时间（如延期）时，以新时间覆盖；
* 出现取消采购信息后，项目不可投标；
* 任一关联详情页抓取/解析失败 → 整体标记“待核实”，且不返回任何截止时间，
  避免把旧时间当作最终期限；
* 发布时间缺失的公告无法参与时间合并，同样按待核实处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import NoticeKind, NoticeRecord, ProjectStatus


@dataclass
class MergeInput:
    """合并器输入：成功解析的公告 + 失败的关联页面。"""

    notices: list[NoticeRecord]
    failures: list[str] = field(default_factory=list)  # 抓取/解析失败的详情页 URL
    site_timezone: str | None = None  # IANA 站点时区名，用于解释无时区的发布时间/截止时间


@dataclass
class MergeResult:
    status: ProjectStatus
    can_bid: bool
    effective_deadline: datetime | None
    cancelled: bool
    ordered_notices: list[NoticeRecord]
    warnings: list[str]


def _is_aware(dt: datetime) -> bool:
    """datetime 是否携带有效时区偏移。"""
    return dt.tzinfo is not None and dt.utcoffset() is not None


def _display_key(notice: NoticeRecord) -> datetime:
    """待核实路径的兜底排序键。

    带时区时间先换算为 UTC 再去掉时区，与无时区本地时间保持同维度，
    保证混有时区口径时排序也不会抛出 TypeError（此时顺序仅供人工追溯）。
    """
    dt = notice.published_at
    if dt is None:
        return datetime.min
    if _is_aware(dt):
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _load_site_tz(name: str | None) -> tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:  # pragma: no cover - 请求模型已校验，此处兜底
        return None


def _localize_notice(notice: NoticeRecord, tz: tzinfo) -> NoticeRecord:
    """把公告中的无时区时间解释为站点时区（带时区时间保持原偏移不变）。"""
    updates: dict[str, datetime] = {}
    if notice.published_at is not None and not _is_aware(notice.published_at):
        updates["published_at"] = notice.published_at.replace(tzinfo=tz)
    if notice.deadline is not None and not _is_aware(notice.deadline):
        updates["deadline"] = notice.deadline.replace(tzinfo=tz)
    return notice.model_copy(update=updates) if updates else notice


def merge_notices(data: MergeInput) -> MergeResult:
    warnings = list(data.failures and [f"关联详情页抓取失败：{u}" for u in data.failures])

    # 发布时间缺失 → 无法保证合并顺序，按待核实处理
    undated = [n for n in data.notices if n.published_at is None]
    if undated:
        warnings.extend(f"公告缺少可解析的发布时间：{n.url}" for n in undated)

    if data.failures or undated:
        return MergeResult(
            status=ProjectStatus.PENDING,
            can_bid=False,
            effective_deadline=None,  # 关键：失败时绝不返回旧时间
            cancelled=any(n.cancelled for n in data.notices),
            ordered_notices=sorted(data.notices, key=_display_key),
            warnings=warnings,
        )

    # --------------------------------------------------------------- #
    # 时区口径归一：带时区时间按实际时刻比较；无时区时间需要站点时区解释
    # --------------------------------------------------------------- #
    notices = list(data.notices)
    has_aware = any(_is_aware(n.published_at) for n in notices)  # 调用方已保证 published_at 非空
    has_naive = any(not _is_aware(n.published_at) for n in notices)

    if has_aware and has_naive:
        site_tz = _load_site_tz(data.site_timezone)
        if site_tz is None:
            warnings.append(
                "公告发布时间时区口径不一致（部分带偏移、部分不带），且未提供站点时区 "
                "site_timezone，无法确定各公告的真实先后顺序，请核实后重试"
            )
            return MergeResult(
                status=ProjectStatus.PENDING,
                can_bid=False,
                effective_deadline=None,  # 顺序不明 → 任何截止时间都不可信
                cancelled=any(n.cancelled for n in notices),
                ordered_notices=sorted(notices, key=_display_key),
                warnings=warnings,
            )
        warnings.append(
            f"部分公告发布时间未携带时区偏移，已按站点时区 {data.site_timezone} 解释后参与排序"
        )
        notices = [_localize_notice(n, site_tz) for n in notices]
    elif has_aware:
        # 全部发布时间带时区：个别公告的截止时间若不带时区且提供了站点时区，一并解释，
        # 保证输出的有效截止时间口径一致
        site_tz = _load_site_tz(data.site_timezone)
        if site_tz is not None:
            notices = [_localize_notice(n, site_tz) for n in notices]
    # 全部不带时区 → 沿用原有本地时间口径，不做任何转换

    # 归一后要么全部带时区（按实际时刻比较），要么全部不带（本地口径比较）
    ordered = sorted(notices, key=lambda n: n.published_at or datetime.min)
    if not ordered:
        return MergeResult(
            status=ProjectStatus.NOT_FOUND,
            can_bid=False,
            effective_deadline=None,
            cancelled=False,
            ordered_notices=[],
            warnings=warnings,
        )

    originals = [n for n in ordered if n.kind == NoticeKind.ORIGINAL]
    if not originals:
        warnings.append("未识别到原公告，仅有更正类公告，信息链不完整")
        return MergeResult(
            status=ProjectStatus.PENDING,
            can_bid=False,
            effective_deadline=None,
            cancelled=False,
            ordered_notices=ordered,
            warnings=warnings,
        )

    # 逐条应用：截止时间仅在公告明确提及时更新，否则沿用已生效时间
    current_deadline: datetime | None = None
    cancelled = False
    cancel_reason: str | None = None

    for notice in ordered:
        if notice.deadline is not None:
            current_deadline = notice.deadline
        if notice.cancelled:
            cancelled = True
            cancel_reason = notice.cancellation_reason or cancel_reason

    # 原公告本身就没有截止时间，且没有任何更正补充 → 无法给出有效期限
    if current_deadline is None:
        warnings.append("原公告及全部更正均未给出投标截止时间")
        return MergeResult(
            status=ProjectStatus.PENDING,
            can_bid=False,
            effective_deadline=None,
            cancelled=cancelled,
            ordered_notices=ordered,
            warnings=warnings,
        )

    if cancelled:
        if cancel_reason:
            warnings.append(f"采购已取消：{cancel_reason}")
        else:
            warnings.append("采购已取消，不可投标")
        return MergeResult(
            status=ProjectStatus.CLOSED,
            can_bid=False,
            # 取消后不可投标：不提供“有效截止时间”，避免调用方误用旧时间安排投标；
            # 最后已知时间仍保留在 notices 明细中可供追溯。
            effective_deadline=None,
            cancelled=True,
            ordered_notices=ordered,
            warnings=warnings,
        )

    return MergeResult(
        status=ProjectStatus.OPEN,
        can_bid=True,
        effective_deadline=current_deadline,
        cancelled=False,
        ordered_notices=ordered,
        warnings=warnings,
    )
