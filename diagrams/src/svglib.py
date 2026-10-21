"""极简 SVG 绘图库：统一 AILab 架构图风格。

用法：
    c = Canvas(1400, 800, "标题", "副标题")
    c.box(40, 80, 200, 80, "标题", ["第一行", "第二行"], style="own")
    c.arrow([(240, 120), (400, 120)], color="blue", label="LLM 调用")
    c.save("out.svg")
"""
from html import escape

FONT = "'PingFang SC','Microsoft YaHei','Noto Sans CJK SC','Source Han Sans SC',sans-serif"

# 填充色, 边框色
STYLES = {
    "own":   ("#eef2ff", "#4f46e5"),   # 自建
    "oss":   ("#ecfeff", "#0891b2"),   # 开源组件
    "npu":   ("#fff7ed", "#ea580c"),   # 智算
    "data":  ("#f5f3ff", "#7c3aed"),   # 数据
    "sto":   ("#f0fdf4", "#16a34a"),   # 存储
    "ops":   ("#f1f5f9", "#64748b"),   # 运维
    "usr":   ("#ffffff", "#334155"),   # 用户/外部
    "warn":  ("#fef2f2", "#dc2626"),   # 风险/禁止
    "ok":    ("#f0fdf4", "#16a34a"),
    "note":  ("#fefce8", "#ca8a04"),   # 说明
    "plain": ("#ffffff", "#94a3b8"),
    "gray":  ("#f8fafc", "#cbd5e1"),
    "sbx":   ("#ecfeff", "#0891b2"),
    "pink":  ("#fdf2f8", "#db2777"),
}

COLORS = {
    "gray": "#475569", "blue": "#2563eb", "orange": "#c2410c", "green": "#15803d",
    "red": "#dc2626", "purple": "#7c3aed", "teal": "#0e7490",
}


def tw(s, size):
    """估算文本宽度（CJK 按 1 字宽，ASCII 按 0.56 字宽）。"""
    w = 0.0
    for ch in s:
        w += size * (1.0 if ord(ch) > 0x2E80 else 0.6)
    return w


class Canvas:
    def __init__(self, w, h, title=None, subtitle=None, legend=None):
        self.w, self.h = w, h
        self.els = []
        self.markers = set()
        if title:
            self.text(30, 42, title, 24, weight=700, color="#0f172a")
            if subtitle:
                self.text(30 + tw(title, 24) + 20, 42, subtitle, 13.5, color="#64748b")
        if legend:
            self.legend(legend, w - 30)

    # ---------- primitives ----------
    def raw(self, s):
        self.els.append(s)

    def text(self, x, y, s, size=13, weight=400, color="#1e293b", anchor="start", italic=False):
        st = ' font-style="italic"' if italic else ""
        stripped = s.lstrip(" ")
        s = " " * (len(s) - len(stripped)) + stripped  # SVG 会折叠前导空格
        self.els.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" fill="{color}" '
            f'text-anchor="{anchor}" style="white-space:pre"{st}>{escape(s)}</text>')

    def rect(self, x, y, w, h, fill="#fff", stroke="#94a3b8", sw=1.5, rx=8, dash=None, opacity=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        o = f' fill-opacity="{opacity}"' if opacity is not None else ""
        self.els.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" ry="{rx}" '
                        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}{o}/>')

    def circle(self, cx, cy, r, fill="#fff", stroke="#475569", sw=1.5):
        self.els.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def line(self, x1, y1, x2, y2, color="#94a3b8", sw=1, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.els.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{sw}"{d}/>')

    # ---------- components ----------
    def band(self, x, y, w, h, label=None, style="gray", label_size=15, sub=None, label_pos="tl"):
        fill, stroke = STYLES[style]
        self.rect(x, y, w, h, fill=fill, stroke=stroke, sw=1, rx=10)
        if label:
            if label_pos == "tl":
                self.text(x + 14, y + 24, label, label_size, weight=700, color="#334155")
                if sub:
                    self.text(x + 14 + tw(label, label_size) + 12, y + 24, sub, 12, color="#64748b")
            elif label_pos == "left":
                self.text(x + 14, y + h / 2 + 5, label, label_size, weight=700, color="#334155")

    def box(self, x, y, w, h, title=None, lines=(), style="oss", title_size=15, line_size=12.5,
            align="center", dash=None, title_color="#0f172a", line_color="#334155", sw=1.5, badge=None):
        fill, stroke = STYLES[style]
        self.rect(x, y, w, h, fill=fill, stroke=stroke, sw=sw, dash=dash)
        n = (1 if title else 0) + len(lines)
        total = (title_size + 6 if title else 0) + len(lines) * (line_size + 6.5)
        cy = y + (h - total) / 2 + title_size * 0.85 if title else y + (h - total) / 2 + line_size
        if align == "center":
            ax, anchor = x + w / 2, "middle"
        else:
            ax, anchor = x + 12, "start"
        if title:
            self.text(ax, cy, title, title_size, weight=700, color=title_color, anchor=anchor)
            cy += title_size * 0.15 + line_size + 8
        for ln in lines:
            col = line_color
            weight = 400
            if isinstance(ln, tuple):
                ln, col = ln[0], ln[1]
            self.text(ax, cy, ln, line_size, color=col, anchor=anchor, weight=weight)
            cy += line_size + 6.5
        if badge:
            bw = tw(badge, 11) + 12
            self.rect(x + w - bw - 6, y - 9, bw, 18, fill=stroke, stroke=stroke, rx=9)
            self.text(x + w - bw / 2 - 6, y + 4, badge, 11, weight=700, color="#ffffff", anchor="middle")

    def pill(self, x, y, s, style="oss", size=12, h=22):
        fill, stroke = STYLES[style]
        w = tw(s, size) + 18
        self.rect(x, y, w, h, fill=fill, stroke=stroke, sw=1.2, rx=h / 2)
        self.text(x + w / 2, y + h / 2 + size * 0.36, s, size, anchor="middle", color="#1e293b")
        return w

    def num(self, x, y, n, color="#2563eb", r=11):
        self.circle(x, y, r, fill=color, stroke=color)
        self.text(x, y + 4.5, str(n), 12.5, weight=700, color="#ffffff", anchor="middle")

    def _marker(self, color):
        mid = "m" + color.strip("#")
        if mid not in self.markers:
            self.markers.add(mid)
        return mid

    def arrow(self, pts, color="gray", dash=False, label=None, lpos=None, lanchor="middle",
              sw=1.6, head="end", lsize=11.5, lbold=False, lbg=True):
        col = COLORS.get(color, color)
        mid = self._marker(col)
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        da = ' stroke-dasharray="6 4"' if dash is True else (f' stroke-dasharray="{dash}"' if dash else "")
        ms = f' marker-end="url(#{mid})"' if head in ("end", "both") else ""
        if head == "both":
            ms += f' marker-start="url(#{mid})"'
        self.els.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="{sw}"{da}{ms}/>')
        if label:
            if lpos is None:
                (x1, y1), (x2, y2) = pts[len(pts) // 2 - 1], pts[len(pts) // 2]
                lpos = ((x1 + x2) / 2, (y1 + y2) / 2 - 5)
            if lbg:
                lw = tw(label, lsize) + 8
                lx = lpos[0] - (lw / 2 if lanchor == "middle" else (lw - 4 if lanchor == "end" else 4))
                self.rect(lx, lpos[1] - lsize + 1, lw, lsize + 5, fill="#ffffff", stroke="none", sw=0, rx=3,
                          opacity=0.9)
            self.text(lpos[0], lpos[1], label, lsize, weight=700 if lbold else 400, color=col, anchor=lanchor)

    def legend(self, items, right_x, y=24):
        """items: [(style, 文本)]，右对齐放在标题行。"""
        widths = [tw(t, 11.5) + 30 for _, t in items]
        x = right_x - sum(widths)
        for (st, t), wdt in zip(items, widths):
            fill, stroke = STYLES[st]
            self.rect(x, y, 16, 12, fill=fill, stroke=stroke, sw=1.5, rx=2)
            self.text(x + 22, y + 11, t, 11.5, color="#475569")
            x += wdt

    def bullets(self, x, y, items, size=13, gap=21, color="#1e293b", bullet="•"):
        for i, it in enumerate(items):
            col = color
            if isinstance(it, tuple):
                it, col = it
            self.text(x, y + i * gap, f"{bullet} {it}" if bullet else it, size, color=col)

    # ---------- output ----------
    def svg(self):
        defs = []
        for mid in sorted(self.markers):
            col = "#" + mid[1:]
            defs.append(f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
                        f'fill="{col}"/></marker>')
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" font-family="{FONT}">\n<defs>{"".join(defs)}</defs>\n'
                f'<rect width="{self.w}" height="{self.h}" fill="#ffffff"/>\n')
        return head + "\n".join(self.els) + "\n</svg>\n"

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.svg())
