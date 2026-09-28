from copy import deepcopy
import base64

import pytest

from app.brownfield_baseline_report import BaselineReportError, render_baseline_report
from app.project_profile_v2 import ProjectProfileV2Content
from app.project_profiles import _canonicalize

SYNTHETIC_PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


def report_fixture():
    head = 'a' * 40
    content = ProjectProfileV2Content.model_validate({
        'schema_version': 'project_profile_v2',
        'planned_modules': [{'client_id': 'access', 'name': '访问管理', 'prd_refs': ['FR-SYNTHETIC'], 'requirements': ['查看项目', '修改项目']}],
        'implementation_mappings': [{'planned_module_id': 'access', 'status': 'partial', 'exact_head': head,
            'evidence_ids': ['repo-code-one'], 'paths': [{'type': 'backend', 'pattern': 'src/access.py'}],
            'rationale': 'Original module analysis'}],
    }).model_dump()
    profile = dict(id=8, project_id=3, status='confirmed', content=content,
                   content_hash=_canonicalize(ProjectProfileV2Content.model_validate(content))[1],
                   confirmed_by='示例审阅人', confirmed_at='2026-09-24T12:00:00Z')
    task = dict(task_id='baseline-example', project_id=3, profile_id=8, status='succeeded',
                created_at='2026-09-23T12:00:00Z', identity=dict(exact_head=head))
    output = dict(content=deepcopy(content), requirements={'access': [
        dict(requirement_index=0, status='implemented', evidence_ids=['repo-code-one'], rationale='Evidence exists; runtime not tested.'),
        dict(requirement_index=1, status='unknown', evidence_ids=[], rationale='Insufficient evidence.')
    ]}, coverage=dict(tracked_files=20, safe_paths=18, inspected_paths=8, unexplained_safe_paths=10,
                     omitted_neighbor_paths=0, gap_check_completed=True, complete_repository_semantic_proof=False))
    return dict(project={'id': 3, 'name': '示例项目'}, profile=profile, task=task, output=output)


def test_dynamic_readonly_report_preserves_source_and_collapses_technical_detail():
    data = report_fixture()
    before = deepcopy(data)
    html = render_baseline_report(**data, preview=False)
    assert data == before
    assert '全量代码盘点' in html and '发送前请审阅' in html
    assert '需求总数' in html and '>2</strong>' in html
    assert '<details class="analysis">' in html
    assert 'Evidence exists; runtime not tested.' in html
    assert 'Insufficient evidence.' in html
    assert 'repo-code-one' in html and 'src/access.py' in html
    assert 'a' * 40 in html
    assert '<script' not in html and '<form' not in html
    assert '正式确认版' not in html and '完成率' not in html


@pytest.mark.parametrize('mutation', [
    lambda d: d['task'].update(status='failed_after_send'),
    lambda d: d['task'].update(profile_id=9),
    lambda d: d['task'].update(project_id=9),
    lambda d: d['profile'].update(content_hash='b' * 64),
    lambda d: d['profile'].update(confirmed_by=''),
    lambda d: d['output']['requirements']['access'][0].update(requirement_index=True),
    lambda d: d['output']['requirements']['access'][1].update(evidence_ids=['repo-code-one', 'repo-code-one']),
    lambda d: d['output']['requirements']['access'].pop(),
    lambda d: d['output']['requirements']['access'][0].update(status='unknown', evidence_ids=[]),
    lambda d: d['output']['requirements']['access'][0].update(evidence_ids=['repo-code-other']),
])
def test_invalid_authority_or_aggregate_fails_closed(mutation):
    data = report_fixture()
    mutation(data)
    with pytest.raises(BaselineReportError):
        render_baseline_report(**data, preview=False)


def test_candidate_preview_only_and_html_escaping():
    data = report_fixture()
    data['profile']['status'] = 'candidate'
    data['project']['name'] = '<script>example</script>'
    html = render_baseline_report(**data, preview=True)
    assert '分析已完成 · 待你审核' in html and '&lt;script&gt;' in html
    assert '<script>' not in html
    with pytest.raises(BaselineReportError):
        render_baseline_report(**data, preview=False)


@pytest.mark.parametrize('secret', ['sk-' + 'x' * 32, 'Authorization: Bearer ' + 'x' * 32,
                                   'api_key = "' + 'x' * 32 + '"', 'https://user:pass@example.invalid'])
def test_sensitive_text_never_rendered(secret):
    data = report_fixture()
    data['project']['name'] = secret
    with pytest.raises(BaselineReportError, match='SENSITIVE'):
        render_baseline_report(**data, preview=False)


def test_brand_png_is_optional_embedded_and_invalid_bytes_fall_back():
    data = report_fixture()
    html = render_baseline_report(**data, preview=False, brand_png=SYNTHETIC_PNG)
    assert 'data:image/png;base64,' + base64.b64encode(SYNTHETIC_PNG).decode() in html
    assert 'alt="非己所安，不加于物"' in html
    for invalid in [b'<svg onload="bad">', b'\x89PNG\r\n\x1a\n', SYNTHETIC_PNG + b'x' * 65536]:
        html = render_baseline_report(**data, preview=False, brand_png=invalid)
        assert 'data:image/png' not in html
        assert '非己所安，不加于物' in html


def test_unknown_background_references_are_not_implementation_proof():
    data = report_fixture()
    data['output']['requirements']['access'][1]['evidence_ids'] = ['repo-code-background']
    before = deepcopy(data)
    html = render_baseline_report(**data, preview=False)
    assert data == before
    assert '已检查的相关代码（仍待核实）' in html
    assert '不代表已经实现' in html
    assert 'repo-code-background' in html and '待核实' in html
    assert data['profile']['content']['implementation_mappings'][0]['evidence_ids'] == ['repo-code-one']
    for content in (data['profile']['content'], data['output']['content']):
        content['implementation_mappings'][0]['evidence_ids'].append('repo-code-background')
    data['profile']['content_hash'] = _canonicalize(ProjectProfileV2Content.model_validate(data['profile']['content']))[1]
    with pytest.raises(BaselineReportError):
        render_baseline_report(**data, preview=False)


@pytest.mark.parametrize('refs', [['repo-code-background'] * 2, [f'repo-code-{i}' for i in range(101)]])
def test_unknown_background_references_reject_duplicates_and_overflow(refs):
    data = report_fixture()
    data['output']['requirements']['access'][1]['evidence_ids'] = refs
    with pytest.raises(BaselineReportError):
        render_baseline_report(**data, preview=False)


def test_candidate_uses_frozen_calligraphy_layout_with_honest_analysis_statistics():
    data = report_fixture()
    data['profile']['status'] = 'candidate'
    html = render_baseline_report(**data, preview=True, brand_png=SYNTHETIC_PNG)
    for name in ('board-shell', 'board-hero', 'hero-title-lock', 'today-grid', 'board-card', 'table-wrap', 'manager-box'):
        assert f'class="{name}"' in html
    assert '<h1>安心看板</h1>' in html
    assert '<th>功能</th><th>当前核查结果</th><th>需求与代码依据</th>' in html
    assert '分析已完成 · 待你审核' in html and '尚未提供项目经理补充' in html
    assert '本次需求核查统计' in html and '不是 Git 增量指标' in html
    assert '70</strong>' not in html and '>2</strong>' in html
    assert '今天实际发生了什么' not in html
    assert '正式报告确认：尚未进行' in html
