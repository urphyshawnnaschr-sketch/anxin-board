from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

import app.mail_report_renderer as renderer  # noqa: E402
from app.anxin_board_report_v3 import AnxinBoardReportV3Error  # noqa: E402
from app.mail_report_renderer import MailReportRenderError  # noqa: E402


def _git_metrics():
    return {'metrics_hash': 'e' * 64, 'metrics': {'added_lines': 58, 'deleted_lines': 2, 'changed_file_count': 3},
            'source_git_facts': {'branch': 'main', 'from_commit': '1' * 40, 'to_commit': '2' * 40}}


def render_approved_report_for_mail(report, **kwargs):
    # Layout tests already stub the approved-report authority; supply the separately validated metric fixture too.
    kwargs.setdefault('approved_git_metrics', _git_metrics())
    return renderer.render_approved_report_for_mail(report, **kwargs)


def _report(**overrides):
    report = {
        "anxin_board_report_hash": "a" * 64,
        "motto": "非己所安，不加于物",
        "title": "安心看板",
        "module_count": 1, "completed_module_count": 0,
        "active_module_count": 1, "unknown_module_count": 0,
        "project_name": "测试项目",
        "provider": "deepseek", "model_id": "deepseek-flash", "actual_model": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "report_date": "2026-09-06",
        "overall_message": "当前有 1 项工作正在推进或等待后续检查。",
        "modules": [
            {
                "name": "邮件发送",
                "display_stage": "开发中",
                "summary": "正在接入安全的发送配置。",
                "next_step": "完成发送准入检查。",
            }
        ],
        "daily_change": {
            "headline": "邮件配置继续推进",
            "summary": "已补齐配置保存和确定正文生成。",
            "highlights": [{"label": "完成事项", "value": 2, "unit": "项"}],
            "scope_note": "这里只描述今天有依据的变化。",
        },
        "manager_supplement": "同意当前日报。",
        "supplement_version_id": 6,
        "supplement_content_hash": "b" * 64,
        "supplement_source_type": "pm_external_fact",
        "supplement_provided_by": "PM Alice",
        "supplement_provided_at": "2026-09-06T08:50:00+00:00",
        "supplement_provided_timezone": "UTC",
        "confirmed_by": "PM Alice",
        "confirmed_at": "2026-09-06T09:00:00+00:00",
    }
    report.update(overrides)
    return report


def _install_validated(monkeypatch: pytest.MonkeyPatch, report):
    monkeypatch.setattr(renderer, "validate_anxin_board_report_v3", lambda *_args, **_kwargs: report)
    def validate_narrative(value, **kwargs):
        if value is None:
            raise renderer.ApprovedReportNarrativeError()
        return value
    monkeypatch.setattr(renderer, "validate_approved_report_narrative", validate_narrative)
    def validate_metrics(value, **kwargs):
        if value is None:
            raise ValueError('missing approved Git metrics')
        return value
    monkeypatch.setattr(renderer, 'validate_approved_report_git_metrics', validate_metrics)


def _narrative():
    return {"narrative_hash": "d" * 64, "content": {
        "plain_summary": "本次完善邮件发送配置，仍需实际投递验证。",
        "code_change_summary": [], "test_evidence": [], "risks": [],
        "unknown_items": [], "source_warnings": [],
    }}


@pytest.mark.parametrize('provider, actual, version, name', [
    ('deepseek', 'deepseek-flash', 'DeepSeek-V4.1-Flash', 'DeepSeek'),
    ('historic-vendor', 'actual<&model', 'recorded-version<&1', 'historic-vendor'),
])
def test_analysis_model_uses_frozen_actual_identity_and_recorded_version(monkeypatch, provider, actual, version, name):
    from copy import deepcopy
    from html import escape
    report = _report(provider=provider, actual_model=actual, model_version=version, model_id='different-requested-model')
    original = deepcopy(report)
    _install_validated(monkeypatch, report)
    html = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative()).html_body
    assert 'AI 分析模型' in html
    assert escape(name + ' · ' + actual) in html
    assert '版本记录：' + escape(version) in html
    assert 'different-requested-model' not in html
    assert report == original
    assert html.index('AI 分析模型') > html.index('报告信息')


@pytest.mark.parametrize('counts', [(58, 2, 3), (0, 0, 0)])
def test_v11_header_uses_bound_git_metrics_not_module_counts(monkeypatch, counts):
    report = _report()
    _install_validated(monkeypatch, report)
    projection = {'metrics_hash': 'e' * 64, 'metrics': dict(zip(
        ('added_lines', 'deleted_lines', 'changed_file_count'), counts)),
        'source_git_facts': {'branch': 'main', 'from_commit': '1' * 40, 'to_commit': '2' * 40}}
    monkeypatch.setattr(renderer, 'validate_approved_report_git_metrics', lambda value, **kw: value, raising=False)
    html = render_approved_report_for_mail(report, profile={}, approval_snapshot={},
        approved_narrative=_narrative(), approved_git_metrics=projection).html_body
    import re
    cells = re.findall(r'<div style="color:#c2cce0;font-size:11px">(.*?)</div><div[^>]*>(.*?)</div>', html)
    assert cells == list(zip(('本次代码新增', '本次代码删除', '本次改动文件'), map(str, counts)))
    assert '总模块' not in html and '当前并行主线' not in html
    assert 'v11' in html and '本报告绑定的代码版本范围' in html


def test_v11_missing_git_metrics_cannot_render_a_sendable_document(monkeypatch):
    _install_validated(monkeypatch, _report())
    with pytest.raises(MailReportRenderError):
        render_approved_report_for_mail(_report(), profile={}, approval_snapshot={}, approved_narrative=_narrative(), approved_git_metrics=None)


def test_confirmed_module_projection_replaces_stage_copy_and_binds_v11(monkeypatch):
    from copy import deepcopy
    report = _report()
    report['modules'][0]['module_id'] = 'mail'
    _install_validated(monkeypatch, report)
    projection = {
        'module_narrative_hash': 'f' * 64, 'attribution': '已确认的模块说明',
        'confirmed_by': '模块说明审核人', 'confirmed_at': '2026-09-28T12:00:00Z',
        'historical_baseline': True,
        'source': {'exact_head': 'c' * 40},
        'modules': [{'module_id': 'mail', 'summary': '已保存发件配置，并在发送前核对收件版本。',
                     'remaining': '实际投递与收信验收仍待完成。', 'requirement_refs': [0],
                     'source_requirements': [{'rationale': 'Long technical raw baseline details.'}]}],
    }
    original = deepcopy(projection)
    monkeypatch.setattr(renderer, 'validate_approved_module_narrative', lambda value, **kwargs: value, raising=False)
    rendered = render_approved_report_for_mail(report, profile={}, approval_snapshot={},
        approved_narrative=_narrative(), approved_module_narrative=projection)
    assert rendered.schema_version == 'mail_report_render_v11'
    assert rendered.render_identity == f"mail_report_render_v11:{'a' * 64}:{'f' * 64}"
    assert '具体进展与待完善事项' in rendered.html_body
    assert '已有依据' in rendered.html_body and '待完善/待核实' in rendered.html_body
    for key in ('summary', 'remaining'):
        assert projection['modules'][0][key] in rendered.html_body
    assert report['modules'][0]['summary'] not in rendered.html_body
    assert '历史代码盘点' in rendered.html_body
    assert '模块说明审核人' in rendered.html_body and '独立确认' in rendered.html_body
    assert 'Long technical raw baseline details.' not in rendered.html_body
    assert projection == original
    revised = deepcopy(projection)
    revised['module_narrative_hash'] = 'e' * 64
    second = render_approved_report_for_mail(report, profile={}, approval_snapshot={},
        approved_narrative=_narrative(), approved_module_narrative=revised)
    assert second.render_identity != rendered.render_identity
    assert second.render_hash != rendered.render_hash


def test_invalid_module_projection_blocks_mail_render(monkeypatch):
    _install_validated(monkeypatch, _report())
    def reject(*args, **kwargs):
        raise ValueError('invalid synthetic module projection')
    monkeypatch.setattr(renderer, 'validate_approved_module_narrative', reject, raising=False)
    with pytest.raises(MailReportRenderError):
        render_approved_report_for_mail(_report(), profile={}, approval_snapshot={},
            approved_narrative=_narrative(), approved_module_narrative={'module_narrative_hash': 'f' * 64})


def test_approved_specific_work_risks_and_limits_survive_client_render(monkeypatch):
    """Renderer cannot replace approved work with generic module-stage sentences."""
    report = _report()
    _install_validated(monkeypatch, report)
    narrative = {"narrative_hash": "d" * 64, "content": {
        "plain_summary": "本次完善登录页面，外部账户服务仍需联调。",
        "code_change_summary": [{"content": "登录失败时现在会保留已填写的账号。", "source_type": "ai_analysis", "implementation_scope": "前端", "evidence_ids": ["ev-login"]}],
        "test_evidence": [{"content": "新增账号保留的测试代码，尚无执行结果。", "source_type": "git_fact", "evidence_ids": ["ev-test"]}],
        "risks": [{"content": "外部账户服务尚未完成联调。", "source_type": "ai_analysis", "risk_level": "suspected", "evidence_ids": ["ev-risk"]}],
        "unknown_items": [{"content": "暂不能确认手机上的显示效果。", "source_type": "ai_analysis", "evidence_ids": ["ev-ui"]}],
        "source_warnings": [{"content": "本次仅覆盖已选择的代码范围。", "source_type": "fixed_disclaimer", "evidence_ids": ["ev-scope"]}],
    }}
    monkeypatch.setattr(renderer, "validate_approved_report_narrative", lambda value, **_: value)
    rendered = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=narrative)
    for field, value in narrative["content"].items():
        texts = [value] if field == "plain_summary" else [item["content"] for item in value]
        assert all(text in rendered.html_body for text in texts)
    assert rendered.html_body.index("本次工作与进展") < rendered.html_body.index("当前功能状态")
    assert "尚待验证的分析判断" in rendered.html_body
    assert "建议下一步" in rendered.html_body
    assert "今天实际发生了什么" not in rendered.html_body
    assert "<script" not in rendered.html_body


def test_missing_or_mismatched_approved_narrative_cannot_render(monkeypatch):
    _install_validated(monkeypatch, _report())
    with pytest.raises(MailReportRenderError):
        render_approved_report_for_mail(_report(), profile={}, approval_snapshot={})


def test_narrative_change_and_html_escaping_are_part_of_render_identity(monkeypatch):
    _install_validated(monkeypatch, _report())
    first = render_approved_report_for_mail(_report(), profile={}, approval_snapshot={}, approved_narrative=_narrative())
    narrative = _narrative()
    narrative["narrative_hash"] = "e" * 64
    narrative["content"]["plain_summary"] = "<script>此段必须转义</script>"
    second = render_approved_report_for_mail(_report(), profile={}, approval_snapshot={}, approved_narrative=narrative)
    assert '<script>' not in second.html_body
    assert '&lt;script&gt;' in second.html_body
    assert first.html_sha256 != second.html_sha256
    assert first.render_hash != second.render_hash


def test_r01_render_is_deterministic_hash_closed_and_frozen(monkeypatch: pytest.MonkeyPatch):
    report = _report()
    _install_validated(monkeypatch, report)

    first = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative())
    second = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative())

    assert first == second
    assert first.schema_version == "mail_report_render_v11"
    assert first.report_hash == "a" * 64
    assert first.render_identity == f"mail_report_render_v11:{'a' * 64}:none"
    assert len(first.render_hash) == 64
    assert len(first.html_sha256) == 64
    assert first.subject == "测试项目｜2026-09-06 安心看板"
    assert "邮件发送" in first.html_body
    assert "完成发送准入检查" in first.html_body
    assert "这里只描述今天有依据的变化" in first.html_body
    assert "正式确认版" in first.html_body
    assert "由安心看板生成" in first.html_body
    assert "background:#102a56" in first.html_body
    assert "role=\"presentation\"" in first.html_body
    assert "<script" not in first.html_body.lower()
    assert "stylesheet" not in first.html_body.lower()
    with pytest.raises(FrozenInstanceError):
        first.subject = "changed"  # type: ignore[misc]


def test_r02_all_human_visible_text_is_html_escaped(monkeypatch: pytest.MonkeyPatch):
    report = _report(
        project_name="项目<script>alert(1)</script>",
        manager_supplement="<img src=x onerror=alert(1)>",
        modules=[{
            "name": "模块<b>x</b>",
            "display_stage": "开发中",
            "summary": "<script>bad()</script>",
            "next_step": "检查 & 确认",
        }],
    )
    _install_validated(monkeypatch, report)

    rendered = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative())

    assert "<script>" not in rendered.html_body
    assert "<img src=x" not in rendered.html_body
    assert "&lt;script&gt;" in rendered.html_body
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered.html_body
    assert "检查 &amp; 确认" in rendered.html_body


def test_r03_content_change_changes_html_and_render_hash(monkeypatch: pytest.MonkeyPatch):
    first_report = _report()
    _install_validated(monkeypatch, first_report)
    first = render_approved_report_for_mail(first_report, profile={}, approval_snapshot={}, approved_narrative=_narrative())

    second_report = _report(manager_supplement="新的人工补充。")
    _install_validated(monkeypatch, second_report)
    second = render_approved_report_for_mail(second_report, profile={}, approval_snapshot={}, approved_narrative=_narrative())

    assert second.html_sha256 != first.html_sha256
    assert second.render_hash != first.render_hash


def test_r04_validator_rejection_cannot_render_unproven_report(monkeypatch: pytest.MonkeyPatch):
    def reject(*_args, **_kwargs):
        raise AnxinBoardReportV3Error()

    monkeypatch.setattr(renderer, "validate_anxin_board_report_v3", reject)
    with pytest.raises(MailReportRenderError):
        render_approved_report_for_mail(_report(), profile={}, approval_snapshot={}, approved_narrative=_narrative())


def test_r05_missing_render_field_fails_closed_after_authority_validation(monkeypatch: pytest.MonkeyPatch):
    report = _report()
    report.pop("manager_supplement")
    _install_validated(monkeypatch, report)

    with pytest.raises(MailReportRenderError):
        render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative())


def test_r06_stage_tones_are_email_safe_and_do_not_depend_on_report_html(monkeypatch: pytest.MonkeyPatch):
    report = _report(modules=[
        {"name": "完成", "display_stage": "已完成", "summary": "有依据", "next_step": "保持"},
        {"name": "未知", "display_stage": "暂时无法确认", "summary": "证据不足", "next_step": "补证据"},
    ])
    _install_validated(monkeypatch, report)

    rendered = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative())

    assert "#137a43" in rendered.html_body
    assert "#667085" in rendered.html_body
    # A solid fallback remains even where the client drops progressive gradients.
    assert 'bgcolor="#1c2738"' in rendered.html_body


def test_frozen_template_identity_sections_and_approved_git_metrics(monkeypatch):
    _install_validated(monkeypatch, _report())
    rendered = render_approved_report_for_mail(_report(), profile={}, approval_snapshot={}, approved_narrative=_narrative())
    from app.mail_brand_asset import CALLIGRAPHY_CID
    assert f'src="cid:{CALLIGRAPHY_CID}"' in rendered.html_body
    assert '<meta name="anxin-mail-render" content="v11">' in rendered.html_body
    assert 'alt="非己所安，不加于物"' in rendered.html_body
    for heading in ('安心看板', '本次代码变化', '当前功能状态', '本次工作与进展', '项目经理说明', 'AI 分析原文与依据', '报告信息'):
        assert heading in rendered.html_body
    assert '<th' in rendered.html_body and '具体进展与待完善事项' in rendered.html_body
    assert '下一步：完成发送准入检查。' in rendered.html_body
    for label in ('本次代码新增', '本次代码删除', '本次改动文件'):
        assert label in rendered.html_body
    assert '暂不可用</div>' not in rendered.html_body
    # Model-authored highlights must not become GitSnapshot-owned counts.
    assert '完成事项' not in rendered.html_body


@pytest.mark.parametrize('version', ['mail_report_render_v1', 'mail_report_render_v2', 'mail_report_render_v3'])
def test_historical_render_remains_valid_without_rewriting_body(version):
    from dataclasses import replace
    from hashlib import sha256
    original = '<html><body>已冻结的旧报告</body></html>'
    fields = dict(schema_version=version, report_hash='a' * 64,
                  render_identity=f"{version}:{'a' * 64}",
                  html_sha256=sha256(original.encode()).hexdigest(), subject='历史报告')
    value = renderer.MailReportRender(html_body=original,
        render_hash=renderer._expected_render_hash(**fields), **fields)
    assert renderer.validate_mail_report_render(value) is value
    with pytest.raises(MailReportRenderError):
        renderer.validate_mail_report_render(replace(value, html_body=original + 'changed'))

def test_unassociated_feature_judgments_are_preserved_without_changing_module_stage(monkeypatch):
    report = _report()
    _install_validated(monkeypatch, report)
    narrative = _narrative()
    rows = [
        {'feature':'邮件发送（新增范围）', 'stage':'开发中','source_type':'git_fact','implementation_scope':'前端','evidence_ids':['ev-1<script>']},
        {'feature':'邮件发送', 'stage':'开发中','source_type':'ai_analysis','implementation_scope':'后端','evidence_ids':['ev-2']},
        {'feature':'邮件发送', 'stage':'暂时无法确认','source_type':'ai_analysis','implementation_scope':'测试','evidence_ids':['ev-3']},
    ]
    narrative.update(source_task_type='daily_report_generate', source_result={'feature_progress':rows})
    html = render_approved_report_for_mail(report,profile={},approval_snapshot={},approved_narrative=narrative).html_body
    assert '尚未关联到已确认功能模块' in html
    assert '邮件发送（新增范围）' in html
    assert all(ref not in html for ref in ['ev-1', 'ev-2', 'ev-3'])
    assert '开发中' in html and '暂时无法确认' in html
    assert '正在接入安全的发送配置。' in html


def test_unique_exact_feature_is_not_duplicated_and_regenerate_uses_same_source(monkeypatch):
    report = _report()
    _install_validated(monkeypatch, report)
    narrative = _narrative()
    row={'feature':'邮件发送','stage':'开发中','source_type':'git_fact','implementation_scope':'前端','evidence_ids':['ev-exact']}
    narrative.update(source_task_type='daily_report_regenerate',source_result={'new_report':{'feature_progress':[row]},'correction_trace':[]})
    html=render_approved_report_for_mail(report,profile={},approval_snapshot={},approved_narrative=narrative).html_body
    assert '尚未关联到已确认功能模块' not in html
    assert 'ev-exact' not in html

@pytest.mark.parametrize('version', ['v4','v5','v6','v7','v8'])
def test_frozen_cid_render_versions_validate_without_rewrite_and_reject_foreign_cid(version):
    from hashlib import sha256
    original=f'<html><head><meta name="anxin-mail-render" content="{version}"></head><body><img src="cid:{renderer.CALLIGRAPHY_CID}"></body></html>'
    def record(html):
        fields=dict(schema_version=f'mail_report_render_{version}',report_hash='a'*64,
                    render_identity=f'mail_report_render_{version}:'+('a'*64),html_sha256=sha256(html.encode()).hexdigest(),subject='历史报告')
        return renderer.MailReportRender(html_body=html,render_hash=renderer._expected_render_hash(**fields),**fields)
    value=record(original)
    assert renderer.validate_mail_report_render(value) is value
    with pytest.raises((MailReportRenderError,ValueError)):
        renderer.validate_mail_report_render(record(original.replace(renderer.CALLIGRAPHY_CID,'foreign-cid')))


@pytest.mark.parametrize('version', ['v9', 'v10'])
@pytest.mark.parametrize('module_hash', ['none', 'e' * 64])
def test_frozen_v9_v10_retain_module_metrics_and_inline_asset_validation(module_hash, version):
    from hashlib import sha256
    metrics_marker = f'<meta name="anxin-git-metrics-hash" content="{"f" * 64}">' if version == 'v10' else ''
    original = (f'<html><head><meta name="anxin-mail-render" content="{version}">{metrics_marker}'
                f'<meta name="anxin-module-narrative-hash" content="{module_hash}"></head>'
                f'<body><img src="cid:{renderer.CALLIGRAPHY_CID}"></body></html>')
    def record(html):
        fields = dict(schema_version=f'mail_report_render_{version}', report_hash='a' * 64,
                      render_identity=f"mail_report_render_{version}:{'a' * 64}:{module_hash}",
                      html_sha256=sha256(html.encode()).hexdigest(), subject='历史报告')
        return renderer.MailReportRender(html_body=html, render_hash=renderer._expected_render_hash(**fields), **fields)
    value = record(original)
    assert renderer.validate_mail_report_render(value) is value
    for invalid in [original.replace(renderer.CALLIGRAPHY_CID, 'foreign-cid'),
                    original.replace(f'<img src="cid:{renderer.CALLIGRAPHY_CID}">', ''),
                    original.replace(f'content="{module_hash}"', 'content="wrong-module-hash"')]:
        with pytest.raises(MailReportRenderError):
            renderer.validate_mail_report_render(record(invalid))
    if metrics_marker:
        with pytest.raises(MailReportRenderError):
            renderer.validate_mail_report_render(record(original.replace(metrics_marker, '')))


def test_customer_v11_preserves_table_without_presenting_generic_stage_as_detail(monkeypatch):
    report = _report(provider="PRIVATE_PROVIDER", actual_model="PRIVATE_MODEL", approval_snapshot_id=987654,
        modules=[{"name": "功能甲", "display_stage": "已完成", "summary": "当前约定范围已完成。", "next_step": "保持"},
                 {"name": "功能乙", "display_stage": "开发中", "summary": "仍在开发", "next_step": "继续"}])
    _install_validated(monkeypatch, report)
    rendered = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative())
    assert rendered.schema_version == "mail_report_render_v11"
    assert 'PRIVATE_PROVIDER · PRIVATE_MODEL' in rendered.html_body
    assert all(x in rendered.html_body for x in ["功能甲", "功能乙", "本版未另行确认具体模块说明", "本次完善邮件发送配置"])
    assert '约定功能已完成开发' not in rendered.html_body
    assert 'bgcolor="#1c2738"' in rendered.html_body
    for text in ["简洁书法版", "模型厂商", "实际模型", "模型版本", "确认记录", "987654", "通过检查"]:
        assert text not in rendered.html_body
    assert report["modules"][0]["summary"] == "当前约定范围已完成。"


def test_customer_evidence_labels_do_not_expose_internal_ids(monkeypatch):
    _install_validated(monkeypatch, _report())
    narrative = _narrative()
    narrative['content']['code_change_summary'] = [{'content':'保留用户填写的内容。', 'source_type':'ai_analysis', 'implementation_scope':'前端', 'evidence_ids':['internal-sha-' + 'f'*64]}]
    html = render_approved_report_for_mail(_report(), profile={}, approval_snapshot={}, approved_narrative=narrative).html_body
    assert '保留用户填写的内容。' in html and '分析判断' in html
    assert 'internal-sha-' not in html and '依据编号' not in html


def test_customer_header_ignores_module_counts_and_daily_highlights(monkeypatch):
    report = _report(module_count=10, completed_module_count=2, active_module_count=5, unknown_module_count=3)
    _install_validated(monkeypatch, report)
    html = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative()).html_body
    import re
    cells = re.findall(r'<div style="color:#c2cce0;font-size:11px">(.*?)</div><div[^>]*>(.*?)</div>', html)
    assert cells == [('本次代码新增', '58'), ('本次代码删除', '2'), ('本次改动文件', '3')]
    assert all(label not in html for label in ('总模块', '提交次数', '当前并行主线', '暂不可用'))


@pytest.mark.parametrize("summary, expected", [
    ("当前约定范围已完成。", "—"),
    ("这项工作已经完成当前约定范围内的开发和检查，可以作为已完成内容展示。", "—"),
    ("设备在线状态展示已完成，三项自动化测试已通过。", "设备在线状态展示已完成，三项自动化测试已通过。"),
    ("这项功能已完成当前约定范围内的开发；不代表已通过运行测试或验收。", "这项功能已完成当前约定范围内的开发；不代表已通过运行测试或验收。"),
])
def test_completed_summary_hides_only_exact_legacy_generic(summary, expected):
    module = {"name": "设备状态", "display_stage": "已完成", "summary": summary, "next_step": "继续验收"}
    html = renderer._module_html(module)
    assert expected in html
    if expected != summary:
        assert summary not in html
    assert module["summary"] == summary


def test_customer_lead_uses_exact_bound_manager_fact_and_keeps_ai_in_appendix(monkeypatch):
    from copy import deepcopy
    report = _report(
        manager_supplement="模拟验收：检查已运行；尚未真实联调，并非生产发布。",
        supplement_version_id=6, supplement_source_type="pm_external_fact",
        supplement_provided_by="合成验收人", supplement_provided_at="2026-09-28T10:00:00Z",
        supplement_provided_timezone="UTC",
    )
    _install_validated(monkeypatch, report)
    narrative = _narrative()
    narrative["content"]["risks"] = [
        {"content": "测试数据之外的取值仍需验证。", "source_type": "ai_analysis",
         "risk_level": "suspected", "evidence_ids": ["synthetic-risk"]},
        {"content": "请甲方提供联调窗口。", "source_type": "pm_external_fact",
         "risk_level": "needs_client_action", "evidence_ids": ["synthetic-action"]},
    ]
    narrative["content"]["source_warnings"] = [
        {"content": "只分析本次选定范围。", "source_type": "fixed_disclaimer", "evidence_ids": ["synthetic-scope"]}
    ]
    before = deepcopy((report, narrative))
    html = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=narrative).html_body
    assert html.index(report["manager_supplement"]) < html.index("当前功能状态") < html.index(narrative["content"]["plain_summary"])
    assert html.index("请甲方提供联调窗口。") < html.index("当前功能状态")
    assert "项目经理说明" in html and "项目经理提供" in html and "合成验收人" in html
    assert "AI 分析原文与依据" in html and "尚待验证的分析判断" in html
    assert "需要关注的问题" not in html
    for value in narrative["content"].values():
        for text in [value] if isinstance(value, str) else [item["content"] for item in value]:
            assert text in html
    assert html.count(report["manager_supplement"]) == 1
    assert html.count('<details') == 1
    assert '<summary>查看代码统计范围</summary>' in html
    assert narrative['content']['plain_summary'] not in html[html.index('<details'):html.index('</details>')]
    from app.mail_brand_asset import standalone_customer_html, CALLIGRAPHY_CID
    from html.parser import HTMLParser
    class ResourceReader(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []
        def handle_starttag(self, tag, attrs):
            self.urls.extend(value for key, value in attrs if key in ('src', 'href', 'action'))
    attachment = standalone_customer_html(html)
    reader = ResourceReader()
    reader.feed(attachment)
    assert len(reader.urls) == 1 and reader.urls[0].startswith('data:image/png;base64,')
    assert attachment.replace(reader.urls[0], 'cid:' + CALLIGRAPHY_CID) == html
    assert (report, narrative) == before


def test_customer_without_bound_manager_fact_keeps_ai_summary_prominent(monkeypatch):
    report = _report(manager_supplement="没有绑定来源的默认文字。", supplement_version_id=None,
        supplement_content_hash=None, supplement_source_type=None, supplement_provided_by=None,
        supplement_provided_at=None, supplement_provided_timezone=None)
    _install_validated(monkeypatch, report)
    html = render_approved_report_for_mail(report, profile={}, approval_snapshot={}, approved_narrative=_narrative()).html_body
    assert html.index(_narrative()["content"]["plain_summary"]) < html.index("当前功能状态")
    assert ">项目经理说明</h3>" not in html
    assert "没有绑定来源的默认文字。" not in html
