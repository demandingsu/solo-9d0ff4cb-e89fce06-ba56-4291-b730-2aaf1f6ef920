"""解析器与合并器的单元测试（不依赖网络）。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.merger import MergeInput, merge_notices
from app.models import NoticeKind, NoticeRecord, ProjectStatus
from app.parser import (
    extract_detail_links,
    parse_datetime,
    parse_detail,
    same_site,
)

_CST = timezone(timedelta(hours=8))   # UTC+8（上海）
_EST = timezone(timedelta(hours=-5))  # UTC-5（纽约，冬令时）


# --------------------------------------------------------------------------- #
# parse_datetime
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-10 09:30", datetime(2026, 11, 10, 9, 30)),
        ("2026/11/10 09:30:00", datetime(2026, 11, 10, 9, 30, 0)),
        ("2026年11月15日 14时00分", datetime(2026, 11, 15, 14, 0)),
        ("发布时间：2026年11月03日", datetime(2026, 11, 3)),
        ("截止 2026-11-18T10:00", datetime(2026, 11, 18, 10, 0)),
        ("无时间文本", None),
    ],
)
def test_parse_datetime(text, expected):
    assert parse_datetime(text) == expected


# --------------------------------------------------------------------------- #
# parse_datetime：时区偏移（跨时区公告不得丢掉偏移量）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-05T09:00:00+08:00", datetime(2026, 11, 5, 9, 0, tzinfo=_CST)),
        ("2026-11-04T21:00:00-05:00", datetime(2026, 11, 4, 21, 0, tzinfo=_EST)),
        ("2026-11-05 09:00:00+0800", datetime(2026, 11, 5, 9, 0, tzinfo=_CST)),
        ("2026-11-05T09:00:00Z", datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)),
        ("发布时间：2026-11-02T09:00:00+08:00（北京时间）", datetime(2026, 11, 2, 9, 0, tzinfo=_CST)),
        ("2026-11-05T09:00:00.123+08:00", datetime(2026, 11, 5, 9, 0, tzinfo=_CST)),
    ],
)
def test_parse_datetime_keeps_timezone_offset(text, expected):
    result = parse_datetime(text)
    assert result == expected
    assert result.utcoffset() is not None  # 偏移量必须保留


def test_parse_datetime_without_offset_stays_naive():
    result = parse_datetime("2026-11-10 09:30")
    assert result is not None and result.utcoffset() is None


def test_same_site():
    base = "http://localhost:8080/list.html"
    assert same_site("http://localhost:8080/detail/a.html", base)
    assert not same_site("https://example.gov/x.html", base)
    assert not same_site("http://localhost:9000/x.html", base)
    assert not same_site("/relative/x.html", base)


# --------------------------------------------------------------------------- #
# 列表页链接发现
# --------------------------------------------------------------------------- #
def test_extract_detail_links_filters_external_and_nav():
    html = """
    <html><body>
      <a href="/login.html">登录</a>
      <a href="https://example.gov/other.html">外站</a>
      <a href="/detail/a.html">公告A</a>
      <a href="detail/b.html?x=1">公告B</a>
      <a href="/detail/a.html">公告A重复</a>
      <a href="http://localhost:8080/list.html">列表自身</a>
    </body></html>
    """
    links = extract_detail_links(html, "http://localhost:8080/list.html")
    urls = [u for u, _, _ in links]
    assert urls == [
        "http://localhost:8080/detail/a.html",
        "http://localhost:8080/detail/b.html?x=1",
    ]
    assert {u: text for u, text, _ in links}["http://localhost:8080/detail/a.html"] == "公告A"


# --------------------------------------------------------------------------- #
# 详情页解析：三种页面形态
# --------------------------------------------------------------------------- #
LABEL_HTML = """
<html><head><title>测试项目公开招标公告</title></head><body>
<h1>测试项目公开招标公告</h1>
<table>
  <tr><th>项目编号</th><td>GSB-2026-001</td></tr>
  <tr><th>发布时间</th><td>2026-11-01 10:00</td></tr>
  <tr><th>投标截止时间</th><td>2026-11-10 09:30</td></tr>
</table>
</body></html>
"""

DATA_ATTR_HTML = """
<html><head><title>测试项目更正公告</title></head><body>
<h1>测试项目更正公告</h1>
<ul>
  <li data-field="project-code" data-value="GSB-2026-001"></li>
  <li data-field="published-at" data-value="2026-11-06 09:00"></li>
  <li data-field="deadline" data-value="2026-11-15 14:00"></li>
</ul>
</body></html>
"""

NARRATIVE_HTML = """
<html><head><title>测试项目竞争性磋商公告</title></head><body>
<h1>测试项目竞争性磋商公告</h1>
<p>项目编号：GSB-2026-007</p>
<p>发布时间：2026-11-05 08:45</p>
<p>供应商应在 2026年11月18日 10时00分 之前完成响应文件递交，逾期不再受理。</p>
</body></html>
"""

CANCEL_HTML = """
<html><head><title>测试项目取消公告</title></head><body>
<h1>测试项目取消公告</h1>
<ul>
  <li data-field="project-code" data-value="GSB-2026-002"></li>
  <li data-field="published-at" data-value="2026-11-04 11:00"></li>
  <li data-field="cancelled" data-value="true"></li>
  <li data-field="cancellation-reason" data-value="预算收回"></li>
</ul>
<p>因采购计划调整，本项目取消采购。</p>
</body></html>
"""


def test_parse_detail_label_layout():
    d = parse_detail(LABEL_HTML, "http://x/detail/a.html")
    assert d.project_code == "GSB-2026-001"
    assert d.published_at == datetime(2026, 11, 1, 10, 0)
    assert d.deadline == datetime(2026, 11, 10, 9, 30)
    assert d.cancelled is False
    assert d.kind_hint == "original"


def test_parse_detail_data_attr_layout():
    d = parse_detail(DATA_ATTR_HTML, "http://x/detail/b.html")
    assert d.published_at == datetime(2026, 11, 6, 9, 0)
    assert d.deadline == datetime(2026, 11, 15, 14, 0)
    assert d.kind_hint == "correction"


def test_parse_detail_narrative_fallback():
    d = parse_detail(NARRATIVE_HTML, "http://x/detail/c.html")
    assert d.project_code == "GSB-2026-007"
    assert d.published_at == datetime(2026, 11, 5, 8, 45)
    assert d.deadline == datetime(2026, 11, 18, 10, 0)
    assert d.kind_hint == "original"


def test_parse_detail_cancellation():
    d = parse_detail(CANCEL_HTML, "http://x/detail/d.html")
    assert d.cancelled is True
    assert d.cancellation_reason == "预算收回"
    assert d.kind_hint == "cancellation"


TZ_AWARE_HTML = """
<html><head><title>跨境项目更正公告</title></head><body>
<h1>跨境项目更正公告</h1>
<ul>
  <li data-field="project-code" data-value="GSB-2026-004"></li>
  <li data-field="published-at" data-value="2026-11-04T21:00:00-05:00"></li>
  <li data-field="deadline" data-value="2026-11-25T10:00:00-05:00"></li>
</ul>
</body></html>
"""


def test_parse_detail_preserves_timezone_offsets():
    d = parse_detail(TZ_AWARE_HTML, "http://x/detail/tz.html")
    assert d.published_at == datetime(2026, 11, 4, 21, 0, tzinfo=_EST)
    assert d.deadline == datetime(2026, 11, 25, 10, 0, tzinfo=_EST)
    assert d.published_at.utcoffset() == timedelta(hours=-5)


def test_parse_detail_requires_title():
    with pytest.raises(Exception):
        parse_detail("<html><body>无标题</body></html>", "http://x/e.html")


def test_deadline_fallback_ignores_qualification_history_dates():
    html = """
    <html><head><title>测试项目更正公告</title></head><body>
    <h1>测试项目更正公告</h1>
    <p>项目编号：GSB-2026-010</p>
    <p>发布时间：2026-11-03 15:30</p>
    <p>近三年（2023年1月1日至投标截止日）至少完成 2 项同类业绩。</p>
    <p>本更正仅澄清资格要求，未调整投标截止时间，按原招标文件执行。</p>
    </body></html>
    """
    d = parse_detail(html, "http://x/f.html")
    # 业绩说明里的 2023-01-01 不得被当成截止时间；本页确实未给出截止时间
    assert d.deadline is None


# --------------------------------------------------------------------------- #
# 合并器
# --------------------------------------------------------------------------- #
def _notice(kind, published, deadline=None, cancelled=False, url=None, reason=None):
    idx = id(_notice)
    return NoticeRecord(
        url=url or f"http://x/n{idx}.html",
        kind=kind,
        title=f"公告-{kind.value}",
        published_at=published,
        deadline=deadline,
        cancelled=cancelled,
        cancellation_reason=reason,
    )


def test_merge_correction_without_deadline_keeps_effective_time():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 1, 10), datetime(2026, 11, 10, 9, 30)),
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 3, 15), None),
            ]
        )
    )
    assert result.status == ProjectStatus.OPEN
    assert result.can_bid is True
    assert result.effective_deadline == datetime(2026, 11, 10, 9, 30)


def test_merge_later_correction_overrides_deadline_even_if_unsorted_input():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 6, 9), datetime(2026, 11, 15, 14)),
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 3, 15), None),
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 1, 10), datetime(2026, 11, 10, 9, 30)),
            ]
        )
    )
    assert result.status == ProjectStatus.OPEN
    assert result.effective_deadline == datetime(2026, 11, 15, 14, 0)
    # 输出按发布时间排序
    assert [n.published_at for n in result.ordered_notices] == sorted(n.published_at for n in result.ordered_notices)


def test_merge_cancellation_marks_closed():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 2, 9), datetime(2026, 11, 12, 14, 30)),
                _notice(
                    NoticeKind.CANCELLATION,
                    datetime(2026, 11, 4, 11),
                    None,
                    cancelled=True,
                    reason="预算收回",
                ),
            ]
        )
    )
    assert result.status == ProjectStatus.CLOSED
    assert result.can_bid is False
    assert result.cancelled is True


def test_merge_any_failure_forces_pending_and_no_deadline():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 5, 8), datetime(2026, 11, 18, 10)),
            ],
            failures=["http://x/g003-missing.html"],
        )
    )
    assert result.status == ProjectStatus.PENDING
    assert result.can_bid is False
    assert result.effective_deadline is None  # 旧时间不得作为最终期限
    assert any("抓取失败" in w for w in result.warnings)


def test_merge_missing_publish_time_forces_pending():
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 5, 8), datetime(2026, 11, 18, 10)),
                _notice(NoticeKind.CORRECTION, None, datetime(2026, 11, 20, 10)),
            ]
        )
    )
    assert result.status == ProjectStatus.PENDING
    assert result.effective_deadline is None


def test_merge_no_notices_is_not_found():
    result = merge_notices(MergeInput(notices=[]))
    assert result.status == ProjectStatus.NOT_FOUND
    assert result.effective_deadline is None


def test_merge_no_original_forces_pending():
    result = merge_notices(
        MergeInput(
            notices=[_notice(NoticeKind.CORRECTION, datetime(2026, 11, 3), datetime(2026, 11, 20, 10))]
        )
    )
    assert result.status == ProjectStatus.PENDING


def test_merge_original_without_deadline_and_no_supplement_is_pending():
    result = merge_notices(
        MergeInput(notices=[_notice(NoticeKind.ORIGINAL, datetime(2026, 11, 1))])
    )
    assert result.status == ProjectStatus.PENDING
    assert result.effective_deadline is None


# --------------------------------------------------------------------------- #
# 合并器：跨时区发布时间
# --------------------------------------------------------------------------- #
def test_merge_aware_notices_sorted_by_actual_moment_not_face_value():
    """字面时间更早、实际时刻更晚的更正必须排在后面，最终期限取实际最新公告。"""
    # 纽约 11-04 21:00(-05:00) = UTC 11-05 02:00，实际晚于上海 11-05 09:00(+08:00) = UTC 11-05 01:00
    shanghai_corr = _notice(
        NoticeKind.CORRECTION,
        datetime(2026, 11, 5, 9, 0, tzinfo=_CST),
        datetime(2026, 11, 20, 17, 0, tzinfo=_CST),
        url="http://x/corr-shanghai.html",
    )
    newyork_corr = _notice(
        NoticeKind.CORRECTION,
        datetime(2026, 11, 4, 21, 0, tzinfo=_EST),
        datetime(2026, 11, 25, 10, 0, tzinfo=_EST),
        url="http://x/corr-newyork.html",
    )
    original = _notice(
        NoticeKind.ORIGINAL,
        datetime(2026, 11, 2, 9, 0, tzinfo=_CST),
        datetime(2026, 11, 18, 17, 0, tzinfo=_CST),
        url="http://x/original.html",
    )
    result = merge_notices(MergeInput(notices=[newyork_corr, original, shanghai_corr]))

    assert result.status == ProjectStatus.OPEN
    assert [str(n.url) for n in result.ordered_notices] == [
        "http://x/original.html",
        "http://x/corr-shanghai.html",
        "http://x/corr-newyork.html",
    ]
    # 有效期限来自实际时刻最新的纽约更正，且偏移量保留
    assert result.effective_deadline == datetime(2026, 11, 25, 10, 0, tzinfo=_EST)
    assert result.effective_deadline.utcoffset() == timedelta(hours=-5)


def test_merge_mixed_timezone_with_site_timezone_interprets_naive():
    """混有无时区公告时，按请求提供的站点时区解释后参与排序。"""
    original = _notice(
        NoticeKind.ORIGINAL,
        datetime(2026, 11, 3, 10, 0),  # 无时区
        datetime(2026, 11, 17, 15, 0),
        url="http://x/o.html",
    )
    correction = _notice(
        NoticeKind.CORRECTION,
        datetime(2026, 11, 6, 10, 0, tzinfo=_CST),
        datetime(2026, 11, 24, 15, 0, tzinfo=_CST),
        url="http://x/c.html",
    )
    result = merge_notices(
        MergeInput(notices=[correction, original], site_timezone="Asia/Shanghai")
    )

    assert result.status == ProjectStatus.OPEN
    # 无时区时间被解释为站点时区（上海 = UTC+8），偏移量体现在结果中
    first = result.ordered_notices[0]
    assert first.published_at == datetime(2026, 11, 3, 10, 0, tzinfo=_CST)
    assert first.deadline == datetime(2026, 11, 17, 15, 0, tzinfo=_CST)
    assert result.effective_deadline == datetime(2026, 11, 24, 15, 0, tzinfo=_CST)
    assert any("Asia/Shanghai" in w for w in result.warnings)


def test_merge_mixed_timezone_without_site_timezone_is_pending():
    """混有时区口径且未提供站点时区 → 待核实，有效期限置空。"""
    original = _notice(
        NoticeKind.ORIGINAL,
        datetime(2026, 11, 3, 10, 0),
        datetime(2026, 11, 17, 15, 0),
        url="http://x/o.html",
    )
    correction = _notice(
        NoticeKind.CORRECTION,
        datetime(2026, 11, 6, 10, 0, tzinfo=_CST),
        datetime(2026, 11, 24, 15, 0, tzinfo=_CST),
        url="http://x/c.html",
    )
    result = merge_notices(MergeInput(notices=[correction, original]))

    assert result.status == ProjectStatus.PENDING
    assert result.can_bid is False
    assert result.effective_deadline is None  # 顺序不明，任何期限都不可信
    assert any("时区" in w for w in result.warnings)
    # 待核实也要返回可追溯的公告列表，且排序不能因混合口径崩溃
    assert len(result.ordered_notices) == 2


def test_merge_all_naive_keeps_local_time_semantics():
    """全为无时区公告：沿用原有本地时间口径，不附加任何时区。"""
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 1, 10), datetime(2026, 11, 10, 9, 30)),
                _notice(NoticeKind.CORRECTION, datetime(2026, 11, 6, 9), datetime(2026, 11, 15, 14)),
            ],
            site_timezone="Asia/Shanghai",  # 即使提供了站点时区，全无时区公告也不改口径
        )
    )
    assert result.status == ProjectStatus.OPEN
    assert result.effective_deadline == datetime(2026, 11, 15, 14)
    assert result.effective_deadline.utcoffset() is None


def test_merge_pending_path_tolerates_mixed_timezone_notices():
    """抓取失败叠加时区混杂时，待核实路径的排序不得抛出 TypeError。"""
    result = merge_notices(
        MergeInput(
            notices=[
                _notice(NoticeKind.ORIGINAL, datetime(2026, 11, 1, 10), datetime(2026, 11, 10, 9, 30)),
                _notice(
                    NoticeKind.CORRECTION,
                    datetime(2026, 11, 6, 9, tzinfo=_CST),
                    datetime(2026, 11, 15, 14, tzinfo=_CST),
                ),
            ],
            failures=["http://x/missing.html"],
        )
    )
    assert result.status == ProjectStatus.PENDING
    assert result.effective_deadline is None
    assert len(result.ordered_notices) == 2
