#!/usr/bin/env python3
"""Compose canvas-proxy.html from its template and binary assets.

The page is pasted into a Canvas iframe, so it cannot reference external files.
Both the project logo and the animated banner from the README are embedded as
data URIs, which keeps the shipped page a single self-contained file.

    python3 scripts/build_page.py
"""

import base64
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "canvas-proxy.template.html"
LOGO = ROOT / "assets" / "logo.svg"
BANNER = ROOT / "assets" / "banner.svg"
OUT = ROOT / "canvas-proxy.html"


def inline_logo() -> str:
    """The logo as markup, with ids namespaced so its defs cannot collide."""
    svg = LOGO.read_text()
    svg = re.sub(r"<\?xml[^>]*\?>", "", svg).strip()
    svg = svg.replace(' width="1932" height="1932"', ' width="100%" height="100%"')
    svg = svg.replace(' xmlns:c2pa="http://c2pa.org/manifest"', "")
    for ident in set(re.findall(r'id="([^"]+)"', svg)):
        svg = svg.replace(f'id="{ident}"', f'id="lg-{ident}"')
        svg = svg.replace(f"url(#{ident})", f"url(#lg-{ident})")
        svg = svg.replace(f'href="#{ident}"', f'href="#lg-{ident}"')
    return svg


def inline_banner() -> str:
    """The README banner as a data URI.

    Kept as an <img> rather than inlined markup: the SVG carries its own
    @font-face and SMIL timeline, and an image context keeps both isolated from
    the page's styles.
    """
    payload = base64.b64encode(BANNER.read_bytes()).decode("ascii")
    return "data:image/svg+xml;base64," + payload


def main() -> None:
    html = TEMPLATE.read_text()
    html = html.replace("<!--LOGO-->", inline_logo())
    html = html.replace("<!--BANNER-->", inline_banner())
    OUT.write_text(html)
    print("wrote %s (%d bytes)" % (OUT.name, len(html)))


if __name__ == "__main__":
    main()
