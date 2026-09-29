"""Real isolated-browser contract tests; all documents and images are synthetic."""

import base64
import importlib
import struct
import zlib
from pathlib import Path

import pytest


def renderer():
    return importlib.import_module('app.report_screenshot')


def png_pixels(data):
    """Small independent PNG decoder for the browser's RGB/RGBA output."""
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    at, compressed = 8, b''
    while at < len(data):
        size = struct.unpack('>I', data[at:at + 4])[0]
        kind, payload = data[at + 4:at + 8], data[at + 8:at + 8 + size]
        if kind == b'IHDR':
            width, height, depth, color, *_ = struct.unpack('>IIBBBBB', payload)
        elif kind == b'IDAT':
            compressed += payload
        at += size + 12
    assert depth == 8 and color in (2, 6)
    channels = 3 if color == 2 else 4
    raw, stride, rows = zlib.decompress(compressed), width * channels, []
    prior = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        method, row = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for x in range(stride):
            a, b, c = row[x - channels] if x >= channels else 0, prior[x], prior[x - channels] if x >= channels else 0
            if method == 1:
                row[x] = (row[x] + a) & 255
            elif method == 2:
                row[x] = (row[x] + b) & 255
            elif method == 3:
                row[x] = (row[x] + (a + b) // 2) & 255
            elif method == 4:
                p = a + b - c
                distances = (abs(p - a), abs(p - b), abs(p - c))
                row[x] = (row[x] + (a, b, c)[distances.index(min(distances))]) & 255
            else:
                assert method == 0
        rows.append(row)
        prior = row
    return width, height, channels, rows


def sample_column(data, x):
    width, height, channels, rows = png_pixels(data)
    assert x < width
    return [tuple(row[x * channels:x * channels + 3]) for row in rows]


def solid_png(width, height, *, noise=False):
    import random
    randomizer = random.Random(713)
    raw = b''.join(b'\0' + (randomizer.randbytes(width * 3) if noise else b'\x16\x86\x6f' * width) for _ in range(height))
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def test_chinese_and_embedded_png_produce_readable_real_image():
    asset = base64.b64encode(solid_png(80, 30)).decode()
    html = f'<html><body style="margin:0;background:white;font-family:Microsoft YaHei;font-size:24px"><h1>安心看板 · 合成截图验收</h1><p>代码新增 58 行，删除 2 行；已确认。</p><img width="80" height="30" src="data:image/png;base64,{asset}"></body></html>'
    images = renderer().render_report_images(html)
    assert len(images) == 1
    page = images[0]
    assert isinstance(page, renderer().ReportImage)
    assert page.width >= 1400 and 100 < page.height <= 2400
    assert len(page.png) <= 1_300_000
    width, height, _, rows = png_pixels(page.png)
    assert (width, height) == (page.width, page.height)
    assert any(b'\x16\x86\x6f' in row for row in rows)
    assert sum(1 for color in sample_column(page.png, 30) if min(color) < 100) > 20


def test_long_table_preserves_each_row_on_exactly_one_page():
    colors = [(220, 25 + index * 19, 90) for index in range(10)]
    rows = ''.join(f'<tr><td style="padding:0;height:280px;background:rgb{color}">模块 {index}：合成进展内容</td></tr>' for index, color in enumerate(colors))
    html = '<html><body style="margin:0"><table style="border-collapse:collapse;width:100%">' + rows + '</table></body></html>'
    images = renderer().render_report_images(html)
    assert 2 <= len(images) <= 24
    columns = [sample_column(page.png, 1000) for page in images]
    for color in colors:
        occurrences = [column.count(color) for column in columns]
        assert sum(count > 0 for count in occurrences) == 1
        assert sum(occurrences) == 420  # 280 CSS px at the fixed 1.5x pixel density.
    assert all(len(page.png) <= 1_300_000 for page in images)


@pytest.mark.parametrize('unsafe', [
    '<script>document.body.innerHTML="changed"</script>',
    '<img src="https://example.invalid/tracker.png">',
    '<img src="file:///C:/secret.png">',
    '<div onclick="fetch(\'/secret\')">click</div>',
    '<style>@import "https://example.invalid/track.css";</style>',
    '<div style="background:url(https://example.invalid/pixel)">unsafe</div>',
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    '<meta http-equiv="refresh" content="0;url=https://example.invalid">',
])
def test_active_content_and_network_sources_are_rejected_before_render(unsafe):
    module = renderer()
    with pytest.raises(module.ReportScreenshotError) as caught:
        module.render_report_images('<html><body>' + unsafe + '</body></html>')
    assert caught.value.code == 'REPORT_SCREENSHOT_UNSAFE_HTML'


def test_oversize_paragraph_is_explicitly_rejected_instead_of_cut():
    module = renderer()
    with pytest.raises(module.ReportScreenshotError) as caught:
        module.render_report_images('<body><p style="font-size:28px;line-height:2">' + '这是一段必须完整保留的合成报告文字。' * 1000 + '</p></body>')
    assert caught.value.code == 'REPORT_SCREENSHOT_BLOCK_TOO_TALL'


def test_more_than_twenty_four_pages_is_rejected_without_partial_result():
    module = renderer()
    html = '<body style="margin:0">' + '<p style="height:1000px;margin:0">合成报告页</p>' * 25 + '</body>'
    with pytest.raises(module.ReportScreenshotError) as caught:
        module.render_report_images(html)
    assert caught.value.code == 'REPORT_SCREENSHOT_TOO_MANY_PAGES'


def test_high_entropy_page_cannot_silently_exceed_gateway_byte_limit():
    module = renderer()
    asset = base64.b64encode(solid_png(900, 900, noise=True)).decode()
    with pytest.raises(module.ReportScreenshotError) as caught:
        module.render_report_images(f'<body style="margin:0"><img width="900" height="900" src="data:image/png;base64,{asset}"></body>')
    assert caught.value.code == 'REPORT_SCREENSHOT_IMAGE_TOO_LARGE'


def test_missing_bundled_browser_has_a_safe_actionable_error(monkeypatch, tmp_path):
    module = renderer()
    monkeypatch.setattr(module, '_browser_package_directory', lambda: tmp_path)
    with pytest.raises(module.ReportScreenshotError) as caught:
        module.render_report_images('<body><p>合成报告</p></body>')
    assert caught.value.code == 'REPORT_SCREENSHOT_RUNTIME_MISSING'
    assert str(tmp_path) not in str(caught.value)


def test_pinned_windows_browser_resolves_inside_package_without_global_cache(monkeypatch, tmp_path):
    module = renderer()
    (tmp_path / 'browsers.json').write_text(
        '{"browsers":[{"name":"chromium-headless-shell","revision":"1243"}]}', encoding='utf-8')
    executable = tmp_path / '.local-browsers/chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic browser locator fixture')
    monkeypatch.setattr(module, '_browser_package_directory', lambda: tmp_path)
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(tmp_path / 'unrelated-global-cache'))
    assert Path(module._browser_executable()) == executable


def test_frozen_browser_uses_short_revision_bound_bundled_directory(monkeypatch, tmp_path):
    module = renderer()
    (tmp_path / 'browsers.json').write_text(
        '{"browsers":[{"name":"chromium-headless-shell","revision":"1243"}]}', encoding='utf-8')
    executable = tmp_path / 'report-browser/1243/chrome-headless-shell.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic frozen browser fixture')
    monkeypatch.setattr(module, '_browser_package_directory', lambda: tmp_path)
    monkeypatch.setattr(module.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(module.sys, '_MEIPASS', str(tmp_path), raising=False)
    assert Path(module._browser_executable()) == executable


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows process path constraint')
def test_long_windows_browser_path_returns_specific_safe_error(monkeypatch, tmp_path):
    module = renderer()
    package = tmp_path / ('long-synthetic-path-' * 6)
    package.mkdir()
    (package / 'browsers.json').write_text(
        '{"browsers":[{"name":"chromium-headless-shell","revision":"1243"}]}', encoding='utf-8')
    executable = package / '.local-browsers/chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic long path fixture')
    monkeypatch.setattr(module, '_browser_package_directory', lambda: package)
    with pytest.raises(module.ReportScreenshotError) as caught:
        module._browser_executable()
    assert caught.value.code == 'REPORT_SCREENSHOT_RUNTIME_PATH_TOO_LONG'
    assert str(package) not in str(caught.value)


def formal_report_html():
    """Exact production template with synthetic business inputs; no authority/DB reads."""
    from app.mail_report_renderer import _render_html
    from app.mail_brand_asset import standalone_customer_html

    report = {
        'motto': '非己所安，不加于物', 'project_name': '合成项目 · 微信图片验收',
        'report_date': '2026-09-29', 'confirmed_by': '合成确认人',
        'confirmed_at': '2026-09-29T09:00:00+08:00',
        'provider': 'synthetic-provider', 'actual_model': 'synthetic-model', 'model_version': 'fixture-v1',
        'daily_change': {'scope_note': '仅供合成截图验收，未调用真实模型或发送服务。'},
        'supplement_version_id': 1, 'manager_supplement': '今天完成合成流程检查。页面展示清楚，后续继续核验目标环境。',
        'supplement_source_type': 'pm_external_fact', 'supplement_provided_by': '合成项目经理',
        'supplement_provided_at': '2026-09-29T08:30:00+08:00', 'supplement_provided_timezone': 'Asia/Shanghai',
        'modules': [{
            'name': f'合成模块 {index + 1:02d}', 'display_stage': ('开发中', '测试中', '已完成')[index % 3],
            'summary': '已有合成数据展示和条件检查。' * (index % 3 + 1),
            'next_step': '在独立测试环境检查完整流程，保留核验记录。',
        } for index in range(32)],
    }
    narrative = {'narrative_hash': 'd' * 64, 'content': {
        'plain_summary': '这是保留原文的合成分析，用于检查长报告分页和中文显示。',
        'code_change_summary': [{
            'content': f'原文条目 {index + 1:02d}：' + '本段说明合成模块的输入检查、状态显示和异常处理，仍需按约定核验运行结果。' * 3,
            'source_type': 'ai_analysis', 'implementation_scope': '跨模块',
        } for index in range(24)],
        'test_evidence': [], 'risks': [], 'unknown_items': [], 'source_warnings': [],
    }}
    metrics = {'metrics_hash': 'e' * 64, 'metrics': {'added_lines': 58, 'deleted_lines': 2, 'changed_file_count': 3},
               'source_git_facts': {'branch': 'synthetic-main', 'from_commit': '1' * 40, 'to_commit': '2' * 40}}
    return standalone_customer_html(_render_html(report, narrative, None, metrics))


def test_formal_long_report_uses_layout_tables_without_treating_whole_sections_as_rows(monkeypatch):
    module = renderer()
    html = formal_report_html()
    assert 'data:image/png;base64,' in html and 'cid:' not in html
    measure = module._MEASURE
    monkeypatch.setattr(module, '_MEASURE', 'async args => { '
                        'if (document.compatMode !== "CSS1Compat") return {error: "DOCUMENT_MODE_CHANGED"}; '
                        'return await (' + measure + ')(args); }')
    observed = {}
    original_ranges = module._page_ranges
    def record_ranges(height, intervals):
        ranges = original_ranges(height, intervals)
        observed.update(height=height, intervals=intervals, ranges=ranges)
        return ranges
    monkeypatch.setattr(module, '_page_ranges', record_ranges)
    images = module.render_report_images(html)
    assert 4 <= len(images) <= 24
    assert all(image.width == 1680 and image.height <= 2400 and len(image.png) <= 1_300_000 for image in images)
    assert sum(image.height for image in images) == round(observed['height'] * 1.5), {
        'ranges': observed['ranges'], 'image_heights': [image.height for image in images],
    }
    for _, cut in observed['ranges'][:-1]:
        assert all(not (top < cut < bottom) for top, bottom in observed['intervals'])


def test_short_tail_is_balanced_at_a_safe_boundary_without_adding_pages():
    module = renderer()
    intervals = [[0, 640], [640, 1080], [1080, 1580], [1580, 1680]]
    ranges = module._page_ranges(1680, intervals)
    assert ranges == ((0, 640), (640, 1680))
    assert all(end - start <= 1600 for start, end in ranges)
    assert all(not (top < cut < bottom) for _, cut in ranges[:-1] for top, bottom in intervals)


def test_short_tail_stays_unchanged_when_no_more_balanced_boundary_is_safe():
    assert renderer()._page_ranges(1680, [[0, 1580], [1580, 1680]]) == ((0, 1580), (1580, 1680))


@pytest.mark.parametrize('html', ['', None, '<body><p></p></body>'])
def test_empty_documents_are_not_successful_screenshots(html):
    module = renderer()
    with pytest.raises(module.ReportScreenshotError) as caught:
        module.render_report_images(html)
    assert caught.value.code == 'REPORT_SCREENSHOT_EMPTY'
