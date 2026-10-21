"""把 ../svg/lite-*.svg 渲染成 2x PNG 放到 ../png/（用 Playwright 自带的无头 Chromium）。

用法：python3 -I render.py [名称前缀...]
"""
import glob
import os
import re
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SVG_DIR = os.path.normpath(os.path.join(HERE, "..", "svg"))
PNG_DIR = os.path.normpath(os.path.join(HERE, "..", "png"))


def main(prefixes):
    os.makedirs(PNG_DIR, exist_ok=True)
    svgs = [s for s in sorted(glob.glob(os.path.join(SVG_DIR, "lite-*.svg")))
            if not prefixes or any(os.path.basename(s).startswith(p) for p in prefixes)]
    with sync_playwright() as pw:
        # CHROMIUM=/path/to/chrome-headless-shell when Playwright's own browser build is not the installed one
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
        for svg in svgs:
            head = open(svg, encoding="utf-8").read(400)
            w, h = (int(v) for v in re.search(r'width="(\d+)" height="(\d+)"', head).groups())
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
            page.goto("file://" + svg)
            png = os.path.join(PNG_DIR, os.path.basename(svg)[:-4] + ".png")
            page.screenshot(path=png, full_page=False)
            page.close()
            print("rendered", png)
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
