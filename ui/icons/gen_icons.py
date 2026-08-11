#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 nasdash 三套图标：line(线条) / color(彩色) / three(彩色3D)。"""
import os

OUT = "/Users/hanyu/WorkBuddy/2026-07-01-17-37-29/nasdash/ui/icons"
LINE_DIR = os.path.join(OUT, "line")
COLOR_DIR = os.path.join(OUT, "color")
THREE_DIR = os.path.join(OUT, "three")
for d in (LINE_DIR, COLOR_DIR, THREE_DIR):
    os.makedirs(d, exist_ok=True)

# 每个图标: (文件名, 中文标签, 品牌色, [SVG元素...])
ICONS = [
    ("detect", "硬件配置检测", "#0066FF", [
        '<circle cx="10.5" cy="10.5" r="6"/>',
        '<line x1="14.8" y1="14.8" x2="19" y2="19"/>',
    ]),
    ("system", "系统资源", "#00B96B", [
        '<rect x="7" y="7" width="10" height="10" rx="1.5"/>',
        '<rect x="9.5" y="9.5" width="5" height="5" rx="0.5"/>',
        '<line x1="10" y1="4" x2="10" y2="7"/><line x1="14" y1="4" x2="14" y2="7"/>',
        '<line x1="10" y1="17" x2="10" y2="20"/><line x1="14" y1="17" x2="14" y2="20"/>',
        '<line x1="4" y1="10" x2="7" y2="10"/><line x1="4" y1="14" x2="7" y2="14"/>',
        '<line x1="17" y1="10" x2="20" y2="10"/><line x1="17" y1="14" x2="20" y2="14"/>',
    ]),
    ("history", "历史趋势", "#FF7A45", [
        '<polyline points="3,16 9,10 13,14 21,6"/>',
        '<polyline points="16,6 21,6 21,11"/>',
        '<line x1="3" y1="20" x2="21" y2="20"/>',
    ]),
    ("raid", "阵列卡", "#722ED1", [
        '<rect x="4" y="5" width="16" height="4" rx="1"/>',
        '<rect x="4" y="10" width="16" height="4" rx="1"/>',
        '<rect x="4" y="15" width="16" height="4" rx="1"/>',
        '<line x1="20" y1="7" x2="20" y2="17"/>',
    ]),
    ("hdd", "硬盘 SMART", "#13C2C2", [
        '<circle cx="12" cy="12" r="8.5"/>',
        '<circle cx="12" cy="12" r="2.2"/>',
        '<path d="M13.2 10.8 L19 5"/>',
    ]),
    ("storage", "存储卷", "#EB2F96", [
        '<ellipse cx="12" cy="6" rx="7" ry="2.5"/>',
        '<path d="M5 6 v12 a7 2.5 0 0 0 14 0 v-12"/>',
        '<path d="M5 12 a7 2.5 0 0 0 14 0"/>',
    ]),
    ("fan", "风扇控制", "#2F54EB", [
        '<circle cx="12" cy="12" r="8.5"/>',
        '<circle cx="12" cy="12" r="1.6"/>',
        '<path d="M12 12 L12 4"/><path d="M12 12 L19.4 16"/><path d="M12 12 L4.6 16"/>',
    ]),
    ("docker", "Docker", "#1890FF", [
        '<rect x="4" y="9" width="4" height="4" rx="0.8"/>',
        '<rect x="9" y="9" width="4" height="4" rx="0.8"/>',
        '<rect x="14" y="9" width="4" height="4" rx="0.8"/>',
        '<rect x="7" y="4" width="4" height="4" rx="0.8"/>',
        '<path d="M3 16 q9 4 18 0"/>',
    ]),
    ("automation", "控制与自动化", "#FAAD14", [
        '<line x1="4" y1="8" x2="20" y2="8"/>',
        '<circle cx="8" cy="8" r="2.5"/>',
        '<line x1="4" y1="16" x2="20" y2="16"/>',
        '<circle cx="15" cy="16" r="2.5"/>',
    ]),
    ("manual", "操作手册", "#52C41A", [
        '<path d="M4 5.5 C 7 4 10 4.5 12 6 c2-1.5 5-2 8-0.5 v12 c-3-1.5-6-1-8 0.5 c-2-1.5-5-2-8-0.5 z"/>',
        '<line x1="12" y1="6" x2="12" y2="17.5"/>',
    ]),
    ("about", "关于 nasdash", "#8C8C8C", [
        '<circle cx="12" cy="12" r="8.5"/>',
        '<line x1="12" y1="11" x2="12" y2="16"/>',
        '<circle cx="12" cy="7.8" r="1.1" data-fill="1"/>',
    ]),
]

def line_svg(elems):
    g = "\n      ".join(elems)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">\n'
            f'  <g fill="none" stroke="#0066FF" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">\n      {g}\n  </g>\n</svg>')

def color_svg(elems, brand):
    out = [e.replace('data-fill="1"', f'fill="{brand}" stroke="{brand}"') if 'data-fill="1"' in e else e for e in elems]
    g = "\n      ".join(out)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">\n'
            f'  <rect x="2" y="2" width="20" height="20" rx="6" fill="{brand}" fill-opacity="0.12"/>\n'
            f'  <g fill="none" stroke="{brand}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">\n      {g}\n  </g>\n</svg>')

def _mix(hexc, target, f):
    r = int(hexc[1:3], 16); g = int(hexc[3:5], 16); b = int(hexc[5:7], 16)
    r = round(r + (target[0] - r) * f); g = round(g + (target[1] - g) * f); b = round(b + (target[2] - b) * f)
    return f"#{r:02x}{g:02x}{b:02x}"
lighten = lambda c, f: _mix(c, (255, 255, 255), f)
darken  = lambda c, f: _mix(c, (0, 0, 0), f)

def three_svg(elems, brand, name):
    light = lighten(brand, 0.22); dark = darken(brand, 0.14); shadow = darken(brand, 0.50); glyph = darken(brand, 0.34)
    gid, fid = f"g_{name}", f"f_{name}"
    g = "\n      ".join(elems)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">\n'
            f'  <defs>\n'
            f'    <linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">\n'
            f'      <stop offset="0" stop-color="{light}"/><stop offset="1" stop-color="{dark}"/>\n'
            f'    </linearGradient>\n'
            f'    <filter id="{fid}" x="-30%" y="-30%" width="160%" height="160%">\n'
            f'      <feDropShadow dx="0" dy="1.4" stdDeviation="1.3" flood-color="{shadow}" flood-opacity="0.45"/>\n'
            f'    </filter>\n'
            f'  </defs>\n'
            f'  <rect x="2.5" y="2.5" width="19" height="19" rx="6" fill="url(#{gid})" filter="url(#{fid})"/>\n'
            f'  <g transform="translate(0,1.4)" fill="{shadow}" stroke="{shadow}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">\n      {g}\n  </g>\n'
            f'  <g fill="{glyph}" stroke="{glyph}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">\n      {g}\n  </g>\n'
            f'</svg>')

# 生成三套图标文件
for name, label, brand, elems in ICONS:
    open(os.path.join(LINE_DIR, name + ".svg"), "w").write(line_svg(elems))
    open(os.path.join(COLOR_DIR, name + ".svg"), "w").write(color_svg(elems, brand))
    open(os.path.join(THREE_DIR, name + ".svg"), "w").write(three_svg(elems, brand, name))
print("generated", len(ICONS), "icons x3 sets")

# 预览 HTML（三栏：线条 / 彩色 / 彩色3D）
rows = []
for name, label, brand, elems in ICONS:
    ls = line_svg(elems).replace('width="24" height="24"', 'width="100%" height="100%"')
    cs = color_svg(elems, brand).replace('width="24" height="24"', 'width="100%" height="100%"')
    ts = three_svg(elems, brand, name).replace('width="24" height="24"', 'width="100%" height="100%"')
    rows.append(f'''
    <div class="card">
      <div class="name">{label}</div>
      <div class="pair">
        <div class="cell"><div class="ico">{ls}</div><div class="tag">线条版</div></div>
        <div class="cell"><div class="ico">{cs}</div><div class="tag">彩色版</div></div>
        <div class="cell"><div class="ico">{ts}</div><div class="tag">彩色3D</div></div>
      </div>
    </div>''')

html_doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>nasdash 图标方案预览</title>
<style>
  body{{margin:0;background:#0f1115;color:#e6e6e6;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:28px}}
  h1{{font-size:20px;font-weight:600;margin:0 0 4px}}
  .sub{{color:#9aa0a6;font-size:13px;margin-bottom:22px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}}
  .card{{background:#1a1e23;border:1px solid #2a2f37;border-radius:12px;padding:16px}}
  .name{{font-size:14px;font-weight:600;margin-bottom:12px;color:#fff}}
  .pair{{display:flex;gap:12px}}
  .cell{{flex:1;display:flex;flex-direction:column;align-items:center;gap:8px}}
  .ico{{width:64px;height:64px;display:flex;align-items:center;justify-content:center}}
  .ico svg{{width:64px;height:64px;display:block}}
  .tag{{font-size:12px;color:#9aa0a6}}
  .hint{{margin-top:24px;font-size:13px;color:#9aa0a6;line-height:1.7}}
</style></head><body>
<h1>nasdash 图标方案预览</h1>
<div class="sub">左→右：线条版（飞牛蓝）　·　彩色版（色块底+品牌色线条）　·　彩色3D（渐变立体底+白色浮雕+投影）</div>
<div class="grid">{''.join(rows)}</div>
<div class="hint">三套均为 SVG 矢量，任意尺寸都清晰。你看完告诉我用哪套（或混搭），我再替换进 app.py / 模板并热更到 158。</div>
</body></html>'''

preview = "/Users/hanyu/WorkBuddy/2026-07-01-17-37-29/nasdash/ui/icons/preview.html"
open(preview, "w").write(html_doc)
print("preview ->", preview)
