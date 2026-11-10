"""Build a static copy of the live map for hosting on its own (for example on Vercel).

The page needs no backend: the slips move in the visitor's browser, using a snapshot of
the numbers the running app had when this was built (average time per step, how invoices
split between approved, review and stopped). Links into the rest of the app are removed.

Run the app first (`uv run poe dev`), then, from the project root:

    python scripts/build_showcase.py                       # reads http://localhost:8000
    python scripts/build_showcase.py --url http://localhost:8002

Writes showcase/: index.html and assets/. Only the Python standard library is used.
"""

# The page's HTML and CSS are kept inline as text, so their lines can run long.
# ruff: noqa: E501

import argparse
import json
import re
import shutil
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "jev_invoice" / "web" / "static"
OUT = ROOT / "showcase"
REPO_URL = "https://github.com/advaithajith-coder/Jev_AI-Invoice-procesing-"

MARK = (
    '<path class="mark-frame" d="M18 84V29c0-9 7-16 16-16h24c18 0 30 12 30 29S76 70 58 70H39v14z"/>'
    '<path class="mark-page" d="M34 30h21l14 14v17H34z"/>'
    '<path class="mark-accent" d="M55 30v14h14zM82 3l2.5 6.5L91 12l-6.5 2.5L82 21l-2.5-6.5'
    'L73 12l6.5-2.5zM8 58h7v7H8zM3 69h7v7H3z"/>'
    '<path class="mark-lines" d="M41 43h9m-9 7h20m-20 7h14"/>'
)
FAVICON = (
    "data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 96'%3E"
    "%3Crect width='96' height='96' rx='20' fill='%23092746'/%3E"
    "%3Cpath d='M18 84V29c0-9 7-16 16-16h24c18 0 30 12 30 29S76 70 58 70H39v14z' fill='%23fff'/%3E"
    "%3Cpath d='M34 30h21l14 14v17H34z' fill='%23092746'/%3E%3C/svg%3E"
)


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (local app URL)
        body: bytes = response.read()
    return body.decode("utf-8")


def cut(html: str, pattern: str) -> str:
    """Remove one block that must exist, so a template change cannot slip past silently."""
    new, count = re.subn(pattern, "", html, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"Could not find {pattern!r} in the page: has the template changed?")
    return new


def the_map(page: str) -> str:
    """The live map and its step dialogs, without anything that needs the backend."""
    start = page.index('<div class="parser-home">')
    end = page.index("</main>")
    html = page[start:end]
    html = cut(html, r'<header class="parser-hero">.*?</header>')
    html = cut(
        html, r'<div class="section-head">.*?</div>\s*<section class="wf-preparation".*?</section>'
    )
    html = cut(html, r'<p class="wf-upload-prompt">.*?</p>')
    html = cut(html, r'<p class="wf-speed" data-speed[^>]*>.*?</p>')  # rewritten without links
    # The app's own data block (with invoice links) is replaced by the cleaned snapshot.
    html = cut(html, r'<script type="application/json" id="workflow-data">.*?</script>')
    html = html.replace(' data-live-url="/workflow/live"', "")
    return html.strip()


def snapshot(page: str) -> dict:
    """The numbers the map runs on, without invoice links or anything in progress."""
    match = re.search(
        r'<script type="application/json" id="workflow-data">(.*?)</script>', page, re.S
    )
    if not match:
        raise SystemExit("No workflow data in the page.")
    data = json.loads(match.group(1))
    data["in_flight"] = []
    data["recent"] = []
    if data.get("speed"):
        data["speed"].pop("url", None)
    return data


def showcase_script() -> str:
    """workflow.js without polling the backend and without links to invoice pages."""
    js = (STATIC / "workflow.js").read_text(encoding="utf-8")
    replacements = {
        "  setTimeout(refresh, 3000);\n": "  // Static snapshot: nothing to refresh.\n",
        '    const link = el("a", `${sp.label}${sp.number ? ` ${sp.number}` : ""}`);\n'
        "    link.href = sp.url;\n": '    const link = el("span", `${sp.label}${sp.number ? ` ${sp.number}` : ""}`);\n',
    }
    for old, new in replacements.items():
        if old not in js:
            raise SystemExit(f"workflow.js changed; update build_showcase.py near: {old.strip()!r}")
        js = js.replace(old, new)
    return js


def speed_line(data: dict) -> str:
    sp = data.get("speed")
    if not sp:
        return '<p class="wf-speed" data-speed hidden></p>'

    def secs(ms: float) -> str:
        return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.1f} s"

    label = f"{sp['label']} {sp['number']}".strip()
    return (
        f'<p class="wf-speed" data-speed>Fastest invoice: <strong>{secs(sp["fastest_ms"])}</strong> '
        f"from arrival to decision ({label}). Typical: {secs(sp['typical_ms'])} across "
        f"{sp['count']} invoices.</p>"
    )


def page_html(body: str, data: dict, built: str) -> str:
    counts = data["counts"]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Parser.AI | Live invoice workflow</title>
<meta name="description" content="How Parser.AI moves a supplier invoice from arrival to decision: OCR, AI extraction, checks in code, Jev judgment and a fixed policy.">
<link rel="icon" href="{FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&family=Space+Grotesk:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="assets/app.css">
<link rel="stylesheet" href="assets/design.css">
<link rel="stylesheet" href="assets/showcase.css">
</head>
<body>
<main class="showcase">
  <header class="showcase__head">
    <div class="showcase__brand">
      <svg viewBox="0 0 96 96" aria-hidden="true" focusable="false">{MARK}</svg>
      <div><b>Parser.AI</b><span>Your AI powered invoice manager</span></div>
    </div>
    <a class="showcase__code" href="{REPO_URL}" target="_blank" rel="noopener">View the code on GitHub</a>
  </header>
  <section class="showcase__intro">
    <h1>Where every invoice goes</h1>
    <p>A supplier invoice is read (local OCR for scans, an LLM for the fields), checked in code
      against vendors, purchase orders and deliveries, judged by Jev for intent such as fraud or
      hidden instructions, and decided by a fixed policy. Press <b>i</b> on a step to see how it works.</p>
    <ul class="showcase__figures">
      <li><b>{counts["total"]}</b> invoices</li>
      <li><b>{counts["approved"] + counts["exported"]}</b> checks passed</li>
      <li><b>{counts["pending_review"]}</b> waiting for review</li>
      <li><b>{counts["failed"] + counts["rejected"]}</b> stopped</li>
    </ul>
  </section>
  {speed_line(data)}
  {body}
  <script type="application/json" id="workflow-data">{json.dumps(data)}</script>
  <footer class="showcase__foot">
    <p>Demo data for a fictional company, Northwind Analytics Inc. The slips illustrate the route
      in the same proportions as the demo invoices; figures are a snapshot from {built}.</p>
  </footer>
</main>
<script src="assets/workflow.js" defer></script>
</body>
</html>
"""


SHOWCASE_CSS = """\
body { margin: 0; background: var(--canvas); }
.showcase { max-width: 1240px; margin: 0 auto; padding: 1.5rem clamp(1rem, 3vw, 2.5rem) 3rem; }
.showcase__head { display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  flex-wrap: wrap; margin-bottom: 2rem; }
.showcase__brand { display: inline-flex; align-items: center; gap: .85rem; padding: .55rem 1.1rem .55rem .6rem;
  background: #092746; border-radius: var(--r-lg); color: #fff; }
.showcase__brand svg { width: 52px; height: 52px; flex: none; }
.showcase__brand .mark-frame { fill: #fff; } .showcase__brand .mark-page { fill: #092746; }
.showcase__brand .mark-accent { fill: #5cdec9; }
.showcase__brand .mark-lines { fill: none; stroke: #fff; stroke-width: 2; stroke-linecap: round; }
.showcase__brand b { display: block; font-family: "Space Grotesk", var(--sans); font-size: 1.9rem;
  font-weight: 700; letter-spacing: -.06em; line-height: 1; color: #5cdec9; }
.showcase__brand span { display: block; margin-top: .2rem; font-size: .9rem; }
.showcase__code { display: inline-flex; align-items: center; min-height: 44px; padding: .55rem 1rem;
  border: 1px solid var(--line-strong); border-radius: var(--r-md); color: var(--ink); text-decoration: none;
  font-weight: 500; }
.showcase__code:hover { background: var(--surface-2); }
.showcase__intro h1 { font-family: "Space Grotesk", var(--sans); font-size: clamp(1.7rem, 3vw, 2.3rem);
  font-weight: 500; letter-spacing: -.035em; margin: 0; }
.showcase__intro p { color: var(--ink-2); max-width: 70ch; margin: .6rem 0 1.25rem; }
.showcase__figures { list-style: none; display: flex; flex-wrap: wrap; gap: .5rem 1.75rem; margin: 0 0 .5rem;
  padding: 0; color: var(--ink-2); }
.showcase__figures b { color: var(--ink); font-size: 1.15rem; font-variant-numeric: tabular-nums; }
.showcase .wf-speed { margin: .5rem 0 1rem; }
.showcase__foot p { color: var(--ink-3); font-size: .85rem; margin-top: 1.5rem; max-width: 80ch; }
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8000", help="the running app")
    args = parser.parse_args()

    page = fetch(args.url.rstrip("/") + "/workflow")
    data = snapshot(page)
    body = the_map(page)
    built = datetime.now(UTC).strftime("%d %b %Y")

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    (OUT / "index.html").write_text(page_html(body, data, built), encoding="utf-8")
    shutil.copy(STATIC / "app.css", OUT / "assets" / "app.css")
    shutil.copy(STATIC / "react" / "design.css", OUT / "assets" / "design.css")
    (OUT / "assets" / "showcase.css").write_text(SHOWCASE_CSS, encoding="utf-8")
    (OUT / "assets" / "workflow.js").write_text(showcase_script(), encoding="utf-8")
    print(f"Wrote {OUT} ({data['counts']['total']} invoices in the snapshot)")


if __name__ == "__main__":
    main()
