#!/usr/bin/env python3
"""
Publish daily report: MD → HTML → git push to docs/ → Bark push notification.

Usage:
    python scripts/publish_daily_report.py --date today
    python scripts/publish_daily_report.py --date 2026-03-05 --skip-push
    python scripts/publish_daily_report.py --date today --bark-key YOUR_KEY

Flow:
    1. Read the generated .md report from reports/daily_llm/{date}.md
    2. Convert to a styled HTML page
    3. Write to docs/reports/{date}.html + update docs/reports/index.html
    4. Git add + commit + push
    5. Send Bark notification with the GitHub Pages URL
"""

import argparse
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_SOURCE_DIR = PROJECT_ROOT / "reports" / "daily_llm"
DOCS_REPORT_DIR = PROJECT_ROOT / "docs" / "reports"

# GitHub Pages base URL: https://{user}.github.io/{repo}/reports/
GITHUB_USER = "Ikkisovi"
GITHUB_REPO = "portifolio-dashboard"
PAGES_BASE = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/reports"

DEFAULT_BARK_KEY = "3zUqGMZTPifBH7pRDw5Z5P"


def md_to_html(md_text: str, report_date: str) -> str:
    """Convert markdown report to a styled HTML page."""
    import re

    # Escape HTML in the markdown content
    def _esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = md_text.split("\n")
    html_lines = []
    in_code = False
    in_table = False
    in_details = False
    in_ul = False

    for line in lines:
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            if in_code:
                html_lines.append("</code></pre>")
                in_code = False
            else:
                lang = stripped[3:].strip()
                html_lines.append(f'<pre><code class="language-{lang}">')
                in_code = True
            continue
        if in_code:
            html_lines.append(_esc(line))
            continue

        # Details/summary
        if "<details>" in stripped:
            in_details = True
            html_lines.append("<details>")
            continue
        if "</details>" in stripped:
            in_details = False
            html_lines.append("</details>")
            continue
        if "<summary>" in stripped:
            summary_text = re.sub(r"</?summary>", "", stripped)
            html_lines.append(f"<summary>{_esc(summary_text)}</summary>")
            continue

        # Skip empty lines
        if not stripped:
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False
            if in_table:
                html_lines.append("</tbody></table></div>")
                in_table = False
            html_lines.append("")
            continue

        # Horizontal rule
        if stripped == "---":
            if in_table:
                html_lines.append("</tbody></table></div>")
                in_table = False
            html_lines.append("<hr>")
            continue

        # Headers
        if stripped.startswith("# "):
            html_lines.append(f"<h1>{_esc(stripped[2:])}</h1>")
            continue
        if stripped.startswith("## "):
            html_lines.append(f'<h2>{_esc(stripped[3:])}</h2>')
            continue
        if stripped.startswith("### "):
            html_lines.append(f"<h3>{_esc(stripped[4:])}</h3>")
            continue

        # Tables
        if "|" in stripped and stripped.startswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if all(set(c) <= {"-", ":", " "} for c in cells):
                continue  # separator row
            if not in_table:
                html_lines.append('<div class="table-wrap"><table><thead><tr>')
                for cell in cells:
                    html_lines.append(f"<th>{_esc(cell)}</th>")
                html_lines.append("</tr></thead><tbody>")
                in_table = True
            else:
                html_lines.append("<tr>")
                for cell in cells:
                    html_lines.append(f"<td>{_esc(cell)}</td>")
                html_lines.append("</tr>")
            continue

        # Bold
        processed = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", stripped)
        # Inline code
        processed = re.sub(r"`([^`]+)`", r'<code class="inline">\1</code>', processed)

        # List items
        if processed.startswith("- ") or processed.startswith("* "):
            if not in_ul:
                html_lines.append("<ul>")
                in_ul = True
            html_lines.append(f"<li>{processed[2:]}</li>")
            continue

        # Regular paragraph
        if in_ul:
            html_lines.append("</ul>")
            in_ul = False
        html_lines.append(f"<p>{processed}</p>")

    if in_ul:
        html_lines.append("</ul>")
    if in_table:
        html_lines.append("</tbody></table></div>")

    body = "\n".join(html_lines)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaSAGE Report — {report_date}</title>
<style>
  :root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text-muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    max-width: 900px; margin: 0 auto; padding: 24px 20px;
    line-height: 1.6;
  }}
  h1 {{ color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 20px; font-size: 1.6em; }}
  h2 {{ color: var(--green); margin: 28px 0 12px; font-size: 1.25em; }}
  h3 {{ color: var(--yellow); margin: 20px 0 8px; }}
  p {{ margin: 8px 0; }}
  hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}
  ul {{ padding-left: 24px; margin: 8px 0; }}
  li {{ margin: 4px 0; }}
  strong {{ color: #f0f6fc; }}
  code.inline {{
    background: var(--surface); border: 1px solid var(--border);
    padding: 2px 6px; border-radius: 4px; font-size: 0.9em; color: var(--accent);
  }}
  pre {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; overflow-x: auto;
    margin: 12px 0; font-size: 0.85em;
  }}
  pre code {{ color: var(--text); }}
  .table-wrap {{ overflow-x: auto; margin: 12px 0; }}
  table {{
    border-collapse: collapse; width: 100%;
    background: var(--surface); border-radius: 8px; overflow: hidden;
  }}
  th {{ background: #1c2128; color: var(--accent); text-align: left; padding: 10px 12px; font-size: 0.85em; white-space: nowrap; }}
  td {{ padding: 8px 12px; border-top: 1px solid var(--border); font-size: 0.85em; }}
  tr:hover td {{ background: #1c2128; }}
  details {{ margin: 16px 0; }}
  summary {{ cursor: pointer; color: var(--text-muted); font-size: 0.9em; }}
  .meta {{ color: var(--text-muted); font-size: 0.85em; margin-bottom: 20px; }}
  .nav {{ display: flex; gap: 16px; margin-bottom: 20px; font-size: 0.9em; }}
  .nav a {{ color: var(--accent); text-decoration: none; }}
  .nav a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="nav">
  <a href="index.html">← All Reports</a>
  <span style="color:var(--text-muted)">AlphaSAGE Daily Factor Report</span>
</div>
<div class="meta">Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} · qwen3.5:397b-cloud via Ollama</div>
{body}
</body>
</html>"""


def build_index(report_dir: Path) -> str:
    """Build an index.html listing all report files."""
    html_files = sorted(report_dir.glob("*.html"), reverse=True)
    html_files = [f for f in html_files if f.name != "index.html"]

    rows = []
    for f in html_files[:90]:  # last 90 reports
        date_str = f.stem
        rows.append(f'<li><a href="{f.name}">{date_str}</a></li>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaSAGE Daily Reports</title>
<style>
  :root {{ --bg: #0d1117; --surface: #161b22; --border: #30363d; --text: #e6edf3; --accent: #58a6ff; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); max-width: 600px; margin: 0 auto; padding: 40px 20px; }}
  h1 {{ color: var(--accent); margin-bottom: 24px; }}
  ul {{ list-style: none; }}
  li {{ padding: 10px 16px; border-bottom: 1px solid var(--border); }}
  li:hover {{ background: var(--surface); }}
  a {{ color: var(--accent); text-decoration: none; font-size: 1.05em; }}
  a:hover {{ text-decoration: underline; }}
  .count {{ color: #8b949e; font-size: 0.9em; margin-bottom: 16px; }}
</style>
</head>
<body>
<h1>📊 AlphaSAGE Daily Reports</h1>
<p class="count">{len(html_files)} reports available</p>
<ul>
{"".join(rows)}
</ul>
</body>
</html>"""


def git_push(project_root: Path, report_date: str) -> bool:
    """Git add, commit, push the docs/reports/ changes."""
    try:
        subprocess.run(["git", "add", "docs/reports/"], cwd=str(project_root), check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(project_root), capture_output=True,
        )
        if result.returncode == 0:
            print("[Publish] No changes to commit")
            return True

        subprocess.run(
            ["git", "commit", "-m", f"report: daily factor report {report_date}"],
            cwd=str(project_root), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(project_root), check=True, capture_output=True, timeout=60,
        )
        print(f"[Publish] Pushed to GitHub")
        return True
    except Exception as e:
        print(f"[Publish] Git push failed: {e}", file=sys.stderr)
        return False


def send_bark(bark_key: str, title: str, body: str, url: str) -> bool:
    """Send push notification via Bark (api.day.app)."""
    try:
        encoded_title = urllib.parse.quote(title)
        encoded_body = urllib.parse.quote(body)
        bark_url = f"https://api.day.app/{bark_key}/{encoded_title}/{encoded_body}?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(bark_url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[Bark] Push sent (status {resp.status})")
            return resp.status == 200
    except Exception as e:
        print(f"[Bark] Push failed: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Publish daily report to GitHub Pages + Bark")
    parser.add_argument("--date", default="today", help="Report date (YYYY-MM-DD or 'today')")
    parser.add_argument("--skip-push", action="store_true", help="Skip git push")
    parser.add_argument("--skip-bark", action="store_true", help="Skip Bark notification")
    parser.add_argument("--bark-key", default=DEFAULT_BARK_KEY, help="Bark push key")
    args = parser.parse_args()

    report_date = date.today().strftime("%Y-%m-%d") if args.date == "today" else args.date

    # 1. Read source MD
    md_path = REPORT_SOURCE_DIR / f"{report_date}.md"
    if not md_path.exists():
        print(f"[Publish] Report not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    md_text = md_path.read_text(encoding="utf-8-sig")
    print(f"[Publish] Read {md_path} ({len(md_text)} bytes)")

    # 2. Convert MD → HTML
    html_content = md_to_html(md_text, report_date)

    # 3. Write to docs/reports/
    DOCS_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = DOCS_REPORT_DIR / f"{report_date}.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"[Publish] Wrote {html_path}")

    # 4. Update index
    index_html = build_index(DOCS_REPORT_DIR)
    index_path = DOCS_REPORT_DIR / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    print(f"[Publish] Updated {index_path}")

    # 5. Git push
    pages_url = f"{PAGES_BASE}/{report_date}.html"
    if not args.skip_push:
        git_push(PROJECT_ROOT, report_date)
    else:
        print("[Publish] Skipping git push")

    # 6. Bark notification
    if not args.skip_bark:
        send_bark(
            bark_key=args.bark_key,
            title=f"📊 AlphaSAGE {report_date}",
            body="Daily factor report ready",
            url=pages_url,
        )
    else:
        print("[Publish] Skipping Bark push")

    print(f"\n[Publish] Done! URL: {pages_url}")


if __name__ == "__main__":
    main()
