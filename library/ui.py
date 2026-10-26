"""Shared bits of the reading UI: request guard, cover fallback, scroller script.

These used to live in the store module. The store (a catalogue of Chinese ebooks built on
Douban and Chinese literary prizes) was dropped when the project went international; this
module keeps the handful of pieces the reader itself needs.
"""
from __future__ import annotations

import html
import re

from fastapi import HTTPException, Request

from .present import short       # noqa: F401  (re-exported: pages import it from here)
from .web import hue

e = html.escape
CJK = re.compile(r"[　-鿿＀-￯]")
PAGE_W = 1400                    # original-page render width


def same_origin(req: Request) -> None:
    """Only accept requests from this site's own pages: any web page can send a "simple
    request" to 127.0.0.1, so require JSON and a matching origin."""
    origin = req.headers.get("origin")
    host = req.headers.get("host", "")
    if "application/json" not in req.headers.get("content-type", "") or \
            (origin and origin.split("://", 1)[-1] != host):
        raise HTTPException(403, "same-origin requests only")


def book_names(b, w=None) -> dict:
    """A book from the reading library → the names shown on the shelf and the book page."""
    return {"title": b["title"] or "", "orig": "",
            "author": b["author"] or "", "how": ""}


def wrap_text(s: str, width: int, lines: int) -> list[str]:
    if CJK.search(s):
        out = [s[i:i + width // 2] for i in range(0, len(s), width // 2)]
    else:
        out, cur = [], ""
        for word in s.split():
            if cur and len(cur) + 1 + len(word) > width:
                out.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][: max(1, len(out[-1]) - 1)] + "…"
    return out


def typo_svg(w) -> str:
    """A typographic cover for a book with no cover image."""
    h = hue(str(w["id"]))
    big, small = w["title"] or "", w["author"] or ""
    t = "".join(f'<tspan x="24" dy="{0 if i == 0 else 30}">{e(x)}</tspan>'
                for i, x in enumerate(wrap_text(big, 16, 6)))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 360" width="240" height="360">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2=".6" y2="1"><stop offset="0" stop-color="hsl({h},32%,32%)"/>'
            f'<stop offset="1" stop-color="hsl({(h + 30) % 360},30%,17%)"/></linearGradient></defs>'
            f'<rect width="240" height="360" fill="url(#g)"/><rect x="10" y="10" width="220" height="340" rx="3" '
            f'fill="none" stroke="rgba(255,255,255,.35)"/>'
            f'<text x="24" y="62" fill="#fbf6ea" font-family="Georgia,serif" font-size="23" font-weight="600">{t}</text>'
            f'<text x="24" y="326" fill="#fbf6ea" opacity=".85" font-family="-apple-system,sans-serif" '
            f'font-size="13">{e(wrap_text(small, 30, 1)[0] if small else "")}</text></svg>')


SCROLL_JS = """<script>
document.querySelectorAll('.scroller').forEach(s=>{const t=s.querySelector('.track');
 s.querySelector('.prev').onclick=()=>t.scrollBy({left:-t.clientWidth*.85,behavior:'smooth'});
 s.querySelector('.next').onclick=()=>t.scrollBy({left:t.clientWidth*.85,behavior:'smooth'});});
</script>"""
