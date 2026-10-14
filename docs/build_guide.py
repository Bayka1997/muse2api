"""Build the local illustrated guide from sanitized project HTML. No service calls."""
from pathlib import Path
import html
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs'
IMAGES = DOCS / 'images'
IMAGES.mkdir(exist_ok=True)
CHROME = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')

def screenshot(source, destination, width=1440, height=1100):
    with tempfile.TemporaryDirectory(prefix='muse-guide-') as temporary:
        folder = Path(temporary)
        page = folder / 'page.html'
        page.write_text(source, encoding='utf-8')
        result = subprocess.run([
            str(CHROME), '--headless=new', '--disable-gpu', '--no-first-run',
            '--no-sandbox', '--disable-software-rasterizer',
            '--disable-background-networking', '--hide-scrollbars',
            '--user-data-dir=' + str(folder / 'profile'),
            '--window-size=' + str(width) + ',' + str(height),
            '--screenshot=' + str(destination), page.as_uri(),
        ], capture_output=True, timeout=45)
        if not destination.is_file() or destination.stat().st_size < 5000:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-1500:])

admin = (ROOT / 'admin.html').read_text(encoding='utf-8')
admin = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', admin, flags=re.I)
admin = re.sub(r'\son\w+="[^"]*"', '', admin)
admin = admin.replace('id="inBase" readonly', 'id="inBase" value="http://localhost:18610/v1" readonly')
admin = admin.replace('id="inKey" readonly', 'id="inKey" value="•••••••••••••••• (khóa đã ẩn)" readonly')
admin = admin.replace('id="key" type="password"', 'id="key" value="KHOA_MINH_HOA" type="password"')
admin = re.sub(r'(<tbody id="poolBody">)[\s\S]*?</tbody>', r'\1<tr><td colspan="10" class="empty">Dữ liệu minh họa · Nhập tài khoản rồi bấm Kiểm tra</td></tr></tbody>', admin)
for tab, filename in [('pool', '01-tai-khoan.png'), ('play', '03-thu-api.png'), ('batch', '04-hang-loat.png')]:
    page = admin.replace('class="tab active"', 'class="tab"')
    page = page.replace('class="tab" id="tab-' + tab + '"', 'class="tab active" id="tab-' + tab + '"')
    page = page.replace('class="active" data-t="pool"', 'data-t="pool"')
    page = page.replace('data-t="' + tab + '"', 'class="active" data-t="' + tab + '"')
    # Crop irrelevant banners for readable illustrations.
    page = page.replace('</head>', '<style>.announcement,.repo-bar,#updateBanner,#authHint{display:none!important}body{zoom:.88}</style></head>')
    screenshot(page, IMAGES / filename, height=700 if tab == 'play' else 1100)

popup = (ROOT / 'extension/popup.html').read_text(encoding='utf-8')
popup = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', popup, flags=re.I)
popup = popup.replace('id="base"', 'id="base" value="http://localhost:18610"')
popup = popup.replace('id="key"', 'id="key" value="m2a_KHOA_MINH_HOA"')
popup = popup.replace('id="label"', 'id="label" value="muse-main"')
popup = popup.replace('</head>', '<style>body{width:540px;font-size:16px}label,.sub,.steps,.foot{font-size:14px}input{font-size:16px}h1{font-size:21px}</style></head>')
screenshot(popup, IMAGES / '02-tien-ich.png', 580, 640)

def inline(value):
    value = html.escape(value)
    value = re.sub(r'`([^`]+)`', r'<code>\1</code>', value)
    value = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', value)
    value = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', value)
    return value

lines = (DOCS / 'HUONG-DAN-SU-DUNG.md').read_text(encoding='utf-8').splitlines()
output = []
index = 0
while index < len(lines):
    line = lines[index]
    if line.startswith('```'):
        language = line[3:]
        code = []
        index += 1
        while index < len(lines) and not lines[index].startswith('```'):
            code.append(lines[index])
            index += 1
        output.append('<pre><code data-language="' + html.escape(language) + '">' + html.escape('\n'.join(code)) + '</code></pre>')
    elif line.startswith('|'):
        rows = []
        while index < len(lines) and lines[index].startswith('|'):
            cells = lines[index].strip('|').split('|')
            if not all(re.fullmatch(r'[\s:\-]+', cell) for cell in cells):
                tag = 'th' if not rows else 'td'
                rows.append('<tr>' + ''.join('<' + tag + '>' + inline(cell.strip()) + '</' + tag + '>' for cell in cells) + '</tr>')
            index += 1
        output.append('<div class="table-wrap"><table>' + ''.join(rows) + '</table></div>')
        continue
    elif line.startswith('#'):
        level = len(line) - len(line.lstrip('#'))
        title = line[level:].strip()
        anchor = 'section-' + str(len(output))
        output.append(f'<h{level} id="{anchor}">' + inline(title) + f'</h{level}>')
    elif line.startswith('!['):
        match = re.fullmatch(r'!\[(.*?)\]\((.*?)\)', line)
        output.append('<figure><img alt="' + html.escape(match[1]) + '" src="' + html.escape(match[2]) + '"></figure>')
    elif re.match(r'^(\d+\. |\- )', line):
        ordered = bool(re.match(r'^\d+\. ', line))
        tag = 'ol' if ordered else 'ul'
        items = []
        pattern = r'^\d+\. ' if ordered else r'^\- '
        while index < len(lines) and re.match(pattern, lines[index]):
            items.append('<li>' + inline(re.sub(pattern, '', lines[index])) + '</li>')
            index += 1
        output.append('<' + tag + '>' + ''.join(items) + '</' + tag + '>')
        continue
    elif line.strip():
        output.append('<p>' + inline(line) + '</p>')
    index += 1

style = '''
body{margin:0;background:#edf2f7;color:#253247;font:16px/1.7 "Segoe UI",Arial,sans-serif}
main{max-width:1060px;margin:35px auto;padding:45px 55px;background:white;border-radius:15px}
h1{font-size:38px;line-height:1.2;color:#12365b}h2{margin-top:48px;color:#17456e;font-size:27px}h3{font-size:20px;margin-top:26px}
li{margin:6px 0}code{font:14px/1.5 Consolas,monospace;background:#eef3f8;padding:2px 4px;border-radius:4px}
pre{background:#102337;color:#e8f1fa;padding:20px;border-radius:9px;overflow:auto}pre code{background:none;color:inherit;padding:0}
table{width:100%;border-collapse:collapse;font-size:15px}td,th{text-align:left;border:1px solid #d9e2ec;padding:10px;vertical-align:top}th{background:#eaf1f8;color:#12365b}
.table-wrap{overflow:auto}figure{margin:25px 0}img{max-width:100%;height:auto;border:1px solid #d9e2ec;border-radius:9px}
@media(max-width:700px){main{padding:24px;margin:0;border-radius:0}h1{font-size:30px}}
@media print{body{background:white}main{margin:0;padding:0;max-width:none}h2,h3{break-after:avoid}figure,pre,tr{break-inside:avoid}pre{white-space:pre-wrap}h2{margin-top:28px}}
'''
document = '<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hướng dẫn sử dụng Muse2API</title><style>' + style + '</style></head><body><main>' + '\n'.join(output) + '</main></body></html>'
(DOCS / 'HUONG-DAN-SU-DUNG.html').write_text(document, encoding='utf-8')
assert document.count('<figure>') == 4
assert document.count('<h2 ') == 16
for image in re.findall(r'<img[^>]*src="([^"]+)"', document):
    assert (DOCS / image).is_file(), image
print('Built HTML guide and four sanitized interface illustrations.')
