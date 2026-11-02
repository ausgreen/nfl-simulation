"""Assembles a page from its source: shared.css and shared.js are inlined and the nav bar written in, so what's
published is one self-contained file (a page and its scripts can't be out of step in the CDN's cache)."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent
TABS = (("./", "All games"), ("game.html", "Game"), ("chart.html", "Chart"), ("edges.html", "Model vs Kalshi"),
        ("watch.html", "Watch list"))
CURRENT = ' aria-current="page"'


def _nav(current: str) -> str:
    tabs = "".join(f'<a href="{href}"{CURRENT if label == current else ""}>{label}</a>'
                   for href, label in TABS)
    return f'''<header class="site">
    <span class="brand">Live Game Companion</span>
    <nav class="tabs" aria-label="Pages">{tabs}</nav>
    <span class="beat" id="beat" title="Pulses when new numbers arrive"></span>
  </header>'''


def render(page: Path | str) -> str:
    """The page's HTML. In the source: `/*@shared.css*/`, `/*@shared.js*/` and `<!--@nav Tab label-->`."""
    html = Path(page).read_text()
    html = html.replace("/*@shared.css*/", (HERE / "shared.css").read_text())
    html = html.replace("/*@shared.js*/", (HERE / "shared.js").read_text())
    for _, label in TABS:
        html = html.replace(f"<!--@nav {label}-->", _nav(label))
    return html
