"""Real local projection/render boundaries over synthetic immutable SQLite facts."""
from copy import deepcopy

from app.approved_module_narrative import get_approved_module_narrative
from app.approved_report_narrative import load_approved_report_narrative
from app.approved_report_git_metrics import load_approved_report_git_metrics
from app.mail_brand_asset import CALLIGRAPHY_CID, standalone_customer_html, get_calligraphy_png
from app.mail_report_renderer import render_approved_report_for_mail
from test_approved_module_narrative import state, client, URL  # noqa: F401
from test_approved_report_git_metrics import metrics_state  # noqa: F401


def test_saved_confirmation_reaches_real_v11_renderer_and_exact_self_contained_attachment(metrics_state, client):
    import base64
    from html import escape
    state = metrics_state
    payload = deepcopy(state['payload'])
    for key in ('anxin_board_report_hash', 'approval_snapshot_hash'):
        payload[key] = state['report'][key]
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    confirmed = response.json()
    report, approval, profile = state['report'], state['approval_snapshot'], state['profile']
    before = deepcopy((report, approval, profile))
    database_before = state['path'].read_bytes()
    with state['connect']() as conn:
        projection = get_approved_module_narrative(1, report, approval, profile, conn=conn)
        narrative = load_approved_report_narrative(project_id=1, report=report, approval_snapshot=approval, conn=conn)
        metrics = load_approved_report_git_metrics(project_id=1, report=report, approval_snapshot=approval, conn=conn)
    assert projection == confirmed
    rendered = render_approved_report_for_mail(report, profile=profile, approval_snapshot=approval,
        approved_narrative=narrative, approved_module_narrative=projection, approved_git_metrics=metrics)
    without_notes = render_approved_report_for_mail(report, profile=profile, approval_snapshot=approval,
        approved_narrative=narrative, approved_git_metrics=metrics)
    assert rendered.schema_version == 'mail_report_render_v11'
    provider_name = 'DeepSeek' if report['provider'] == 'deepseek' else report['provider']
    assert 'AI 分析模型' in rendered.html_body
    assert escape(provider_name + ' · ' + report['actual_model']) in rendered.html_body
    assert '版本记录：' + escape(report['model_version']) in rendered.html_body
    assert metrics['metrics']['added_lines'] == 58 and metrics['metrics']['deleted_lines'] == 2
    assert metrics['metrics']['changed_file_count'] == 3
    assert all(label in rendered.html_body for label in ('本次代码新增', '本次代码删除', '本次改动文件'))
    assert rendered.render_identity.endswith(':' + confirmed['module_narrative_hash'])
    assert rendered.render_identity != without_notes.render_identity
    assert rendered.render_hash != without_notes.render_hash
    for module in confirmed['modules']:
        assert module['summary'] in rendered.html_body and module['remaining'] in rendered.html_body
        for original in module['source_requirements']:
            assert original['rationale'] not in rendered.html_body
    attachment = standalone_customer_html(rendered.html_body)
    image = 'data:image/png;base64,' + base64.b64encode(get_calligraphy_png()).decode('ascii')
    assert attachment.replace(image, 'cid:' + CALLIGRAPHY_CID) == rendered.html_body
    assert (report, approval, profile) == before
    assert state['path'].read_bytes() == database_before
