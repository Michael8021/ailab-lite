"""Build a designed, editable .pptx from a JSON deck spec.

    python build.py deck.json out.pptx          build
    python build.py --themes                    list themes
    python build.py --icons                     list icon names
    python build.py --example                   print an example spec

The spec format is documented in ../SKILL.md. Everything is drawn with native PowerPoint shapes,
text boxes, tables and charts, so the result stays editable. Warnings (text that had to shrink a lot,
missing images or icons) are printed as lines starting with "WARN"; fix them in the spec and rebuild.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

HERE = Path(__file__).resolve().parent
ICON_DIR = HERE.parent / "assets" / "icons"
ICON_CACHE = Path("/tmp/pptx-icon-cache")
FONT = "微软雅黑"  # the sandbox maps it to Noto Sans CJK; Office users have it natively
W, H = Inches(13.333), Inches(7.5)
MARGIN = Inches(0.7)

THEMES: dict[str, dict[str, str]] = {
    # name: primary (titles, accents), dark (cover / section backgrounds), accent (highlights),
    #       soft (tinted panels), bg / text / muted (content slides), chart (series colours)
    "azure": {"primary": "1F5AA6", "dark": "0E2A52", "accent": "F2A93B", "soft": "EAF1FB", "bg": "FFFFFF", "text": "1B2330",
              "muted": "5E6B7D", "chart": "1F5AA6,F2A93B,4FB3BF,8C6BB1,E4572E,7A8B99"},
    "emerald": {"primary": "0F766E", "dark": "0A3A36", "accent": "F59E0B", "soft": "E7F5F2", "bg": "FFFFFF", "text": "17252A",
                "muted": "5B6B6E", "chart": "0F766E,F59E0B,3B82F6,A855F7,EF4444,64748B"},
    "sunset": {"primary": "C2410C", "dark": "3B1406", "accent": "0EA5E9", "soft": "FDF0E8", "bg": "FFFFFF", "text": "2A1B14",
               "muted": "6E5B52", "chart": "C2410C,0EA5E9,F59E0B,14B8A6,7C3AED,78716C"},
    "violet": {"primary": "6D28D9", "dark": "241047", "accent": "EC4899", "soft": "F2ECFD", "bg": "FFFFFF", "text": "1E1B2E",
               "muted": "625C77", "chart": "6D28D9,EC4899,06B6D4,F59E0B,10B981,64748B"},
    "crimson": {"primary": "B3261E", "dark": "3D0C0A", "accent": "C9A227", "soft": "FBEDEC", "bg": "FFFFFF", "text": "251716",
                "muted": "6B5755", "chart": "B3261E,C9A227,3F6E8C,6B8E23,8E5572,7A7A7A"},
    "graphite": {"primary": "2563EB", "dark": "0B1220", "accent": "22D3EE", "soft": "EEF2F7", "bg": "FFFFFF", "text": "111827",
                 "muted": "5B6474", "chart": "2563EB,22D3EE,F59E0B,10B981,F43F5E,94A3B8"},
    "midnight": {"primary": "60A5FA", "dark": "070D1A", "accent": "FBBF24", "soft": "16213A", "bg": "0D1528", "text": "E8EEF8",
                 "muted": "9AA8BF", "chart": "60A5FA,FBBF24,34D399,F472B6,A78BFA,94A3B8"},
}

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)


# ------------------------------------------------------------------------------------------ helpers


def rgb(hex6: str) -> RGBColor:
    return RGBColor.from_string(hex6.lstrip("#").upper())


def mix(a: str, b: str, t: float) -> str:
    """Blend colour a towards b by t (0..1)."""
    a, b = a.lstrip("#"), b.lstrip("#")
    ca = [int(a[i : i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i : i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb, strict=True))


def is_dark(hex6: str) -> bool:
    h = hex6.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b < 140


def set_alpha(shape, alpha: float) -> None:
    """Fill transparency (0 = invisible, 1 = opaque); python-pptx has no API for it."""
    clr = shape.fill._xPr.find(qn("a:solidFill")).find(qn("a:srgbClr"))
    el = etree.SubElement(clr, qn("a:alpha"))
    el.set("val", str(int(alpha * 100000)))


def rect(slide, x, y, w, h, color: str, shape=MSO_SHAPE.RECTANGLE, alpha: float | None = None, radius: float | None = None, line: str | None = None):
    s = slide.shapes.add_shape(shape, x, y, w, h)
    style = s._element.find(qn("p:style"))
    if style is not None:  # the default style adds a theme shadow (LibreOffice draws it) and white text
        s._element.remove(style)
    s.fill.solid()
    s.fill.fore_color.rgb = rgb(color)
    if line:
        s.line.color.rgb = rgb(line)
        s.line.width = Pt(1)
    else:
        s.line.fill.background()
    if alpha is not None:
        set_alpha(s, alpha)
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        s.adjustments[0] = radius
    return s


def _em(c: str) -> float:
    o = ord(c)
    # CJK, full-width forms, and the curly quotes / dashes / ellipsis that CJK fonts draw full width
    if o > 0x2E80 or 0x2000 <= o <= 0x206F or c in "·•":
        return 1.0
    if c in "%@&WMwm":
        return 0.9
    if c.isdigit() or c.isupper() or c in "$¥€#?":
        return 0.65
    if c in " .,:;!'|ilj()[]":
        return 0.32
    return 0.56


def text_units(text: str) -> float:
    """Width of a string in 'em': CJK and full-width ≈ 1, latin ≈ 0.55."""
    return sum(_em(c) for c in text)


def line_count(text: str, box_w, size: float) -> int:
    """Wrapped lines of one paragraph. 0.94: CJK line breaking may not split before punctuation."""
    per_line = max(1.0, (box_w / 12700 - 10) / size * 0.94)
    return max(1, -(-int(text_units(text) * 100) // int(per_line * 100)))


def text_height(lines: list[str], box_w, size: float, spacing: float = 1.25, gap: float = 0.35) -> int:
    """Height in EMU that the paragraphs take at this size."""
    rows = sum(line_count(t, box_w, size) for t in lines) if lines else 1
    # "single" line spacing is ~1.2 em for Noto Sans CJK / YaHei; line_spacing multiplies that
    return int((rows * size * spacing * 1.2 + max(0, len(lines) - 1) * size * gap) * 12700)


def fit_size(lines: list[str], box_w, box_h, start: float, minimum: float, spacing: float = 1.25, gap: float = 0.35) -> float:
    """Largest font size (pt) at which the lines fit the box, wrapping by width."""
    size = start
    while size > minimum:
        if text_height(lines, box_w, size, spacing, gap) <= box_h - 6 * 12700:
            return size
        size -= 1
    return minimum


def set_font(run, size: float, color: str, bold: bool = False, italic: bool = False) -> None:
    f = run.font
    f.size = Pt(size)
    f.bold = bold
    f.italic = italic
    f.color.rgb = rgb(color)
    f.name = FONT
    rpr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rpr.find(qn(tag))
        if el is None:
            el = etree.SubElement(rpr, qn(tag))
        el.set("typeface", FONT)


def text(slide, x, y, w, h, content, size: float, color: str, bold: bool = False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
         spacing: float = 1.15, min_size: float | None = None, italic: bool = False, gap_pt: float | None = None, where: str = ""):
    """A text box. `content` is a string or a list of paragraphs; shrinks to fit down to min_size."""
    paras = content if isinstance(content, list) else [content]
    paras = [str(p) for p in paras]
    if min_size is not None:
        fitted = fit_size(paras, w, h, size, min_size, spacing)
        if fitted <= min_size and fit_size(paras, w, h, min_size, min_size - 4, spacing) < min_size:
            warn(f"{where}: text is too long for its box even at {min_size}pt; shorten it")
        size = fitted
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for m in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(tf, m, Emu(0))
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        if i and gap_pt is not None:
            p.space_before = Pt(gap_pt)
        run = p.add_run()
        run.text = para
        set_font(run, size, color, bold, italic)
    return tb


def icon_png(name: str, color: str) -> Path | None:
    src = ICON_DIR / f"{name}.png"
    if not src.is_file():
        warn(f"unknown icon {name!r} (run build.py --icons)")
        return None
    ICON_CACHE.mkdir(parents=True, exist_ok=True)
    out = ICON_CACHE / f"{name}-{color}.png"
    if not out.exists():
        im = Image.open(src).convert("RGBA")
        tinted = Image.new("RGBA", im.size, tuple(int(color[i : i + 2], 16) for i in (0, 2, 4)) + (255,))
        tinted.putalpha(im.getchannel("A"))
        tinted.save(out)
    return out


def icon(slide, name: str | None, x, y, size, color: str, bg: str | None = None, shape=MSO_SHAPE.OVAL):
    """An icon, optionally centred on a coloured circle / rounded square of the given size."""
    if not name:
        return
    if bg:
        rect(slide, x, y, size, size, bg, shape=shape, radius=0.22 if shape == MSO_SHAPE.ROUNDED_RECTANGLE else None)
        pad = int(size * 0.26)
        x, y, size = x + pad, y + pad, size - 2 * pad
    png = icon_png(name, color)
    if png:
        slide.shapes.add_picture(str(png), x, y, size, size)


def load_credits(image: str) -> str:
    """Attribution for a picture fetched by search_images (credits.json next to it), if any."""
    try:
        path = Path(image)
        credits = json.loads((path.parent / "credits.json").read_text(encoding="utf-8"))
        return credits.get(path.name, {}).get("attribution", "")
    except Exception:
        return ""


def picture(slide, image: str | None, x, y, w, h, where: str, credit_color: str | None = "FFFFFF"):
    """Place an image filling the frame (centre-cropped to its aspect ratio). Returns False if absent."""
    if not image:
        return False
    path = Path(image)
    if not path.is_file():
        warn(f"{where}: image not found: {image}")
        return False
    try:
        with Image.open(path) as im:
            iw, ih = im.size
    except Exception:
        warn(f"{where}: not a readable image: {image}")
        return False
    pic = slide.shapes.add_picture(str(path), x, y, w, h)
    frame, img = w / h, iw / ih
    if img > frame:  # too wide: crop the sides
        c = (1 - frame / img) / 2
        pic.crop_left, pic.crop_right = c, c
    elif img < frame:
        c = (1 - img / frame) / 2
        pic.crop_top, pic.crop_bottom = c, c
    credit = load_credits(image)
    if credit and credit_color:
        band = rect(slide, x, y + h - Inches(0.28), w, Inches(0.28), "000000", alpha=0.35)
        band.name = "credit-band"
        text(slide, x + Inches(0.1), y + h - Inches(0.25), w - Inches(0.2), Inches(0.22), credit[:160], 8, credit_color,
             align=PP_ALIGN.RIGHT)
    return True


# ------------------------------------------------------------------------------------------ deck


class Deck:
    def __init__(self, spec: dict):
        self.spec = spec
        theme = spec.get("theme") or "azure"
        if isinstance(theme, dict):
            base = THEMES.get(theme.get("base", "azure"), THEMES["azure"])
            self.t = {**base, **{k: str(v).lstrip("#") for k, v in theme.items() if k != "base"}}
        else:
            if theme not in THEMES:
                warn(f"unknown theme {theme!r}; using azure (choices: {', '.join(THEMES)})")
            self.t = THEMES.get(theme, THEMES["azure"])
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = W, H
        self.blank = self.prs.slide_layouts[6]
        self.footer = spec.get("footer", spec.get("title", ""))
        self.page = 0

    # ---- frame shared by content slides
    def new_slide(self, bg: str | None = None):
        slide = self.prs.slides.add_slide(self.blank)
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = rgb(bg or self.t["bg"])
        self.page += 1
        return slide

    def content_slide(self, title: str, where: str, subtitle: str = "", kicker: str = ""):
        """Title (optional kicker above, subtitle below), footer and page number. Sets self.top for the body."""
        t = self.t
        slide = self.new_slide()
        y = Inches(0.45)
        if kicker:
            text(slide, MARGIN, y, Inches(8), Inches(0.35), kicker, 13, t["primary"], bold=True)
            y += Inches(0.38)
        text(slide, MARGIN, y, W - 2 * MARGIN, Inches(0.8), title, 30, t["text"], bold=True, anchor=MSO_ANCHOR.MIDDLE, min_size=20,
             where=f"{where} title")
        y += Inches(0.85)
        if subtitle:
            text(slide, MARGIN, y, W - 2 * MARGIN, Inches(0.45), subtitle, 17, t["muted"], min_size=12, where=f"{where} subtitle")
            y += Inches(0.5)
        self.top = max(y + Inches(0.3), Inches(1.75))
        line_color = mix(t["bg"], t["text"], 0.12)
        rect(slide, MARGIN, H - Inches(0.55), W - 2 * MARGIN, Emu(9525), line_color)
        if self.footer:
            text(slide, MARGIN, H - Inches(0.45), Inches(8), Inches(0.3), self.footer, 11, t["muted"])
        text(slide, W - MARGIN - Inches(1), H - Inches(0.45), Inches(1), Inches(0.3), str(self.page), 11, t["muted"], align=PP_ALIGN.RIGHT)
        return slide

    BOTTOM = Inches(6.7)  # body content ends here (footer below)
    top = Inches(1.75)

    def frame(self, s: dict, where: str):
        return self.content_slide(s.get("title", ""), where, s.get("subtitle", ""), s.get("kicker", ""))

    def notes(self, slide, s: dict) -> None:
        if s.get("notes"):
            slide.notes_slide.notes_text_frame.text = str(s["notes"])

    # ---- layouts
    def cover(self, s: dict, where: str, closing: bool = False):
        t = self.t
        slide = self.new_slide(t["dark"])
        has_img = picture(slide, s.get("image"), int(W * 0.5), 0, W - int(W * 0.5), H, where)
        if not has_img:
            rect(slide, W - Inches(5.2), Inches(-1.6), Inches(7), Inches(7), t["primary"], shape=MSO_SHAPE.OVAL, alpha=0.35)
            rect(slide, W - Inches(3.0), Inches(4.2), Inches(4.4), Inches(4.4), t["primary"], shape=MSO_SHAPE.OVAL, alpha=0.22)
            if not closing:
                rect(slide, W - Inches(6.6), Inches(4.9), Inches(1.1), Inches(1.1), t["accent"], shape=MSO_SHAPE.OVAL)
        width = int(W * 0.5) - MARGIN - Inches(0.5) if has_img else Inches(8.6)
        top = Inches(2.2) if not closing else Inches(2.6)
        if s.get("kicker"):
            text(slide, MARGIN, top - Inches(0.6), width, Inches(0.4), s["kicker"], 15, t["accent"], bold=True)
        rect(slide, MARGIN, top, Inches(0.9), Inches(0.08), t["accent"])
        title = s.get("title", "")
        size = fit_size([title], width, Inches(2.0), 46 if not closing else 50, 28, spacing=1.1)
        if size <= 28 and text_height([title], width, 28, 1.1) > Inches(2.0):
            warn(f"{where} title: too long for a cover; shorten it")
        used = min(Inches(2.6), text_height([title], width, size, 1.1))
        text(slide, MARGIN, top + Inches(0.3), width, used + Inches(0.1), title, size, "FFFFFF", bold=True, spacing=1.1)
        if s.get("subtitle"):
            text(slide, MARGIN, top + Inches(0.55) + used, width, Inches(1.0), s["subtitle"], 20, mix("FFFFFF", t["dark"], 0.25), min_size=14,
                 where=f"{where} subtitle")
        meta = " · ".join(str(v) for v in (s.get("author"), s.get("date")) if v)
        if meta:
            text(slide, MARGIN, H - Inches(1.1), width, Inches(0.4), meta, 14, mix("FFFFFF", t["dark"], 0.35))
        self.notes(slide, s)

    def section(self, s: dict, where: str):
        t = self.t
        slide = self.new_slide(t["primary"] if not is_dark(t["bg"]) else t["dark"])
        if not picture(slide, s.get("image"), int(W * 0.62), 0, W - int(W * 0.62), H, where):
            rect(slide, W - Inches(4.5), Inches(-1), Inches(6), Inches(6), "FFFFFF", shape=MSO_SHAPE.OVAL, alpha=0.08)
            rect(slide, W - Inches(2.6), Inches(4.4), Inches(4), Inches(4), "FFFFFF", shape=MSO_SHAPE.OVAL, alpha=0.06)
        number = s.get("number") or ""
        if number:
            text(slide, MARGIN, Inches(1.6), Inches(4), Inches(1.6), str(number), 96, t["accent"], bold=True)
        text(slide, MARGIN, Inches(3.4), Inches(7.4), Inches(1.4), s.get("title", ""), 44, "FFFFFF", bold=True, min_size=26,
             where=f"{where} title")
        if s.get("subtitle"):
            text(slide, MARGIN, Inches(4.85), Inches(7.4), Inches(1.0), s["subtitle"], 20, mix("FFFFFF", t["primary"], 0.2), min_size=14,
                 where=f"{where} subtitle")
        self.notes(slide, s)

    def agenda(self, s: dict, where: str):
        """A coloured panel on the left with the heading, the numbered items on the right."""
        t = self.t
        slide = self.new_slide()
        pw = Inches(4.4)
        rect(slide, 0, 0, pw, H, t["dark"] if is_dark(t["bg"]) else t["primary"])
        rect(slide, Inches(-1.2), H - Inches(2.6), Inches(3.4), Inches(3.4), "FFFFFF", shape=MSO_SHAPE.OVAL, alpha=0.08)
        text(slide, MARGIN, Inches(2.4), pw - MARGIN - Inches(0.3), Inches(1.2), s.get("title", "目录"), 44, "FFFFFF", bold=True, min_size=28,
             where=f"{where} title")
        text(slide, MARGIN, Inches(3.55), pw - MARGIN - Inches(0.3), Inches(0.5), s.get("subtitle", "CONTENTS"), 16,
             mix("FFFFFF", t["primary"], 0.3), bold=True)
        items = [str(i) for i in s.get("items", [])][:7]
        n = len(items) or 1
        x0 = pw + Inches(0.9)
        row_h = min(Inches(1.0), (Inches(6.3) - Inches(1.0)) // n)
        y0 = (H - row_h * n) // 2
        for i, item in enumerate(items):
            y = y0 + i * row_h
            text(slide, x0, y, Inches(1.2), row_h, f"{i + 1:02d}", 34, t["primary"] if i % 2 == 0 else t["accent"], bold=True,
                 anchor=MSO_ANCHOR.MIDDLE)
            text(slide, x0 + Inches(1.3), y, W - x0 - Inches(1.3) - MARGIN, row_h, item, 24, t["text"], anchor=MSO_ANCHOR.MIDDLE, min_size=16,
                 where=f"{where} item {i + 1}")
            if i < n - 1:
                rect(slide, x0 + Inches(1.3), y + row_h - Emu(4762), W - x0 - Inches(1.3) - MARGIN, Emu(9525), mix(t["bg"], t["text"], 0.1))
        self.notes(slide, s)

    def bullets(self, s: dict, where: str):
        t = self.t
        slide = self.frame(s, where)
        items = s.get("bullets", [])[:7]
        top, bottom = self.top, self.BOTTOM
        right_w = Inches(5.0)
        has_side = bool(s.get("image")) or bool(s.get("icon")) or bool(s.get("highlight"))
        text_w = (W - 2 * MARGIN) - (right_w + Inches(0.6) if has_side else 0)
        if s.get("image"):
            picture(slide, s["image"], W - MARGIN - right_w, top, right_w, bottom - top, where)
        elif has_side:
            px = W - MARGIN - right_w
            rect(slide, px, top, right_w, bottom - top, t["primary"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.05)
            rect(slide, px + right_w - Inches(2.2), top + Inches(0.3), Inches(2.6), Inches(2.6), "FFFFFF", shape=MSO_SHAPE.OVAL, alpha=0.07)
            y = top + Inches(0.55)
            if s.get("icon"):
                icon(slide, s["icon"], px + Inches(0.55), y, Inches(1.0), t["primary"], bg="FFFFFF")
                y += Inches(1.35)
            hl = s.get("highlight") or {}
            if isinstance(hl, str):
                hl = {"text": hl}
            if hl.get("value"):
                text(slide, px + Inches(0.55), y, right_w - Inches(1.1), Inches(1.1), hl["value"], 54, "FFFFFF", bold=True, min_size=30,
                     where=f"{where} highlight")
                y += Inches(1.15)
            if hl.get("text"):
                text(slide, px + Inches(0.55), y, right_w - Inches(1.1), bottom - y - Inches(0.4), hl["text"], 19,
                     mix("FFFFFF", t["primary"], 0.12), spacing=1.35, min_size=13, where=f"{where} highlight")
        texts = [(b.get("text", "") if isinstance(b, dict) else str(b)) for b in items]
        size = fit_size(texts, text_w - Inches(0.1), bottom - top, 24, 15, spacing=1.3, gap=1.0)
        tb = slide.shapes.add_textbox(MARGIN, top, text_w, bottom - top)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE if len(items) <= 3 and not has_side else MSO_ANCHOR.TOP
        for m in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            setattr(tf, m, Emu(0))
        first = True
        for i, b in enumerate(items):
            lead = b.get("title") if isinstance(b, dict) else None
            body = texts[i]
            if not lead and "：" in body[:16]:
                lead, body = body.split("：", 1)
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            if not first:
                p.space_before = Pt(size * 0.9)
            first = False
            p.line_spacing = 1.3
            r = p.add_run()
            r.text = "■  "
            set_font(r, size * 0.6, t["primary"] if i % 2 == 0 else t["accent"])
            if lead:
                r = p.add_run()
                r.text = f"{lead}  "
                set_font(r, size, t["primary"], bold=True)
            r = p.add_run()
            r.text = body
            set_font(r, size, t["text"])
            for sub in (b.get("sub", []) if isinstance(b, dict) else [])[:3]:
                q = tf.add_paragraph()
                q.line_spacing = 1.25
                q.space_before = Pt(4)
                rr = q.add_run()
                rr.text = f"      – {sub}"
                set_font(rr, max(13, size - 4), t["muted"])
        if size <= 15:
            warn(f"{where}: bullets are long for one slide; keep each under ~40 characters or split the slide")
        self.notes(slide, s)

    def cards(self, s: dict, where: str):
        t = self.t
        slide = self.frame(s, where)
        cards = s.get("cards", [])[:6]
        n = len(cards) or 1
        cols = n if n <= 4 else 3
        rows = -(-n // cols)
        gap = Inches(0.35)
        cw = (W - 2 * MARGIN - gap * (cols - 1)) // cols
        avail = self.BOTTOM - self.top
        compact = rows > 1  # two rows: the icon sits beside the title instead of above it
        body_size = 17 if cols <= 3 else 15
        ix = Inches(0.6) if compact else Inches(0.95)
        inner = cw - Inches(0.7)

        def head_height(c) -> int:
            if compact:
                return Inches(0.4) + max(ix, Inches(0.55)) + Inches(0.2)
            return Inches(0.45) + ((ix + Inches(0.25)) if (c.get("icon") or c.get("value")) else 0) + Inches(0.62)

        def need(c) -> int:  # card height from its content, so cards never end in an empty bottom half
            body = c.get("text", "")
            return head_height(c) + (text_height([body], inner, body_size, 1.35) if body else 0) + Inches(0.35)

        row_room = (avail - gap * (rows - 1)) // rows
        ch = min(row_room, max(Inches(2.2) if not compact else Inches(1.6), max(need(c) for c in cards) if cards else 0))
        block = ch * rows + gap * (rows - 1)
        y0 = self.top + max(0, (avail - block) // 2)
        for i, c in enumerate(cards):
            r, k = divmod(i, cols)
            x, y = MARGIN + k * (cw + gap), y0 + r * (ch + gap)
            color = t["primary"] if i % 2 == 0 else t["accent"]
            rect(slide, x, y, cw, ch, t["soft"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.05)
            rect(slide, x + Inches(0.35), y, Inches(0.8), Inches(0.07), color)
            title_size = 22 if cols <= 3 else 20
            if compact:
                cy = y + Inches(0.4)
                tx = x + Inches(0.35)
                if c.get("icon"):
                    icon(slide, c["icon"], tx, cy, ix, "FFFFFF", bg=color, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
                    tx += ix + Inches(0.2)
                head = c.get("title", "") if not c.get("value") else f"{c['value']}  {c.get('title', '')}"
                text(slide, tx, cy, x + cw - Inches(0.3) - tx, ix, head, title_size - 2, t["text"], bold=True, anchor=MSO_ANCHOR.MIDDLE,
                     min_size=13, where=f"{where} card {i + 1} title")
            else:
                cy = y + Inches(0.45)
                if c.get("icon"):
                    icon(slide, c["icon"], x + Inches(0.35), cy, ix, "FFFFFF", bg=color, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
                    cy += ix + Inches(0.25)
                elif c.get("value"):
                    text(slide, x + Inches(0.35), cy, inner, ix, str(c["value"]), 40, color, bold=True, min_size=24, anchor=MSO_ANCHOR.MIDDLE)
                    cy += ix + Inches(0.25)
                text(slide, x + Inches(0.35), cy, inner, Inches(0.55), c.get("title", ""), title_size, t["text"], bold=True, min_size=15,
                     where=f"{where} card {i + 1} title")
            cy = y + head_height(c)
            if c.get("text"):
                room = y + ch - cy - Inches(0.2)
                if room < Inches(0.35):
                    warn(f"{where}: too many cards with text for one slide; use at most 4, or shorten the texts")
                    room = Inches(0.35)
                text(slide, x + Inches(0.35), cy, inner, room, c["text"], body_size, t["muted"], spacing=1.35, min_size=11,
                     where=f"{where} card {i + 1}")
        self.notes(slide, s)

    def two_column(self, s: dict, where: str):
        t = self.t
        slide = self.frame(s, where)
        gap = Inches(1.0) if s.get("vs") else Inches(0.45)
        cw = (W - 2 * MARGIN - gap) // 2
        top, bottom = self.top, self.BOTTOM
        for k, side in enumerate(("left", "right")):
            col = s.get(side) or {}
            x = MARGIN + k * (cw + gap)
            color = t["primary"] if k == 0 else t["accent"]
            rect(slide, x, top, cw, bottom - top, t["soft"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
            rect(slide, x, top, cw, Inches(0.09), color)
            hx = x + Inches(0.4)
            if col.get("icon"):
                icon(slide, col["icon"], hx, top + Inches(0.4), Inches(0.7), "FFFFFF", bg=color, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
                hx += Inches(0.9)
            text(slide, hx, top + Inches(0.4), cw - (hx - x) - Inches(0.3), Inches(0.7), col.get("heading", ""), 24, color, bold=True,
                 anchor=MSO_ANCHOR.MIDDLE, min_size=16, where=f"{where} {side} heading")
            items = [str(b) for b in col.get("bullets", [])][:6]
            if items:
                text(slide, x + Inches(0.4), top + Inches(1.4), cw - Inches(0.8), bottom - top - Inches(1.6), ["•  " + b for b in items], 20,
                     t["text"], spacing=1.3, gap_pt=12, min_size=13, where=f"{where} {side}")
        if s.get("vs"):
            cx = MARGIN + cw + gap // 2 - Inches(0.42)
            cy = (top + bottom) // 2 - Inches(0.42)
            rect(slide, cx, cy, Inches(0.84), Inches(0.84), t["dark"], shape=MSO_SHAPE.OVAL)
            text(slide, cx, cy, Inches(0.84), Inches(0.84), "VS", 18, "FFFFFF", bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        self.notes(slide, s)

    def stats(self, s: dict, where: str):
        t = self.t
        slide = self.frame(s, where)
        items = s.get("stats", [])[:4]
        n = len(items) or 1
        gap = Inches(0.35)
        has_text = bool(s.get("text"))
        ch = Inches(3.3) if any(st.get("icon") for st in items) else Inches(2.9)
        avail = self.BOTTOM - self.top
        block = ch + (Inches(1.4) if has_text else 0)
        top = self.top + max(0, (avail - block) // 2)
        cw = (W - 2 * MARGIN - gap * (n - 1)) // n
        for i, st in enumerate(items):
            x = MARGIN + i * (cw + gap)
            dark = i == 0
            rect(slide, x, top, cw, ch, t["primary"] if dark else t["soft"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
            fg = "FFFFFF" if dark else t["primary"]
            y = top + Inches(0.3)
            if st.get("icon"):  # above the figure, so the figure gets the card's full width
                icon(slide, st["icon"], x + Inches(0.4), y, Inches(0.5), fg)
                y += Inches(0.5)
            vw = cw - Inches(0.8)
            value = str(st.get("value", ""))
            vsize = 60 if n <= 3 else 48
            while vsize > 24 and line_count(value, vw, vsize) > 1:  # a figure must not wrap ("86" / "%")
                vsize -= 2
            text(slide, x + Inches(0.4), y, vw, Inches(1.1), value, vsize, fg, bold=True, anchor=MSO_ANCHOR.MIDDLE)
            y -= Inches(0.15)
            text(slide, x + Inches(0.4), y + Inches(1.3), cw - Inches(0.8), Inches(0.5), st.get("label", ""), 20,
                 "FFFFFF" if dark else t["text"], bold=True, min_size=13, where=f"{where} stat {i + 1} label")
            if st.get("note"):
                text(slide, x + Inches(0.4), y + Inches(1.85), cw - Inches(0.8), top + ch - y - Inches(1.95), st["note"], 15,
                     mix("FFFFFF", t["primary"], 0.2) if dark else t["muted"], spacing=1.3, min_size=11, where=f"{where} stat {i + 1} note")
        if has_text:
            text(slide, MARGIN, top + ch + Inches(0.4), W - 2 * MARGIN, Inches(1.0), s["text"], 19, t["text"],
                 spacing=1.35, min_size=13, where=f"{where} text")
        self.notes(slide, s)

    def big_number(self, s: dict, where: str):
        """One figure that carries the slide, with what it means beside it."""
        t = self.t
        slide = self.frame(s, where)
        top, bottom = self.top, self.BOTTOM
        lw = Inches(5.6)
        text(slide, MARGIN, top + Inches(0.3), lw, Inches(2.0), str(s.get("value", "")), 110, t["primary"], bold=True, min_size=54,
             anchor=MSO_ANCHOR.MIDDLE, where=f"{where} value")
        rect(slide, MARGIN, top + Inches(2.5), Inches(0.9), Inches(0.08), t["accent"])
        text(slide, MARGIN, top + Inches(2.75), lw, Inches(1.5), s.get("label", ""), 22, t["text"], bold=True, spacing=1.3, min_size=15,
             where=f"{where} label")
        x = MARGIN + lw + Inches(0.6)
        items = [str(b) for b in s.get("bullets", [])][:5]
        if items or s.get("text"):
            rect(slide, x, top, W - MARGIN - x, bottom - top, t["soft"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
            content = ([s["text"]] if s.get("text") else []) + ["•  " + b for b in items]
            text(slide, x + Inches(0.5), top + Inches(0.5), W - MARGIN - x - Inches(1.0), bottom - top - Inches(1.0), content, 20, t["text"],
                 spacing=1.35, gap_pt=12, min_size=13, anchor=MSO_ANCHOR.MIDDLE, where=f"{where} text")
        self.notes(slide, s)

    def chart(self, s: dict, where: str):
        t = self.t
        slide = self.frame(s, where)
        spec = s.get("chart") or {}
        kind = {
            "bar": XL_CHART_TYPE.BAR_CLUSTERED, "column": XL_CHART_TYPE.COLUMN_CLUSTERED, "stacked": XL_CHART_TYPE.COLUMN_STACKED,
            "line": XL_CHART_TYPE.LINE_MARKERS, "pie": XL_CHART_TYPE.PIE, "doughnut": XL_CHART_TYPE.DOUGHNUT, "area": XL_CHART_TYPE.AREA,
        }.get(spec.get("type", "column"), XL_CHART_TYPE.COLUMN_CLUSTERED)
        data = CategoryChartData()
        cats = [str(c) for c in spec.get("categories", [])]
        data.categories = cats
        series = spec.get("series", [])
        for se in series:
            vals = [float(v) if v is not None else None for v in se.get("values", [])]
            if len(vals) != len(cats):
                warn(f"{where}: series {se.get('name')!r} has {len(vals)} values for {len(cats)} categories")
            data.add_series(str(se.get("name", "")), vals, number_format=spec.get("number_format", "General"))
        side = s.get("bullets") or s.get("insight")
        cw = (W - 2 * MARGIN) - (Inches(4.3) if side else 0)
        frame = slide.shapes.add_chart(kind, MARGIN, self.top, cw, self.BOTTOM - self.top, data)
        ch = frame.chart
        ch.font.size = Pt(14)
        ch.font.name = FONT
        ch.font.color.rgb = rgb(t["muted"])
        colors = [c.strip() for c in t["chart"].split(",")]
        pie = kind in (XL_CHART_TYPE.PIE, XL_CHART_TYPE.DOUGHNUT)
        ch.has_legend = pie or len(series) > 1
        if ch.has_legend:
            ch.legend.position = XL_LEGEND_POSITION.BOTTOM
            ch.legend.include_in_layout = False
            ch.legend.font.size = Pt(12)
        plot = ch.plots[0]
        if pie:
            for i, point in enumerate(plot.series[0].points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = rgb(colors[i % len(colors)])
            plot.has_data_labels = True
            plot.data_labels.number_format = spec.get("label_format", "0%") if spec.get("percent", True) else "General"
            plot.data_labels.number_format_is_linked = False
            plot.data_labels.show_percentage = spec.get("percent", True)
            plot.data_labels.show_value = not spec.get("percent", True)
            plot.data_labels.font.size = Pt(12)
            plot.data_labels.font.color.rgb = rgb("FFFFFF")
        else:
            for i, se in enumerate(plot.series):
                c = rgb(colors[i % len(colors)])
                if kind == XL_CHART_TYPE.LINE_MARKERS:
                    se.format.line.color.rgb = c
                    se.format.line.width = Pt(2.75)
                    se.smooth = False
                    se.marker.format.fill.solid()
                    se.marker.format.fill.fore_color.rgb = c
                    se.marker.format.line.color.rgb = c
                else:
                    se.format.fill.solid()
                    se.format.fill.fore_color.rgb = c
            if hasattr(plot, "gap_width"):
                plot.gap_width = 70
            if spec.get("labels", len(series) == 1):
                plot.has_data_labels = True
                plot.data_labels.font.size = Pt(13)
                plot.data_labels.font.color.rgb = rgb(t["text"])
                plot.data_labels.number_format = spec.get("number_format", "General")
                plot.data_labels.number_format_is_linked = False
                if kind in (XL_CHART_TYPE.COLUMN_CLUSTERED, XL_CHART_TYPE.BAR_CLUSTERED):
                    plot.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
            va = ch.value_axis
            va.has_major_gridlines = True
            va.major_gridlines.format.line.color.rgb = rgb(mix(t["bg"], t["text"], 0.12))
            va.format.line.fill.background()
            va.tick_labels.font.size = Pt(12)
            ca = ch.category_axis
            ca.format.line.color.rgb = rgb(mix(t["bg"], t["text"], 0.25))
            ca.tick_labels.font.size = Pt(14)
            if spec.get("y_title"):
                va.has_title = True
                va.axis_title.text_frame.text = spec["y_title"]
        if side:
            x = W - MARGIN - Inches(3.9)
            rect(slide, x, self.top, Inches(3.9), self.BOTTOM - self.top, t["soft"], shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
            y = self.top + Inches(0.35)
            text(slide, x + Inches(0.35), y, Inches(3.2), Inches(0.45), s.get("insight_title", "要点"), 20, t["primary"], bold=True)
            items = side if isinstance(side, list) else [side]
            text(slide, x + Inches(0.35), y + Inches(0.6), Inches(3.2), self.BOTTOM - y - Inches(0.9), ["•  " + str(i) for i in items],
                 18, t["text"], spacing=1.35, gap_pt=10, min_size=12, where=f"{where} insight")
        self.notes(slide, s)

    def timeline(self, s: dict, where: str):
        t = self.t
        slide = self.frame(s, where)
        steps = s.get("steps", [])[:6]
        n = len(steps) or 1
        span = W - 2 * MARGIN
        seg = span // n
        has_text = any(st.get("text") for st in steps)
        line_y = self.top + Inches(1.35) + (0 if has_text else Inches(0.6))
        rect(slide, MARGIN + seg // 2, line_y - Emu(19050), span - seg, Emu(38100), mix(t["bg"], t["text"], 0.18))
        for i, st in enumerate(steps):
            cx = MARGIN + i * seg + seg // 2
            color = t["primary"] if i % 2 == 0 else t["accent"]
            if st.get("label"):
                text(slide, cx - seg // 2 + Inches(0.1), line_y - Inches(1.2), seg - Inches(0.2), Inches(0.6), str(st["label"]), 24, color,
                     bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.BOTTOM, min_size=14, where=f"{where} step {i + 1} label")
            d = Inches(0.8)
            rect(slide, cx - d // 2, line_y - d // 2, d, d, color, shape=MSO_SHAPE.OVAL)
            if st.get("icon"):
                icon(slide, st["icon"], cx - Inches(0.22), line_y - Inches(0.22), Inches(0.44), "FFFFFF")
            else:
                text(slide, cx - d // 2, line_y - d // 2, d, d, str(i + 1), 20, "FFFFFF", bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
            cy = line_y + Inches(0.65)
            if has_text:
                rect(slide, cx - seg // 2 + Inches(0.12), cy, seg - Inches(0.24), self.BOTTOM - cy, t["soft"], shape=MSO_SHAPE.ROUNDED_RECTANGLE,
                     radius=0.06)
            text(slide, cx - seg // 2 + Inches(0.3), cy + Inches(0.25), seg - Inches(0.6), Inches(0.6), st.get("title", ""), 21, t["text"],
                 bold=True, align=PP_ALIGN.CENTER, min_size=14, where=f"{where} step {i + 1} title")
            if st.get("text"):
                text(slide, cx - seg // 2 + Inches(0.3), cy + Inches(0.9), seg - Inches(0.6), self.BOTTOM - cy - Inches(1.1), st["text"], 16,
                     t["muted"], align=PP_ALIGN.CENTER, spacing=1.35, min_size=11, where=f"{where} step {i + 1}")
        self.notes(slide, s)

    def table(self, s: dict, where: str):
        t = self.t
        slide = self.frame(s, where)
        cols = [str(c) for c in s.get("columns", [])]
        rows = [[("" if v is None else str(v)) for v in r] for r in s.get("rows", [])][:12]
        if not cols:
            warn(f"{where}: table without columns")
            return
        nr = len(rows) + 1
        avail = self.BOTTOM - self.top
        total_w = W - 2 * MARGIN
        pad = Inches(0.1)  # cell margin on each side
        grid = [cols] + [[(r[j] if j < len(r) else "") for j in range(len(cols))] for r in rows]
        # column widths follow the content (the longest cell, capped), at least a fifth of an even share
        demand = [min(18.0, max(text_units(row[j]) for row in grid)) + 1.5 for j in range(len(cols))]
        floor = 0.2 / len(cols)
        shares = [max(floor, d / sum(demand)) for d in demand]
        widths = [int(total_w * sh / sum(shares)) for sh in shares]

        def heights(size: float) -> list[int]:
            return [max(int(size * 1.2 * 1.25 * 12700) + 2 * Inches(0.06),
                        max(text_height([row[j]], widths[j] - 2 * pad, size, 1.0) for j in range(len(cols))) + 2 * Inches(0.06))
                    for row in grid]

        size = 16 if nr <= 6 else 14 if nr <= 9 else 12
        while sum(heights(size)) > avail and size > 10:
            size -= 1
        row_h = heights(size)
        if sum(row_h) > avail:
            warn(f"{where}: the table is too long for one slide; shorten the cells or split it")
        shape = slide.shapes.add_table(nr, len(cols), MARGIN, self.top, total_w, sum(row_h))
        tbl = shape.table
        tbl.first_row = True
        for j, w in enumerate(widths):
            tbl.columns[j].width = w
        for i, h in enumerate(row_h):
            tbl.rows[i].height = h
        for j, c in enumerate(cols):
            cell = tbl.cell(0, j)
            cell.fill.solid()
            cell.fill.fore_color.rgb = rgb(t["primary"])
            cell.text = c
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = cell.margin_right = pad
            set_font(cell.text_frame.paragraphs[0].runs[0], size, "FFFFFF", bold=True)

        def numeric(v: str) -> bool:
            return v.replace(",", "").replace("%", "").replace(".", "", 1).lstrip("+-").strip().isdigit()

        for j in range(len(cols)):
            if rows and all(numeric(r[j]) for r in rows if j < len(r) and r[j]):
                tbl.cell(0, j).text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT
        for i, r in enumerate(rows, start=1):
            for j in range(len(cols)):
                cell = tbl.cell(i, j)
                cell.fill.solid()
                cell.fill.fore_color.rgb = rgb(t["soft"] if i % 2 == 0 else t["bg"])
                cell.text = r[j] if j < len(r) else ""
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.margin_left = cell.margin_right = pad
                cell.text_frame.word_wrap = True
                para = cell.text_frame.paragraphs[0]
                para.line_spacing = 1.0
                if para.runs:
                    set_font(para.runs[0], size, t["text"], bold=(j == 0 and s.get("first_col_bold", True)))
                para.alignment = PP_ALIGN.RIGHT if numeric(cell.text) else PP_ALIGN.LEFT
        if len(s.get("rows", [])) > 12:
            warn(f"{where}: only the first 12 table rows fit; summarise or split the table")
        self.notes(slide, s)

    def quote(self, s: dict, where: str):
        t = self.t
        slide = self.new_slide(t["soft"])
        text(slide, MARGIN + Inches(0.4), Inches(0.9), Inches(2), Inches(2), "“", 160, t["primary"], bold=True)
        text(slide, Inches(1.9), Inches(2.4), W - Inches(3.8), Inches(2.6), s.get("quote", ""), 30, t["text"], bold=True, spacing=1.3,
             min_size=18, where=f"{where} quote")
        if s.get("author"):
            rect(slide, Inches(1.9), Inches(5.35), Inches(0.6), Inches(0.06), t["accent"])
            text(slide, Inches(1.9), Inches(5.55), W - Inches(3.8), Inches(0.5), s["author"], 18, t["muted"])
        self.notes(slide, s)

    def image(self, s: dict, where: str):
        t = self.t
        slide = self.new_slide(t["dark"])
        if not picture(slide, s.get("image"), 0, 0, W, H, where):
            rect(slide, 0, 0, W, H, t["primary"])
        band_h = Inches(1.9)
        rect(slide, 0, H - band_h, W, band_h, t["dark"], alpha=0.72)
        text(slide, MARGIN, H - band_h + Inches(0.3), W - 2 * MARGIN, Inches(0.8), s.get("title", ""), 32, "FFFFFF", bold=True, min_size=20,
             where=f"{where} title")
        if s.get("caption"):
            text(slide, MARGIN, H - band_h + Inches(1.05), W - 2 * MARGIN, Inches(0.6), s["caption"], 16, mix("FFFFFF", t["dark"], 0.25),
                 min_size=11, where=f"{where} caption")
        self.notes(slide, s)

    LAYOUTS = {
        "cover": "cover", "title": "cover", "section": "section", "agenda": "agenda", "toc": "agenda", "bullets": "bullets",
        "content": "bullets", "cards": "cards", "features": "cards", "two_column": "two_column", "compare": "two_column",
        "stats": "stats", "numbers": "stats", "big_number": "big_number", "statement": "big_number", "chart": "chart",
        "timeline": "timeline", "process": "timeline", "table": "table",
        "quote": "quote", "image": "image", "closing": "closing", "end": "closing",
    }

    def build(self, out: str) -> None:
        for i, s in enumerate(self.spec.get("slides", []), start=1):
            layout = self.LAYOUTS.get(str(s.get("layout", "bullets")))
            where = f"slide {i} ({s.get('layout', 'bullets')})"
            if layout is None:
                warn(f"{where}: unknown layout; using bullets (choices: {', '.join(sorted(set(self.LAYOUTS.values())))})")
                layout = "bullets"
            if layout == "closing":
                self.cover(s, where, closing=True)
            else:
                getattr(self, layout)(s, where)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(out)


EXAMPLE = {
    "theme": "azure",
    "title": "AILab 平台介绍",
    "footer": "AILab · 内部分享",
    "slides": [
        {"layout": "cover", "kicker": "2026 年度规划", "title": "AILab 智能体平台", "subtitle": "让每个人都能在安全沙箱里用好大模型",
         "author": "平台组", "date": "2026-10", "image": "/workspace/assets/images/cover_1.jpg"},
        {"layout": "agenda", "items": ["背景与目标", "平台架构", "关键能力", "落地计划"]},
        {"layout": "section", "number": "01", "title": "背景与目标", "subtitle": "为什么要建统一的 AI 平台"},
        {"layout": "bullets", "title": "我们面临的问题", "icon": "target",
         "bullets": ["工具分散：各团队自建脚本，重复投入", "安全风险：代码与数据缺乏隔离", "成本不可控：模型调用无计量"],
         "highlight": {"value": "60%", "text": "的重复建设可以通过统一平台消除"}},
        {"layout": "cards", "title": "四大核心能力", "cards": [
            {"icon": "shield-check", "title": "安全沙箱", "text": "gVisor 隔离，默认无网络"},
            {"icon": "brain", "title": "统一模型", "text": "按需切换，平台统一计量"},
            {"icon": "layers", "title": "办公技能", "text": "PPT、Excel、文档一键生成"},
            {"icon": "users", "title": "多人协作", "text": "资料库共享与版本管理"}]},
        {"layout": "stats", "title": "试点成效", "stats": [
            {"value": "3.2×", "label": "交付效率", "note": "材料制作时间缩短"},
            {"value": "98%", "label": "任务成功率"}, {"value": "0", "label": "安全事件"}]},
        {"layout": "chart", "title": "月度调用量", "chart": {"type": "column", "categories": ["7月", "8月", "9月", "10月"],
         "series": [{"name": "调用次数（千）", "values": [12, 18, 27, 41]}]}, "bullets": ["环比增长 52%", "办公类任务占 64%"]},
        {"layout": "timeline", "title": "落地计划", "steps": [
            {"label": "Q1", "title": "试点", "text": "两个部门上线"}, {"label": "Q2", "title": "推广", "text": "全代表处开放"},
            {"label": "Q3", "title": "深化", "text": "接入本地模型"}, {"label": "Q4", "title": "运营", "text": "建立技能市场"}]},
        {"layout": "closing", "title": "谢谢", "subtitle": "欢迎交流与试用"},
    ],
}


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--themes":
        for name, t in THEMES.items():
            print(f"{name:10s} primary #{t['primary']}  accent #{t['accent']}  {'dark' if is_dark(t['bg']) else 'light'} content slides")
        return 0
    if len(argv) == 2 and argv[1] == "--icons":
        print(" ".join(sorted(p.stem for p in ICON_DIR.glob("*.png"))))
        return 0
    if len(argv) == 2 and argv[1] == "--example":
        print(json.dumps(EXAMPLE, ensure_ascii=False, indent=2))
        return 0
    if len(argv) != 3:
        print(__doc__)
        return 2
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    Deck(spec).build(argv[2])
    for w in WARNINGS:
        print(f"WARN {w}")
    print(f"OK {argv[2]} ({len(spec.get('slides', []))} slides, theme {spec.get('theme', 'azure')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
