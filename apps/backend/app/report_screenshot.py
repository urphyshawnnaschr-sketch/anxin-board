"""Bounded, offline screenshots of already-approved standalone report HTML.

This module never reads a report, invokes a model, or sends a message. Only the
bundled browser is used; the user's browser/profile and global browser cache are
not fallbacks. Page boundaries preserve paragraphs, data rows and text lines.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import struct
import sys
import threading
import time


_WIDTH = 1120
_PAGE_HEIGHT = 1600
_SCALE = 1.5
_MAX_BYTES = 1_300_000
_MAX_PAGES = 24
_MAX_HTML_BYTES = 4_000_000
_RENDER_LOCK = threading.BoundedSemaphore(1)
_TAGS = frozenset('html head body meta title p h1 h2 h3 h4 h5 h6 div span strong b em i u s ul ol li table thead tbody tfoot tr td th caption colgroup col br hr img details summary code pre blockquote'.split())
_ATTRIBUTES = frozenset('id class style lang charset name content role width height alt src border cellspacing cellpadding bgcolor align valign colspan rowspan open scope'.split())
_CSP = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"


class ReportScreenshotError(RuntimeError):
    """Safe fixed code only: no HTML, local paths or browser diagnostic output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReportImage:
    png: bytes
    width: int
    height: int


def _fail(code: str) -> ReportScreenshotError:
    return ReportScreenshotError('REPORT_SCREENSHOT_' + code)


class _PassiveHTML(HTMLParser):
    """Reject active markup before it reaches even the isolated browser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 30_000:
            raise _fail('DOCUMENT_TOO_LARGE')
        if tag not in _TAGS:
            raise _fail('UNSAFE_HTML')
        seen = set()
        for name, value in attrs:
            if name not in _ATTRIBUTES or name in seen:
                raise _fail('UNSAFE_HTML')
            seen.add(name)
            if name == 'src':
                if tag != 'img' or not isinstance(value, str) or not value.startswith('data:image/png;base64,'):
                    raise _fail('UNSAFE_HTML')
                try:
                    png = base64.b64decode(value[22:], validate=True)
                    width, height = struct.unpack('>II', png[16:24])
                except (ValueError, TypeError, struct.error):
                    raise _fail('UNSAFE_HTML') from None
                if (png[:8] != b'\x89PNG\r\n\x1a\n' or png[12:16] != b'IHDR'
                        or width == 0 or height == 0 or width * height > 16_000_000):
                    raise _fail('UNSAFE_HTML')
            if name == 'style' and (not isinstance(value, str) or re.search(
                    r'url\s*\(|image-set\s*\(|expression\s*\(|behavior\s*:|@|\\|/\*', value, re.I)):
                raise _fail('UNSAFE_HTML')
        if tag == 'img' and 'src' not in seen:
            raise _fail('UNSAFE_HTML')

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag not in _TAGS:
            raise _fail('UNSAFE_HTML')


def _browser_package_directory() -> Path:
    try:
        import playwright
    except ImportError:
        raise _fail('RUNTIME_MISSING') from None
    return Path(playwright.__file__).resolve().parent / 'driver' / 'package'


def _browser_executable() -> str:
    package = _browser_package_directory()
    try:
        manifest = json.loads((package / 'browsers.json').read_text(encoding='utf-8'))
        revision = next(item['revision'] for item in manifest['browsers'] if item['name'] == 'chromium-headless-shell')
        if not isinstance(revision, str) or not revision.isdecimal():
            raise ValueError()
        if getattr(sys, 'frozen', False):
            # A short explicit destination avoids MAX_PATH under the installer's
            # versions/<64-character digest>/AnxinBoard.Runtime directory.
            root = Path(sys._MEIPASS) / 'report-browser' / revision
            pattern = ''
        else:
            root = package / '.local-browsers' / ('chromium_headless_shell-' + revision)
            pattern = '*/'
        candidates = [path for name in ('chrome-headless-shell.exe', 'chrome-headless-shell') for path in root.glob(pattern + name) if path.is_file()]
        if len(candidates) != 1:
            raise ValueError()
        executable = str(candidates[0])
        if os.name == 'nt' and len(executable) >= 260:
            raise _fail('RUNTIME_PATH_TOO_LONG')
        return executable
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        raise _fail('RUNTIME_MISSING') from None


# Executed through the trusted browser-control channel with document JavaScript
# disabled. No report-provided script is executed or interpolated into this code.
_MEASURE = r"""async ({maxHeight, width, scale}) => {
    for (const element of document.querySelectorAll('details')) element.open = true;
    await document.fonts.ready;
    for (const image of document.images) {
        try { await image.decode(); } catch { return {error: 'ASSET_INVALID'}; }
        if (!image.naturalWidth) return {error: 'ASSET_INVALID'};
    }
    if (!document.body || (!document.body.innerText.trim() && !document.images.length))
        return {error: 'EMPTY'};
    const intervals = [];
    let bottom = document.body.getBoundingClientRect().bottom;
    const add = rect => {
        if (rect.height <= 0 || rect.width <= 0) return;
        if (rect.left < -0.5 || rect.right > width + 0.5) throw new Error('HORIZONTAL_OVERFLOW');
        bottom = Math.max(bottom, rect.bottom);
        // Quantize shared edges identically on the output pixel grid. Rounding
        // one row's bottom up and its neighbour's top down makes them overlap,
        // falsely turning a long data table into one unbreakable block.
        intervals.push([Math.max(0, Math.round(rect.top * scale) / scale), Math.round(rect.bottom * scale) / scale]);
    };
    try {
        for (const element of document.body.querySelectorAll('*')) {
            const style = getComputedStyle(element);
            if (style.position === 'fixed' || style.position === 'absolute' || style.position === 'sticky'
                || style.transform !== 'none' || style.contentVisibility === 'hidden')
                return {error: 'UNSUPPORTED_LAYOUT'};
            // Formal email HTML uses presentation tables as section containers.
            // Their rows may span several pages; only actual data rows are atomic.
            const dataRow = element.tagName === 'TR' && !element.querySelector('table')
                && element.closest('table')?.getAttribute('role') !== 'presentation';
            const atomic = element.matches('p,pre,blockquote,h1,h2,h3,h4,h5,h6,img,li') || dataRow;
            if (atomic) {
                const rect = element.getBoundingClientRect();
                if (rect.height > maxHeight) return {error: 'BLOCK_TOO_TALL'};
                add(rect);
            }
        }
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const node = walker.currentNode;
            if (!node.textContent.trim()) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const rect of range.getClientRects()) add(rect);
        }
    } catch (error) { return {error: error.message === 'HORIZONTAL_OVERFLOW' ? error.message : 'LAYOUT_INVALID'}; }
    bottom += Math.max(0, parseFloat(getComputedStyle(document.body).marginBottom) || 0);
    return {height: Math.ceil(bottom * scale) / scale, intervals};
}"""


def _page_ranges(height: float, intervals: list[list[float]]) -> tuple[tuple[float, float], ...]:
    if height <= 0:
        raise _fail('EMPTY')
    if height > _PAGE_HEIGHT * _MAX_PAGES:
        raise _fail('TOO_MANY_PAGES')
    ranges, start = [], 0
    while start < height:
        cut = min(height, start + _PAGE_HEIGHT)
        while cut < height:
            crossed = [top for top, bottom in intervals if top < cut < bottom]
            if not crossed:
                break
            cut = min(crossed)
        if cut <= start:
            raise _fail('BLOCK_TOO_TALL')
        ranges.append((start, cut))
        if len(ranges) > _MAX_PAGES:
            raise _fail('TOO_MANY_PAGES')
        start = cut
    if len(ranges) > 1 and ranges[-1][1] - ranges[-1][0] < 400:
        # Avoid sending a footer-only image. Move just the final shared boundary
        # to the nearest safe point to the pair's midpoint; preserve page count.
        pair_start, pair_end = ranges[-2][0], ranges[-1][1]
        midpoint = round((pair_start + pair_end) * _SCALE / 2) / _SCALE
        merged = []
        for top, bottom in sorted(intervals):
            if merged and top < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], bottom)
            else:
                merged.append([top, bottom])
        candidates = [edge for interval in merged for edge in interval]
        if not any(top < midpoint < bottom for top, bottom in merged):
            candidates.append(midpoint)
        candidates.append(ranges[-1][0])
        safe = [cut for cut in candidates if pair_start < cut < pair_end
                and cut - pair_start <= _PAGE_HEIGHT and pair_end - cut <= _PAGE_HEIGHT]
        cut = min(safe, key=lambda value: (abs(value - midpoint), value))
        ranges[-2:] = [(pair_start, cut), (cut, pair_end)]
    return tuple(ranges)


def render_report_images(html: str) -> tuple[ReportImage, ...]:
    """Render the entire passive report or fail without returning partial pages.

    A block too tall to fit is explicitly rejected; no text is silently clipped,
    shrunk to unreadability or rewritten. Every returned PNG obeys the byte cap.
    """
    if not isinstance(html, str) or not html.strip():
        raise _fail('EMPTY')
    if len(html.encode('utf-8')) > _MAX_HTML_BYTES:
        raise _fail('DOCUMENT_TOO_LARGE')
    parser = _PassiveHTML()
    try:
        parser.feed(html)
        parser.close()
    except ReportScreenshotError:
        raise
    except (ValueError, RecursionError):
        raise _fail('UNSAFE_HTML') from None
    executable = _browser_executable()
    if not _RENDER_LOCK.acquire(blocking=False):
        raise _fail('BUSY')
    try:
        from playwright.sync_api import Error as BrowserError, sync_playwright
        deadline = time.monotonic() + 90
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=executable, headless=True, timeout=20_000,
                chromium_sandbox=True,
                args=['--disable-background-networking', '--disable-component-update', '--no-proxy-server'],
            )
            try:
                context = browser.new_context(
                    viewport={'width': _WIDTH, 'height': _PAGE_HEIGHT}, device_scale_factor=_SCALE,
                    java_script_enabled=False, offline=True, service_workers='block',
                    accept_downloads=False, locale='zh-CN', color_scheme='light',
                )
                context.route('**/*', lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(15_000)
                csp = '<meta http-equiv="Content-Security-Policy" content="' + _CSP + '">'
                # Keep the formal HTML5 doctype first: a meta tag before it
                # would silently switch the approved layout to quirks mode.
                doctype = re.match(r'\s*<!doctype\s+html\s*>', html, re.I)
                at = doctype.end() if doctype else 0
                document = html[:at] + csp + html[at:]
                page.set_content(document, wait_until='load')
                layout = page.evaluate(_MEASURE, {'maxHeight': _PAGE_HEIGHT, 'width': _WIDTH, 'scale': _SCALE})
                if 'error' in layout:
                    raise _fail(layout['error'])
                ranges = _page_ranges(layout['height'], layout['intervals'])
                # scrollHeight is integer-valued, while text layout and clips
                # can end at fractional CSS pixels. Supply unrendered trailing
                # space so full_page's integer bounds cannot crop the last row
                # of output pixels. Existing content positions stay unchanged.
                page.evaluate("""() => {
                    const root = document.documentElement;
                    const padding = parseFloat(getComputedStyle(root).paddingBottom) || 0;
                    root.style.paddingBottom = (padding + 2) + 'px';
                }""")
                images = []
                for start, end in ranges:
                    if time.monotonic() > deadline:
                        raise _fail('TIMEOUT')
                    png = page.screenshot(
                        type='png', full_page=True,
                        clip={'x': 0, 'y': start, 'width': _WIDTH, 'height': end - start},
                        animations='disabled', caret='hide', timeout=15_000,
                    )
                    if len(png) > _MAX_BYTES:
                        raise _fail('IMAGE_TOO_LARGE')
                    width, height = struct.unpack('>II', png[16:24])
                    images.append(ReportImage(png=png, width=width, height=height))
                return tuple(images)
            finally:
                browser.close()
    except ReportScreenshotError:
        raise
    except (ImportError, OSError):
        raise _fail('RUNTIME_MISSING') from None
    except BrowserError:
        raise _fail('RENDER_FAILED') from None
    finally:
        _RENDER_LOCK.release()
