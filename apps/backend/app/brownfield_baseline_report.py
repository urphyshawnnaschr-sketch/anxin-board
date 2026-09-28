"""Pure HTML for saved Atlas evidence; never a formal daily report or approval."""
from collections import Counter
import base64
from html import escape
import re

from app.context_redaction import redact_credential_safe_structured_value
from app.project_profile_v2 import ProjectProfileV2Content
from app.project_profiles import _canonicalize, _path_pattern_error


class BaselineReportError(ValueError):
    pass


def _require(condition, code='BROWNFIELD_REPORT_IDENTITY_INVALID'):
    if not condition:
        raise BaselineReportError(code)


LABELS = {'implemented': '已有实现证据', 'partial': '部分实现', 'unknown': '待核实'}
SUMMARIES = {
    'implemented': '已找到支持此需求的代码证据；代码证据不等于运行验收。',
    'partial': '已找到部分实现证据，仍有范围或细节需要补充核对。',
    'unknown': '当前证据不足，暂时无法确认；不据此认定为未实现。',
}
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
# The same navy/light visual language and Chinese sans typography as the product board.
CSS = """
.board-shell{--primary:#4f6ef7;--primary-dark:#3852d0;--primary-soft:#eef2ff;--success:#16866f;--success-soft:#eaf9f4;--warning:#bd7414;--muted:#68758a;--ink:#1f2a3d;--text:#3b485e;--line:#e2e8f0;--card:#fff;--soft:#f8fafc;--shadow:0 10px 30px rgba(15,23,42,.075);width:100%;max-width:1060px;margin:0 auto;color:var(--text);line-height:1.68}.board-hero{padding:34px 38px 26px;border-radius:22px;color:#fff;overflow:hidden;background:radial-gradient(circle at 84% 16%,rgba(0,212,170,.18),transparent 28%),radial-gradient(circle at 11% 94%,rgba(79,110,247,.16),transparent 31%),linear-gradient(135deg,#1c2738,#2a3857 52%,#4f6ef7);box-shadow:0 18px 44px rgba(30,42,58,.20)}.hero-main{display:flex;justify-content:space-between;gap:28px;align-items:flex-start}.kicker{display:inline-block;padding:5px 12px;border:1px solid rgba(255,255,255,.18);border-radius:999px;background:rgba(255,255,255,.1);font-size:12px}.hero-title-lock{display:flex;flex-direction:column;align-items:flex-start;width:192px;max-width:192px;min-width:192px}.hero-title-lock h1{display:block;width:192px;margin:14px 0 0;color:#fff;font-size:44px;line-height:1.12;letter-spacing:.09em;white-space:nowrap}.calligraphy-motto{display:block;width:192px;max-width:192px;height:auto;margin-top:8px;object-fit:contain;filter:invert(1);user-select:none;-webkit-user-drag:none}.hero-report{min-width:270px;text-align:right;color:rgba(255,255,255,.76);font-size:13px}.hero-report strong{display:block;margin-bottom:6px;color:#fff;font-size:20px}.today-title{margin-top:25px;color:rgba(255,255,255,.7);font-size:12px;font-weight:700;letter-spacing:.08em}.today-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:9px}.today-item{padding:13px 14px;border:1px solid rgba(255,255,255,.13);border-radius:12px;background:rgba(255,255,255,.08)}.today-label{color:rgba(255,255,255,.62);font-size:11px}.today-value{margin-top:4px;color:#fff;font-size:15px;font-weight:750}.today-value strong{font-size:23px;margin-right:4px}.today-unavailable{margin-top:2px;color:rgba(255,255,255,.56);font-size:10px}.today-note{margin-top:9px;color:rgba(255,255,255,.66);font-size:10px}.today-note-secondary{color:rgba(255,255,255,.52)}.board-card{margin-top:18px;padding:23px 24px;border:1px solid var(--line);border-radius:18px;background:var(--card);box-shadow:var(--shadow)}.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:15px}.section-title{display:flex;align-items:center;gap:10px;margin:0;color:var(--ink);font-size:20px;font-weight:850}.num{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,var(--primary),#6b85fa);color:#fff;font-size:12px}.helper{color:var(--muted);font-size:11px}.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:13px}table{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}thead th{padding:12px 14px;background:var(--primary);color:#fff;text-align:left;font-size:12px}tbody td{padding:13px 14px;border-bottom:1px solid var(--line);vertical-align:middle}tbody tr:nth-child(even){background:#fafbfd}tbody tr:last-child td{border-bottom:none}.module-name{color:var(--ink);font-weight:750}.next-step{margin-top:4px;color:#7a8799;font-size:11px}.status{display:inline-flex;padding:4px 9px;border-radius:999px;font-size:11px;font-weight:800;white-space:nowrap}.status.done{background:var(--success-soft);color:#166f56}.status.dev{background:var(--primary-soft);color:var(--primary-dark)}.status.waiting{background:#f1efff;color:#6653bd}.status.test{background:#fff5e6;color:#bd7414}.status.unknown{background:#f1f5f9;color:#64748b}.summary-box{padding:18px 20px;border:1px solid #d9e0f2;border-left:4px solid var(--primary);border-radius:0 13px 13px 0;background:linear-gradient(180deg,#f8faff,#f4f7ff)}.summary-box p{margin:0 0 9px}.summary-box p:last-child{margin:0}.manager-box{padding:17px 19px;border:1px solid #f1d8a9;border-left:4px solid var(--warning);border-radius:0 13px 13px 0;background:#fffdf7;color:#4a3a20}.manager-box p{margin:0}.manager-provenance{margin-top:12px;padding-top:10px;border-top:1px solid #f0dfbf;color:#806b48;font-size:11px}.basis-group{margin-top:18px}.basis-group:first-of-type{margin-top:0}.basis-group-title{margin:0 0 10px;color:var(--ink);font-size:13px;font-weight:800}.basis{display:grid;grid-template-columns:repeat(3,1fr);gap:11px}.basis-item{padding:12px 13px;border:1px solid var(--line);border-radius:10px;background:var(--soft)}.basis-wide{grid-column:1/-1}.basis-label{margin-bottom:4px;color:var(--muted);font-size:11px}.basis-value{color:var(--ink);font-size:13px;font-weight:700;word-break:break-word}.responsibility-box{padding:14px 15px;border:1px dashed #cfd7e6;border-radius:10px;background:var(--soft);color:#58677c;font-size:12px}.scope-note{margin-top:13px;color:#7a8698;text-align:center;font-size:10px}.board-footer{padding-top:16px;color:var(--muted);text-align:center;font-size:11px}@media(max-width:760px){.anxin-board-page{width:min(100% - 16px,1120px);margin-top:8px}.preview-toolbar,.history-toolbar{align-items:flex-start;flex-direction:column}.history-pagination{justify-content:flex-start}.board-hero{padding:28px 20px 22px;border-radius:16px}.hero-main{flex-direction:column}.hero-report{text-align:left}.today-grid,.basis{grid-template-columns:1fr 1fr}.basis-wide{grid-column:1/-1}.board-card{padding:18px;border-radius:14px}}@media(max-width:520px){.today-grid,.basis{grid-template-columns:1fr}.basis-wide{grid-column:auto}.hero-title-lock{width:148px;max-width:148px;min-width:148px}.hero-title-lock h1{width:148px;font-size:34px}.calligraphy-motto{width:148px;max-width:148px;height:auto}}
/* Offline baseline presentation: frozen board geometry above; disclosure content below. */
*{box-sizing:border-box}body{margin:0;padding:28px 16px;background:#edf3f8;font:14px/1.68 system-ui,-apple-system,"Segoe UI","Microsoft YaHei UI",sans-serif}.notice{margin:18px 0;padding:12px 16px;border:1px solid #f1d8a9;border-radius:10px;background:#fffdf7}.today-value strong{font:inherit}.requirement{margin:12px 0;padding:12px;border:1px solid var(--line);border-radius:10px;background:#fff}.heading{display:flex;justify-content:space-between;gap:12px}.badge{white-space:nowrap}.implemented{color:#166f56}.partial{color:#3852d0}.unknown{color:#64748b}details{margin:12px 0}summary{cursor:pointer;color:var(--primary-dark)}code,.original,.identity{overflow-wrap:anywhere;word-break:break-word}.refs code{display:block;margin:6px 0}code{font-size:12px}.muted{color:var(--muted);font-size:12px}.module-name{width:20%}.module-status{width:18%}.requirement p{margin:8px 0}.motto{font-size:18px}.hero-report strong{display:block}.board-shell table{table-layout:fixed}.board-shell td{overflow-wrap:anywhere}@media(max-width:760px){body{padding:12px 8px}.board-shell table{min-width:0}.module-name{width:22%}.module-status{width:25%}.heading{flex-direction:column}.board-shell tbody td{padding:10px 8px}.today-value{font-size:28px}}
"""


def _validate(project, profile, task, output, preview):
    _require(type(project.get('id')) is int and project['id'] > 0)
    _require(type(profile.get('id')) is int and profile['id'] > 0)
    _require(type(profile.get('project_id')) is int and type(task.get('project_id')) is int
             and project['id'] == profile['project_id'] == task['project_id'])
    _require(type(task.get('profile_id')) is int and task['profile_id'] == profile['id']
             and task.get('status') == 'succeeded')
    _require(profile.get('status') in {'candidate', 'confirmed', 'superseded'})
    if not preview:
        _require(profile['status'] == 'confirmed', 'BROWNFIELD_REPORT_CONFIRMATION_REQUIRED')
    if profile['status'] in {'confirmed', 'superseded'}:
        _require(all(isinstance(profile.get(k), str) and profile[k].strip()
                     for k in ('confirmed_at', 'confirmed_by')), 'BROWNFIELD_REPORT_CONFIRMATION_REQUIRED')
    head = task['identity']['exact_head']
    _require(isinstance(head, str) and re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', head))
    content = ProjectProfileV2Content.model_validate(profile['content'])
    generated = ProjectProfileV2Content.model_validate(output['content'])
    _require(_canonicalize(content)[1] == profile['content_hash'] == _canonicalize(generated)[1])
    data = content.model_dump()
    modules, mappings, requirements = data['planned_modules'], data['implementation_mappings'], output['requirements']
    _require(modules and isinstance(requirements, dict))
    mapped = {m['planned_module_id']: m for m in mappings}
    _require(set(mapped) == set(requirements) == {m['client_id'] for m in modules})
    counts = Counter({status: 0 for status in LABELS})
    for module in modules:
        rows = requirements[module['client_id']]
        mapping = mapped[module['client_id']]
        _require(isinstance(rows, list) and len(rows) == len(module['requirements']))
        indexes = [row['requirement_index'] for row in rows]
        _require(all(type(i) is int for i in indexes) and sorted(indexes) == list(range(len(rows))))
        refs = set()
        local = Counter()
        for row in rows:
            _require(row['status'] in LABELS and isinstance(row['rationale'], str) and 0 < len(row['rationale']) <= 1200)
            evidence = row['evidence_ids']
            _require(isinstance(evidence, list) and all(isinstance(ref, str) and re.fullmatch(r'repo-code-[A-Za-z0-9_-]{1,128}', ref) for ref in evidence))
            _require(len(evidence) <= 100 and len(evidence) == len(set(evidence)) and (row['status'] == 'unknown' or bool(evidence)))
            if row['status'] != 'unknown':
                refs.update(evidence)
            local[row['status']] += 1
        expected = 'implemented' if rows and local['implemented'] == len(rows) else 'partial' if local['implemented'] + local['partial'] else 'unknown'
        _require(mapping['status'] == expected and mapping['exact_head'] == head
                 and len(mapping['evidence_ids']) == len(set(mapping['evidence_ids']))
                 and set(mapping['evidence_ids']) == refs)
        counts.update(local)
    for item in [*mappings, *data['unplanned_code_features']]:
        _require(item['exact_head'] == head)
        for path in item['paths']:
            _require(_path_pattern_error(path['pattern']) is None)
    coverage = output['coverage']
    _require(isinstance(coverage, dict))
    for key in ('tracked_files', 'safe_paths', 'inspected_paths', 'unexplained_safe_paths', 'omitted_neighbor_paths'):
        _require(coverage.get(key) is None or type(coverage[key]) is int and coverage[key] >= 0)
    # Inspect exactly the values that could appear in the document; never alter AI originals.
    displayed = dict(project_name=project['name'], content=data, requirements=requirements,
                     task_id=task['task_id'], created_at=task['created_at'],
                     confirmed_by=profile.get('confirmed_by'), confirmed_at=profile.get('confirmed_at'))
    safe = redact_credential_safe_structured_value(value=displayed)
    _require(safe['redaction_match_count'] == 0, 'BROWNFIELD_REPORT_SENSITIVE_TEXT')
    def scan(value):
        if isinstance(value, str):
            _require(not re.search(r'\bsk-[A-Za-z0-9_-]{20,}|-----BEGIN .*PRIVATE KEY|(?:api_key|password|access_token)\s*[=:]\s*[\"\x27]?[A-Za-z0-9_/+.-]{20,}', value, re.I), 'BROWNFIELD_REPORT_SENSITIVE_TEXT')
        elif isinstance(value, dict):
            for key, child in value.items():
                scan(key)
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)
    scan(displayed)
    return data, mapped, counts


def valid_brand_png(value):
    return (isinstance(value, bytes) and 33 <= len(value) <= 65536
            and value[:16] == b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
            and 0 < int.from_bytes(value[16:20], 'big') <= 4096
            and 0 < int.from_bytes(value[20:24], 'big') <= 4096
            and value[-12:] == b'\x00\x00\x00\x00IEND\xaeB`\x82')


def render_baseline_report(*, project, profile, task, output, preview, brand_png=None):
    try:
        data, mapped, counts = _validate(project, profile, task, output, preview)
        return _render(project, profile, task, output, preview, data, mapped, counts, brand_png)
    except BaselineReportError:
        raise
    except Exception:
        raise BaselineReportError('BROWNFIELD_REPORT_IDENTITY_INVALID') from None


def _render(project, profile, task, output, preview, data, mapped, counts, brand_png):
    def text(value):
        _require(isinstance(value, str))
        return escape(value, quote=True)
    is_candidate = profile['status'] == 'candidate'
    brand = ('<img class="calligraphy-motto" alt="非己所安，不加于物" style="filter:invert(1)" src="data:image/png;base64,'
             + base64.b64encode(brand_png).decode('ascii') + '">' if valid_brand_png(brand_png)
             else '<div class="motto">非己所安，不加于物</div>')
    label = '分析已完成 · 待你审核' if is_candidate else '已确认的代码盘点' if not preview else '历史代码盘点 · 预览'
    notice = ('下方是已完成的代码分析结果。请核对模块与证据，再决定是否采用。' if is_candidate else '基于已确认的代码盘点，代码证据不等于运行验收；发送前请审阅。')
    if preview and not is_candidate:
        notice += ' 此处展示保存的历史结果，不表示当前代码版本。'
    stats = f'<div class="today-item"><div class="today-label">需求总数</div><div class="today-value"><strong>{sum(counts.values())}</strong></div></div>'
    stats += ''.join(f'<div class="today-item"><div class="today-label">{v}</div><div class="today-value"><strong>{counts[k]}</strong></div></div>' for k, v in LABELS.items())
    cards = []
    for module in data['planned_modules']:
        key = module['client_id']
        mapping = mapped[key]
        items = []
        for row in sorted(output['requirements'][key], key=lambda row: row['requirement_index']):
            status = row['status']
            refs = ''.join(f'<code>{text(ref)}</code>' for ref in row['evidence_ids']) or '<p>暂无足够证据。</p>'
            reference_label = '已检查的相关代码（仍待核实）' if status == 'unknown' else '代码证据引用'
            reference_note = '这些引用仅供核查，不代表已经实现。' if status == 'unknown' else ''
            items.append(f'<section class="requirement"><div class="heading"><strong>{text(module["requirements"][row["requirement_index"]])}</strong><span class="badge {status}">{LABELS[status]}</span></div><p>{SUMMARIES[status]}</p><details class="analysis"><summary>分析原文与技术依据</summary><p class="original">{text(row["rationale"])}</p><h4>{reference_label}</h4><p>{reference_note}</p><div class="refs">{refs}</div><p class="muted">引用编号不等于文件路径，不推断逐条对应关系。</p></details></section>')
        paths = ''.join(f'<li><code>{text(p["pattern"])}</code></li>' for p in mapping['paths'])
        local_counts = Counter(row['status'] for row in output['requirements'][key])
        count_summary = ' · '.join(f'{LABELS[status]} {local_counts[status]}' for status in LABELS)
        cards.append(
            f'<tr id="module-{text(key)}"><td class="module-name">{text(module["name"])}</td>'
            f'<td class="module-status"><span class="badge {mapping["status"]}">{LABELS[mapping["status"]]}</span></td>'
            f'<td><p>{count_summary}</p><details><summary>查看 {len(items)} 条需求与依据</summary>{"".join(items)}</details>'
            f'<details><summary>代码来源与关联文件（{len(mapping["paths"])}）</summary><ul>{paths}</ul>'
            f'<p class="original">{text(mapping["rationale"])}</p><p class="muted">保存结果未提供逐引用行号与片段哈希，不补造对应关系或源码正文。</p></details></td></tr>')

    coverage = output['coverage']
    metrics = ''.join(f'<li>{label}：{coverage.get(key) if coverage.get(key) is not None else "未提供"}</li>' for key, label in [('tracked_files', '版本库文件'), ('safe_paths', '可检查路径'), ('inspected_paths', '已检查路径'), ('unexplained_safe_paths', '待解释路径'), ('omitted_neighbor_paths', '未展开关联路径')])
    extra = ''.join(f'<h3>{text(f["name"])}</h3><p class="original">{text(f["description"])}</p><div class="refs">{"".join("<code>"+text(ref)+"</code>" for ref in f["evidence_ids"])}</div><ul>{"".join("<li><code>"+text(p["pattern"])+"</code></li>" for p in f["paths"])}</ul>' for f in data['unplanned_code_features'])
    extra_html = f'<details><summary>计划外发现（{len(data["unplanned_code_features"])}）</summary>{extra}</details>' if extra else ''
    identity = f'<p>任务：{text(task["task_id"])} · {text(task["created_at"])}</p><p>档案：{profile["id"]} · 内容 SHA-256：<code>{text(profile["content_hash"])}</code></p><p>分析代码 HEAD：<code>{text(task["identity"]["exact_head"])}</code></p>'
    if not is_candidate:
        identity += f'<p>档案确认：{text(profile["confirmed_by"])} · {text(profile["confirmed_at"])}</p>'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{escape(CSP, quote=True)}">
<title>{text(project['name'])} · 全量代码盘点安心看板</title><style>{CSS}</style></head><body>
<main class="board-shell">
<header class="board-hero"><div class="hero-main"><div><div class="kicker">代码核查 · 全量代码盘点</div>
<div class="hero-title-lock"><h1>安心看板</h1>{brand}</div></div>
<div class="hero-report"><strong>{text(project['name'])}</strong><div>{label}</div><div>分析时间：{text(task['created_at'])}</div></div></div>
<div class="today-title">本次需求核查统计</div><div class="today-grid">{stats}</div>
<div class="today-note">以下是本次需求的静态代码核查结果，不是 Git 增量指标，不代表运行验收。</div></header>
<div class="notice">{notice}</div>
<section class="board-card"><div class="section-head"><h2 class="section-title"><span class="num">01</span>功能进展</h2></div>
<div class="table-wrap"><table><thead><tr><th>功能</th><th>当前核查结果</th><th>需求与代码依据</th></tr></thead><tbody>{''.join(cards)}</tbody></table></div>{extra_html}</section>
<section class="board-card"><div class="section-head"><h2 class="section-title"><span class="num">02</span>本次核查结论</h2></div>
<div class="summary-box"><p>本次共核查 {len(data['planned_modules'])} 个模块、{sum(counts.values())} 条需求。</p>
<p>已有实现证据 {counts['implemented']} 条，部分实现 {counts['partial']} 条，待核实 {counts['unknown']} 条。</p>
<p>静态代码证据不等于运行验收；待核实不表示未实现，请结合原始依据审阅。</p></div></section>
<section class="board-card"><div class="section-head"><h2 class="section-title"><span class="num">03</span>项目经理补充</h2></div>
<div class="manager-box"><p>尚未提供项目经理补充。</p><div class="manager-provenance">本页没有代填人工意见，也未替你确认分析结果。</div></div></section>
<section class="board-card"><div class="section-head"><h2 class="section-title"><span class="num">04</span>报告信息</h2></div>
<div class="basis"><div class="basis-item"><div class="basis-label">项目名称</div><div class="basis-value">{text(project['name'])}</div></div>
<div class="basis-item"><div class="basis-label">结果状态</div><div class="basis-value">{label}</div></div>
<div class="basis-item"><div class="basis-label">来源</div><div class="basis-value">保存的代码分析结果</div></div></div>
<p>正式报告确认：尚未进行。本页不是正式审批报告。</p>
<details><summary>分析范围与结果身份</summary><ul>{metrics}</ul>
<p>证据不足的范围仍保留待核实；覆盖路径不代表穷尽整个仓库的语义。</p>
<p>这是独立代码盘点，不是增量日报；档案确认不代表运行验收或正式报告审批。本页不会调用模型、确认档案或发送邮件。</p>
<div class="identity">{identity}</div></details></section>
<footer class="board-footer">安心看板 · 简洁书法版 · 代码核查审阅</footer></main></body></html>'''
