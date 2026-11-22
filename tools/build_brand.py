"""StockHawk for OVH brand kit: outlined SVGs (Plus Jakarta Sans, OFL) + transparent PNGs + favicon set.
python build_brand.py OUT"""
import io, os, sys
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
os.makedirs(OUT, exist_ok=True)
FONT = "/usr/share/fonts/truetype/sand-box/google/Plus Jakarta Sans/PlusJakartaSans-VariableFont_wght.ttf"


def text_path(txt, wght, size, x0, baseline, track=0.0):
    f = instantiateVariableFont(TTFont(FONT), {"wght": wght})
    gs, cmap, upm = f.getGlyphSet(), f.getBestCmap(), f["head"].unitsPerEm
    s = size / upm; pen = SVGPathPen(gs); x = x0
    for ch in txt:
        g = cmap[ord(ch)]
        gs[g].draw(TransformPen(pen, (s, 0, 0, -s, x, baseline)))
        x += gs[g].width * s + track
    return pen.getCommands(), x


C1, C2 = "#22d3ee", "#2563eb"
NAVY = "#0b1630"
# hawk head facing right, 100x100 box
HEAD = "M30 1 L61 1 L74 13 L95 26 L100 44 L93 37 L72 45 L27 92 L19 46 L1 44 Z"


def icon(gid="shg", bg=None):
    eye = (f'<path d="M50 9 a16 16 0 1 0 0 32 c8 0 15-3 22-8 c-6-1-8-7-9-11 c-2-8-6-13-13-13Z" fill="{NAVY}"/>'
           f'<g fill="none" stroke="url(#{gid})" stroke-width="2.4"><circle cx="49" cy="25" r="11"/><circle cx="49" cy="25" r="5.6"/></g>'
           f'<g stroke="url(#{gid})" stroke-width="2.2" stroke-linecap="round"><path d="M49 11.5v5M49 33.5v5M35.5 25h5M57.5 25h4"/></g>'
           f'<circle cx="49" cy="25" r="2" fill="{C1}"/>')
    grad = (f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C1}"/>'
            f'<stop offset="1" stop-color="{C2}"/></linearGradient></defs>')
    return grad, f'<path d="{HEAD}" fill="url(#{gid})"/>' + eye


def svg_icon(pad=4, bg=None, rx=0):
    g, body = icon()
    b = f'<rect x="-{pad}" y="-{pad}" width="{100+2*pad}" height="{100+2*pad}" rx="{rx}" fill="{bg}"/>' if bg else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-{pad} -{pad} {100+2*pad} {100+2*pad}">{g}{b}{body}</svg>')


def svg_wordmark(word="#ffffff", sub="#9ca3af", bg=None, stacked_sub=True):
    g, body = icon()
    p1, x1 = text_path("StockHawk", 700, 74, 118, 64, -1.0)
    p2, x2 = text_path("for OVH", 500, 34, 0, 0, 1.2)
    sub_x = 118 + (x1 - 118 - x2) / 2   # centred under the wordmark like the reference
    p2, _ = text_path("for OVH", 500, 34, sub_x, 112, 1.2)
    W, H = int(x1 + 6), 122
    b = f'<rect width="{W}" height="{H}" fill="{bg}"/>' if bg else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">{g}{b}'
            f'<g transform="translate(0 8) scale(1.0)">{body}</g><path d="{p1}" fill="{word}"/><path d="{p2}" fill="{sub}"/></svg>')


files = {
    "stockhawk-icon.svg": svg_icon(),
    "stockhawk-icon-tile.svg": svg_icon(pad=14, bg=NAVY, rx=26),
    "stockhawk-wordmark.svg": svg_wordmark(),                       # for dark backgrounds
    "stockhawk-wordmark-light-bg.svg": svg_wordmark(word=NAVY, sub="#64748b"),
    "stockhawk-wordmark-navy.svg": svg_wordmark(bg="#050d24"),
}
for n, s in files.items():
    open(os.path.join(OUT, n), "w").write(s)

from playwright.sync_api import sync_playwright
from PIL import Image


def png(svg, path, w, h):
    pg.set_viewport_size({"width": w, "height": h})
    pg.set_content(f'<html><body style="margin:0;background:transparent"><div style="width:{w}px;height:{h}px">'
                   + svg.replace("<svg ", f'<svg style="width:{w}px;height:{h}px;display:block" ', 1) + "</div></body></html>")
    pg.screenshot(path=path, omit_background=True, clip={"x": 0, "y": 0, "width": w, "height": h})


with sync_playwright() as p:
    br = p.chromium.launch(executable_path="/usr/bin/google-chrome"); pg = br.new_page()
    import re
    W = int(re.search(r'width="(\d+)"', files["stockhawk-wordmark.svg"]).group(1))
    for n in ("stockhawk-wordmark", "stockhawk-wordmark-light-bg", "stockhawk-wordmark-navy"):
        png(files[n + ".svg"], os.path.join(OUT, n + ".png"), W * 2, 244)
    for sz in (512, 192, 180, 64, 48, 32, 16):
        png(files["stockhawk-icon.svg"], os.path.join(OUT, f"stockhawk-icon-{sz}.png"), sz, sz)
    png(files["stockhawk-icon-tile.svg"], os.path.join(OUT, "apple-touch-icon.png"), 180, 180)
    png(files["stockhawk-icon-tile.svg"], os.path.join(OUT, "stockhawk-avatar-512.png"), 512, 512)
    br.close()
ims = [Image.open(os.path.join(OUT, f"stockhawk-icon-{s}.png")) for s in (16, 32, 48, 64)]
ims[-1].save(os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
print("ok", sorted(os.listdir(OUT)))
