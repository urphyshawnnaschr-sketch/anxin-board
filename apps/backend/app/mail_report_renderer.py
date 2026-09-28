"""Deterministic, side-effect-free HTML rendering for one formal Anxin Board V3 report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
import hashlib
import json
import re

from app.anxin_board_report_v3 import AnxinBoardReportV3Error, validate_anxin_board_report_v3
from app.mail_brand_asset import CALLIGRAPHY_CID, validate_inline_assets
from app.approved_report_narrative import (
    ApprovedReportNarrativeError,
    validate_approved_report_narrative,
)
from app.approved_module_narrative import validate_approved_module_narrative
from app.approved_report_git_metrics import validate_approved_report_git_metrics
from app.client_stage_summary import build_client_stage_summary


SCHEMA_VERSION = "mail_report_render_v11"
_SUPPORTED_SCHEMA_VERSIONS = frozenset({"mail_report_render_v1", "mail_report_render_v2", "mail_report_render_v3", "mail_report_render_v4", "mail_report_render_v5", "mail_report_render_v6", "mail_report_render_v7", "mail_report_render_v8", "mail_report_render_v9", "mail_report_render_v10", SCHEMA_VERSION})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SUBJECT_CHARS = 998


class MailReportRenderError(RuntimeError):
    code = "MAIL_REPORT_RENDER_INVALID"


@dataclass(frozen=True, slots=True)
class MailReportRender:
    schema_version: str
    report_hash: str
    render_identity: str
    render_hash: str
    html_body: str
    html_sha256: str
    subject: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: object) -> str:
    if type(value) is not str:
        raise MailReportRenderError()
    return escape(value, quote=True).replace("\n", "<br>")


def _subject_text(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_SUBJECT_CHARS or value != value.strip():
        raise MailReportRenderError()
    for char in value:
        point = ord(char)
        if point < 32 or 127 <= point <= 159 or point in (0x2028, 0x2029):
            raise MailReportRenderError()
    return value


def _expected_render_hash(
    *,
    schema_version: str,
    report_hash: str,
    render_identity: str,
    html_sha256: str,
    subject: str,
) -> str:
    return _sha256_text(
        _canonical_json(
            {
                "schema_version": schema_version,
                "report_hash": report_hash,
                "render_identity": render_identity,
                "html_sha256": html_sha256,
                "subject": subject,
            }
        )
    )


def validate_mail_report_render(value: object) -> MailReportRender:
    """Re-close current or already-frozen historical render identities."""
    if not isinstance(value, MailReportRender) or value.schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise MailReportRenderError()
    if _HASH_RE.fullmatch(value.report_hash) is None or _HASH_RE.fullmatch(value.html_sha256) is None:
        raise MailReportRenderError()
    if _HASH_RE.fullmatch(value.render_hash) is None:
        raise MailReportRenderError()
    expected_identity = f"{value.schema_version}:{value.report_hash}"
    if value.schema_version in {"mail_report_render_v9", "mail_report_render_v10", SCHEMA_VERSION}:
        match = re.fullmatch(re.escape(expected_identity) + r':([0-9a-f]{64}|none)', value.render_identity)
        if match is None or type(value.html_body) is not str:
            raise MailReportRenderError()
        markers = re.findall(r'<meta name="anxin-module-narrative-hash" content="([0-9a-f]{64}|none)">', value.html_body)
        if markers != [match.group(1)]:
            raise MailReportRenderError()
        expected_identity += ':' + match.group(1)
        if value.schema_version in {"mail_report_render_v10", SCHEMA_VERSION} and len(re.findall(
                r'<meta name="anxin-git-metrics-hash" content="[0-9a-f]{64}">', value.html_body)) != 1:
            raise MailReportRenderError()
    if value.render_identity != expected_identity:
        raise MailReportRenderError()
    if type(value.html_body) is not str or _sha256_text(value.html_body) != value.html_sha256:
        raise MailReportRenderError()
    subject = _subject_text(value.subject)
    expected_hash = _expected_render_hash(
        schema_version=value.schema_version,
        report_hash=value.report_hash,
        render_identity=value.render_identity,
        html_sha256=value.html_sha256,
        subject=subject,
    )
    if value.render_hash != expected_hash:
        raise MailReportRenderError()
    if value.schema_version in {"mail_report_render_v4", "mail_report_render_v5", "mail_report_render_v6", "mail_report_render_v7", "mail_report_render_v8", "mail_report_render_v9", "mail_report_render_v10", SCHEMA_VERSION}:
        try:
            if not validate_inline_assets(value.html_body):
                raise MailReportRenderError()
        except ValueError as exc:
            raise MailReportRenderError() from exc
    return value


def _stage_style(stage: object) -> tuple[str, str]:
    if type(stage) is not str:
        raise MailReportRenderError()
    styles = {
        "已完成": ("#eaf7ef", "#137a43"),
        "开发中": ("#eaf1ff", "#2759a5"),
        "等待联调": ("#fff6df", "#8b6200"),
        "等待测试": ("#fff6df", "#8b6200"),
        "测试中": ("#f2ecff", "#6941c6"),
        "暂时无法确认": ("#f1f3f5", "#667085"),
    }
    return styles.get(stage, ("#f1f3f5", "#667085"))


def _module_html(module: object, note: Mapping[str, object] | None = None) -> str:
    if not isinstance(module, Mapping):
        raise MailReportRenderError()
    stage = module.get("display_stage")
    bg, fg = _stage_style(stage)
    summary = module.get("summary")
    generic = summary in {
        build_client_stage_summary(module_name='模块', stage=stage)['summary'],
        '当前约定范围已完成。', '这项功能已完成当前约定范围内的开发。',
    }
    description = (
        '<div style="font-size:11px;color:#68758a">已有依据</div>'
        f'<div>{_text(note["summary"])}</div>'
        '<div style="margin-top:9px;font-size:11px;color:#68758a">待完善/待核实</div>'
        f'<div>{_text(note["remaining"])}</div>'
    ) if note is not None else ('—' if generic else (
        f'{_text(summary)}<div style="margin-top:4px;color:#7a8799;font-size:11px">'
        f'建议下一步：{_text(module.get("next_step"))}</div>'
    ))
    return (
        '<tr>'
        '<td style="padding:15px 12px;border-bottom:1px solid #e7edf4;vertical-align:top;'
        'font-weight:700;color:#102a43;line-height:1.45">'
        f'{_text(module.get("name"))}</td>'
        '<td style="padding:15px 12px;border-bottom:1px solid #e7edf4;vertical-align:top;white-space:nowrap">'
        f'<span style="display:inline-block;padding:5px 9px;border-radius:999px;background:{bg};color:{fg};'
        f'font-size:12px;font-weight:700;line-height:1.2">{_text(stage)}</span></td>'
        '<td style="padding:15px 12px;border-bottom:1px solid #e7edf4;vertical-align:top;'
        'color:#425466;line-height:1.65">'
        f'{description}</td>'
        '</tr>'
    )


def _section(number: str, title: str, body: str) -> str:
    # Tables/inline styles keep the approved card layout when mail clients strip CSS.
    return (
        '<tr><td style="padding-top:18px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#fff;border:1px solid #e2e8f0;border-radius:18px"><tr>'
        '<td style="padding:23px 24px">'
        '<h2 style="margin:0 0 15px;font-size:20px;line-height:1.5;color:#1f2a3d">'
        '<span style="display:inline-block;padding:4px 8px;margin-right:10px;border-radius:9px;'
        f'background:#4f6ef7;color:#fff;font-size:12px">{number}</span>{title}</h2>'
        f'{body}</td></tr></table></td></tr>'
    )


def _provenance_text(report: Mapping[str, object], key: str) -> str:
    value = report.get(key)
    if value is None or value == '':
        return '暂未提供'
    if type(value) not in (str, int):
        raise MailReportRenderError()
    return _text(str(value))


_SOURCE_LABELS = {
    'git_fact': '代码记录', 'prd_fact': '需求说明', 'ai_analysis': '分析判断',
    'pm_external_fact': '项目经理提供', 'fixed_disclaimer': '分析范围说明',
}
_SCOPE_LABELS = {
    '前端': '页面与交互', '后端': '业务处理', '数据库': '数据存储', '接口': '系统连接',
    '测试': '检查与验证', '配置': '运行设置', '跨模块': '多个功能', '暂时无法确认': '范围待核实',
}


def _narrative_html(narrative: Mapping[str, object]) -> str:
    content = narrative['content']
    parts = [
        '<div style="padding:18px 20px;border-left:4px solid #4f6ef7;background:#f4f7ff;line-height:1.8">'
        '<div style="font-size:11px;color:#68758a;margin-bottom:7px">AI 原文 · 生成时的分析记录</div>'
        f'<p style="margin:0">{_text(content["plain_summary"])}</p></div>',
        '<p style="font-size:12px;color:#68758a">以下保留生成时的完整分析原文；项目经理说明与 AI 原文分别展示。</p>',
    ]
    sections = (
        ('code_change_summary', '本次具体工作'),
        ('test_evidence', '检查与验证情况'),
        ('risks', '分析判断与协助事项'),
        ('unknown_items', '仍待核实的事项'),
        ('source_warnings', '本次分析的范围与限制'),
    )
    for key, heading in sections:
        items = content[key]
        if not items:
            continue
        parts.append(f'<h3 style="margin:20px 0 9px;color:#1f2a3d;font-size:15px">{heading}</h3>')
        for item in items:
            labels = [_SOURCE_LABELS[item['source_type']]]
            if 'implementation_scope' in item:
                labels.append(_SCOPE_LABELS[item['implementation_scope']])
            if key == 'risks':
                labels.insert(0, '需甲方协助' if item['risk_level'] == 'needs_client_action' else '尚待验证的分析判断')
            parts.append(
                '<div style="margin:0 0 10px;padding:12px 14px;border:1px solid #e7edf4;border-radius:10px">'
                f'<p style="margin:0;line-height:1.8">{_text(item["content"])}</p>'
                f'<div style="margin-top:6px;color:#68758a;font-size:11px">{_text(" · ".join(labels))}</div></div>'
            )
    return ''.join(parts)


def _unassociated_features_html(report: Mapping[str, object], narrative: Mapping[str, object]) -> str:
    # Narrative has already passed full source-result hash and contract validation.
    source = narrative.get('source_result', {})
    if narrative.get('source_task_type') == 'daily_report_regenerate':
        source = source.get('new_report', {})
    rows = source.get('feature_progress', [])
    names = [module['name'] for module in report['modules']]
    unassociated = [row for row in rows if names.count(row['feature']) != 1
                    or sum(other['feature'] == row['feature'] for other in rows) != 1]
    if not unassociated:
        return ''
    parts = ['<h3 style="font-size:16px;margin:22px 0 8px">尚未关联到已确认功能模块</h3>',
             '<p style="font-size:12px;color:#68758a">以下保留本报告的原始功能判断。名称尚不能唯一对应到上表模块，因此不改变上表状态；仍需核对关联关系。</p>']
    for row in unassociated:
        parts.append('<div style="padding:14px 0;border-bottom:1px solid #e2e8f0;overflow-wrap:anywhere">'
                     f'<strong>{_text(row["feature"])}</strong><p>原报告判断：{_text(row["stage"])}</p>'
                     f'<p style="font-size:12px;color:#68758a">{_text(_SOURCE_LABELS[row["source_type"]])} · '
                     f'{_text(_SCOPE_LABELS[row["implementation_scope"]])}</p>'
                     '</div>')
    return ''.join(parts)


def _render_html(report: Mapping[str, object], narrative: Mapping[str, object], module_narrative, git_metrics) -> str:
    modules = report.get('modules')
    daily = report.get('daily_change')
    if type(modules) is not list or not isinstance(daily, Mapping):
        raise MailReportRenderError()
    # Counts belong to the exact approved Git snapshot, never prose or module totals.
    metrics = ''.join(
        '<td width="33%" style="padding:5px;vertical-align:top">'
        '<div style="padding:13px 14px;border:1px solid #73809f;border-radius:12px;background:#354770">'
        f'<div style="color:#c2cce0;font-size:11px">{label}</div>'
        f'<div style="font-size:23px;font-weight:700;color:#fff;margin-top:4px">{git_metrics["metrics"][key]}</div>'
        f'<div style="font-size:10px;color:#c2cce0">{unit}</div></div></td>'
        for label, key, unit in (('本次代码新增', 'added_lines', '行'), ('本次代码删除', 'deleted_lines', '行'),
                                ('本次改动文件', 'changed_file_count', '个'))
    )
    notes = {item['module_id']: item for item in module_narrative['modules']} if module_narrative else {}
    module_rows = ''.join(_module_html(item, notes.get(item.get('module_id'))) for item in modules)
    features = (
        '<table width="100%" cellspacing="0" cellpadding="0" border="0" '
        'style="border-collapse:collapse;font-size:13px;border:1px solid #e2e8f0">'
        '<thead><tr bgcolor="#4f6ef7" style="background:#4f6ef7;color:#fff;text-align:left">'
        '<th style="padding:12px 14px">功能</th><th style="padding:12px 14px">现在到哪了</th>'
        '<th style="padding:12px 14px">具体进展与待完善事项</th></tr></thead>'
        f'<tbody>{module_rows}</tbody></table>'
    )
    features += _unassociated_features_html(report, narrative)
    features = '<p style="color:#68758a;font-size:12px">这是当前功能状态，不代表这些功能都是本次完成。</p>' + features
    if module_narrative:
        features = (
            '<p style="color:#68758a;font-size:12px">模块说明依据已保存的历史代码盘点；代码证据不等于实际运行或验收。'
            '本次变化见项目经理说明。原始需求结论与引用可在产品中展开查看。</p>'
            '<p style="color:#68758a;font-size:12px">'
            f'{_text(module_narrative["attribution"])} · 独立确认：{_text(module_narrative["confirmed_by"])} · '
            f'{_text(module_narrative["confirmed_at"])}；与本报告原审批分开记录。</p>'
        ) + features
    else:
        features = '<p style="color:#68758a;font-size:12px">本版未另行确认具体模块说明；仅有阶段模板的条目不补写能力或缺项。</p>' + features
    has_manager = type(report.get('supplement_version_id')) is int and report['supplement_version_id'] > 0
    manager = (
        '<h3 style="margin:0 0 10px;color:#1f2a3d;font-size:16px">项目经理说明</h3>'
        '<div style="padding:17px 19px;border:1px solid #f1d8a9;border-left:4px solid #bd7414;'
        'border-radius:0 13px 13px 0;background:#fffdf7;color:#4a3a20;line-height:1.7">'
        f'<p style="margin:0">{_text(report.get("manager_supplement"))}</p>'
        '<div style="margin-top:12px;padding-top:10px;border-top:1px solid #f0dfbf;font-size:11px;color:#806b48">'
        f'补充来源：{_text(_SOURCE_LABELS.get(report.get("supplement_source_type"), "来源待核实"))}<br>'
        f'提供人：{_provenance_text(report, "supplement_provided_by")}<br>'
        f'提供时间：{_provenance_text(report, "supplement_provided_at")} · '
        f'{_provenance_text(report, "supplement_provided_timezone")}</div></div>'
    ) if has_manager else ''
    conclusion = manager or (
        '<div style="padding:18px 20px;border-left:4px solid #4f6ef7;background:#f4f7ff;line-height:1.8">'
        '<div style="font-size:12px;color:#68758a;margin-bottom:7px">AI 摘要（原文）</div>'
        f'<p style="margin:0">{_text(narrative["content"]["plain_summary"])}</p></div>'
    )
    for item in narrative['content']['risks']:
        if item['risk_level'] == 'needs_client_action':
            conclusion += (
                '<div style="margin-top:16px;padding:16px 18px;border:1px solid #edbe73;background:#fff6df;color:#62420f">'
                '<strong>需甲方协助</strong>'
                f'<p style="margin:8px 0;line-height:1.8">{_text(item["content"])}</p>'
                f'<div style="font-size:11px">{_text(_SOURCE_LABELS[item["source_type"]])} · AI 原文条目</div></div>'
            )
    suspected_count = sum(item['risk_level'] == 'suspected' for item in narrative['content']['risks'])
    if suspected_count:
        conclusion += f'<p style="font-size:12px;color:#68758a">AI 另列出 {suspected_count} 项尚待验证的分析判断，详见下方原文与依据。</p>'
    info = [('项目名称', 'project_name'), ('报告日期', 'report_date'),
            ('确认人', 'confirmed_by'), ('确认时间', 'confirmed_at')]
    cells = [
        '<td width="33%" style="vertical-align:top;padding:5px">'
        '<div style="padding:12px 13px;border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc">'
        f'<div style="margin-bottom:4px;color:#68758a;font-size:11px">{label}</div>'
        '<div style="color:#1f2a3d;font-size:13px;font-weight:700;overflow-wrap:anywhere">'
        f'{_provenance_text(report, key)}</div></div></td>' for label, key in info
    ]
    model_fields = [report.get(key) for key in ('provider', 'actual_model', 'model_version')]
    model_information = '本版未记录'
    if all(type(value) is str and value.strip() for value in model_fields):
        provider, actual_model, model_version = model_fields
        provider_name = 'DeepSeek' if provider == 'deepseek' else provider
        model_information = (
            _text(f'{provider_name} · {actual_model}')
            + '<div style="margin-top:4px;color:#68758a;font-size:11px;font-weight:400">'
            + f'版本记录：{_text(model_version)}</div>'
        )
    cells.append(
        '<td width="33%" style="vertical-align:top;padding:5px">'
        '<div style="padding:12px 13px;border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc">'
        '<div style="margin-bottom:4px;color:#68758a;font-size:11px">AI 分析模型</div>'
        '<div style="color:#1f2a3d;font-size:13px;font-weight:700;overflow-wrap:anywhere">'
        f'{model_information}</div></div></td>'
    )
    information = '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">'
    for index in range(0, len(cells), 3):
        group = cells[index:index + 3]
        information += '<tr>' + ''.join(group) + '<td></td>' * (3 - len(group)) + '</tr>'
    information += '</table><p style="font-size:11px;color:#68758a;text-align:center">报告已人工确认；功能开发进度不等于实际运行或验收结果。</p>'
    source = git_metrics['source_git_facts']
    information += ('<details style="font-size:11px;color:#68758a;overflow-wrap:anywhere"><summary>查看代码统计范围</summary>'
                    f'<p>分支：{_text(source["branch"])}<br>起点：{_text(source["from_commit"])}<br>'
                    f'终点：{_text(source["to_commit"])}</p></details>')
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light"><meta name="anxin-mail-render" content="v11">'
        f'<meta name="anxin-git-metrics-hash" content="{git_metrics["metrics_hash"]}">'
        f'<meta name="anxin-module-narrative-hash" content="{module_narrative["module_narrative_hash"] if module_narrative else "none"}">'
        f'<meta name="anxin-narrative-hash" content="{_text(narrative["narrative_hash"])}"></head>'
        '<body style="margin:0;padding:0;background:#f6f8fc;color:#3b485e;font-size:14px;line-height:1.68;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei','PingFang SC',Arial,sans-serif\">"
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#f6f8fc">'
        '<tr><td align="center" style="padding:26px 14px 40px">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:1060px">'
        '<tr><td bgcolor="#1c2738" style="padding:34px 38px 26px;border-radius:22px;color:#fff;'
        'background:#102a56;background:linear-gradient(135deg,#1c2738,#2a3857 52%,#4f6ef7)">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>'
        '<td style="vertical-align:top">'
        '<span style="display:inline-block;padding:5px 12px;border:1px solid #718096;border-radius:999px;font-size:12px">研发进展</span>'
        '<h1 style="margin:14px 0 0;color:#fff;font-size:44px;line-height:1.12;letter-spacing:.09em;white-space:nowrap">安心看板</h1>'
        f'<img src="cid:{CALLIGRAPHY_CID}" width="192" alt="{_text(report.get("motto"))}" '
        'style="display:block;width:192px;max-width:192px;height:auto;margin-top:8px;border:0;color:#fff">'
        '</td><td align="right" style="vertical-align:top;padding-left:24px;color:#d8e0ef;font-size:13px">'
        f'<strong style="display:block;color:#fff;font-size:20px;margin-bottom:6px">{_text(report.get("project_name"))}</strong>'
        f'报告日期：{_text(report.get("report_date"))}<br>报告状态：正式确认版<br>'
        f'截至时间：{_text(report.get("confirmed_at"))}</td></tr></table>'
        '<div style="margin-top:25px;color:#d8e0ef;font-size:12px;font-weight:700">本次代码变化</div>'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:4px"><tr>'
        f'{metrics}</tr></table>'
        '<div style="margin-top:9px;color:#c2cce0;font-size:10px">统计基于本报告绑定的代码版本范围，仅反映变化量。</div>'
        '</td></tr>'
        + _section('01', '本次工作与进展', conclusion)
        + _section('02', '当前功能状态', features)
        + _section('03', 'AI 分析原文与依据', _narrative_html(narrative)
                   + f'<p style="font-size:11px;color:#68758a">{_text(daily.get("scope_note", ""))}</p>')
        + _section('04', '报告信息', information)
        + '<tr><td style="padding-top:16px;color:#68758a;text-align:center;font-size:11px">'
        f'安心看板 · {_text(report.get("report_date"))}<br>由安心看板生成</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def render_approved_report_for_mail(
    report: object,
    *,
    profile: Mapping[str, object],
    approval_snapshot: Mapping[str, object],
    approved_narrative: Mapping[str, object] | None = None,
    approved_module_narrative: Mapping[str, object] | None = None,
    approved_git_metrics: Mapping[str, object] | None = None,
) -> MailReportRender:
    """Validate exact V3 authority first, then render deterministic email HTML."""
    try:
        validated = validate_anxin_board_report_v3(
            report,
            profile=profile,
            approval_snapshot=approval_snapshot,
        )
        narrative = validate_approved_report_narrative(
            approved_narrative, report=validated, approval_snapshot=approval_snapshot,
        )
        module_narrative = (validate_approved_module_narrative(
            approved_module_narrative, report=validated, approval_snapshot=approval_snapshot, profile=profile,
        ) if approved_module_narrative is not None else None)
        git_metrics = validate_approved_report_git_metrics(
            approved_git_metrics, report=validated, approval_snapshot=approval_snapshot,
        )
    except (ApprovedReportNarrativeError, AnxinBoardReportV3Error, TypeError, ValueError) as exc:
        raise MailReportRenderError() from exc

    try:
        report_hash = validated["anxin_board_report_hash"]
        project_name = validated["project_name"]
        report_date = validated["report_date"]
        if type(report_hash) is not str or _HASH_RE.fullmatch(report_hash) is None:
            raise MailReportRenderError()
        if type(project_name) is not str or type(report_date) is not str:
            raise MailReportRenderError()
        subject_project = " ".join(project_name.split())
        subject_date = " ".join(report_date.split())
        if not subject_project or not subject_date:
            raise MailReportRenderError()
        html_body = _render_html(validated, narrative, module_narrative, git_metrics)
    except (KeyError, TypeError, ValueError) as exc:
        raise MailReportRenderError() from exc

    html_sha256 = _sha256_text(html_body)
    subject = _subject_text(f"{subject_project}｜{subject_date} 安心看板")
    render_identity = f'{SCHEMA_VERSION}:{report_hash}:{module_narrative["module_narrative_hash"] if module_narrative else "none"}'
    rendered = MailReportRender(
        schema_version=SCHEMA_VERSION,
        report_hash=report_hash,
        render_identity=render_identity,
        render_hash=_expected_render_hash(
            schema_version=SCHEMA_VERSION,
            report_hash=report_hash,
            render_identity=render_identity,
            html_sha256=html_sha256,
            subject=subject,
        ),
        html_body=html_body,
        html_sha256=html_sha256,
        subject=subject,
    )
    return validate_mail_report_render(rendered)
