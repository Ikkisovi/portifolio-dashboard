#!/usr/bin/env python3
"""
Publish daily report markdown to styled HTML, docs, git, and Bark.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_SOURCE_DIR = PROJECT_ROOT / "reports" / "daily_llm"
DOCS_REPORT_DIR = PROJECT_ROOT / "docs" / "reports"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "scripts" / "daily_report_config.json"

GITHUB_USER = "Ikkisovi"
GITHUB_REPO = "portifolio-dashboard"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/docs/reports"
PAGES_BASE = f"https://htmlpreview.github.io/?{RAW_BASE}"

DEFAULT_BARK_KEY = "3zUqGMZTPifBH7pRDw5Z5P"


def _load_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return json.loads(path.read_text(encoding=encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def _load_publish_config(config_path: Path) -> dict:
    payload = _load_json(config_path)
    return payload if isinstance(payload, dict) else {}


def _resolve_storage_root(storage_root: Path) -> Path:
    index_path = storage_root / "deployments.json"
    if index_path.exists():
        index = _load_json(index_path)
        active_id = index.get("active_id") if isinstance(index, dict) else None
        if active_id:
            candidate = storage_root / "deployments" / str(active_id)
            if candidate.exists():
                return candidate
    return storage_root


def _extract_report_date(payload: dict, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    for key in ("data_as_of_date", "as_of_date", "date"):
        value = payload.get(key)
        if value:
            return str(value)[:10]
    return fallback


def _factor_index(name: str) -> int:
    match = re.search(r"(\d+)$", str(name))
    return int(match.group(1)) if match else 10**6


def _load_factor_selection_payload(report_date: str, storage_root: Path) -> Optional[dict]:
    report_dir = _resolve_storage_root(storage_root) / "reports" / "factor_selection_daily"
    exact = _load_json(report_dir / f"{report_date}.json")
    if isinstance(exact, dict):
        return exact
    latest = _load_json(report_dir / "latest.json")
    return latest if isinstance(latest, dict) else None


def _load_factor_interpretation(path: Path) -> dict:
    payload = _load_json(path)
    return payload if isinstance(payload, dict) else {}


def _recent_activity_score(row: dict) -> int:
    symbols = set()
    latest = row.get("latest") or {}
    for side in ("top", "bottom"):
        for item in list(latest.get(side) or []):
            if isinstance(item, dict) and item.get("symbol"):
                symbols.add(str(item["symbol"]))
    for hist in list(row.get("history_20d") or []):
        if not isinstance(hist, dict):
            continue
        for side in ("top", "bottom"):
            for item in list(hist.get(side) or []):
                if isinstance(item, dict) and item.get("symbol"):
                    symbols.add(str(item["symbol"]))
    return len(symbols)


def build_factor_atlas_rows(report_date: str, storage_root: Path, factor_interpretation_path: Path) -> list[dict]:
    payload = _load_factor_selection_payload(report_date, storage_root)
    if not isinstance(payload, dict):
        return []

    interpretation = _load_factor_interpretation(factor_interpretation_path)
    data_as_of_date = _extract_report_date(payload, fallback=report_date)
    rows: list[dict] = []

    for category, field in (("cma", "cma_factors"), ("actionable", "actionable_factors")):
        for row in list(payload.get(field) or []):
            if not isinstance(row, dict) or not row.get("name"):
                continue
            name = str(row.get("name"))
            interp = interpretation.get(name) if isinstance(interpretation.get(name), dict) else {}
            row_expr_hash = str(row.get("expr_hash") or "")
            interp_expr_hash = str(interp.get("expr_hash") or "")
            rows.append(
                {
                    "name": name,
                    "category": category,
                    "index": _factor_index(name),
                    "short_name": interp.get("short_name") or name,
                    "interpretation": interp.get("interpretation") or "",
                    "extended_interpretation": interp.get("extended_interpretation") or "",
                    "direction_note": interp.get("direction_note") or "",
                    "expr": row.get("expr") or interp.get("expr") or "",
                    "expr_hash": row_expr_hash or interp_expr_hash,
                    "data_as_of_date": data_as_of_date,
                    "latest": row.get("latest") or {},
                    "history_20d": list(row.get("history_20d") or []),
                    "recent_activity": _recent_activity_score(row),
                    "interpretation_stale": bool(row_expr_hash and interp_expr_hash and row_expr_hash != interp_expr_hash),
                }
            )

    rows.sort(key=lambda item: (item["index"], item["category"], item["name"]))
    return rows


def _render_html_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return '<p class="factor-empty">No data available.</p>'
    head = "".join(f"<th>{escape(str(cell))}</th>" for cell in headers)
    body_rows = []
    for row in rows:
        body_rows.append("<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>")
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _render_rank_table(rows: list[dict]) -> str:
    rendered = []
    for row in list(rows or []):
        if isinstance(row, dict):
            rendered.append([row.get("symbol", ""), row.get("score", ""), row.get("rank", "")])
    return _render_html_table(["Symbol", "Score", "Rank"], rendered)


def _render_history_table(rows: list[dict]) -> str:
    rendered = []
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        top_symbols = ", ".join(
            str(item.get("symbol"))
            for item in list(row.get("top") or [])
            if isinstance(item, dict) and item.get("symbol")
        )
        bottom_symbols = ", ".join(
            str(item.get("symbol"))
            for item in list(row.get("bottom") or [])
            if isinstance(item, dict) and item.get("symbol")
        )
        rendered.append([row.get("date", ""), "Top", top_symbols])
        rendered.append([row.get("date", ""), "Bottom", bottom_symbols])
    return _render_html_table(["Date", "Side", "Symbols"], rendered)


def build_factor_atlas_html(factor_atlas_rows: list[dict], report_date: str) -> str:
    if not factor_atlas_rows:
        return ""

    nav_items = []
    cards = []
    for row in factor_atlas_rows:
        target_id = f'factor-{row["name"]}'
        search_text = " ".join(
            [
                str(row.get("name") or ""),
                str(row.get("short_name") or ""),
                str(row.get("interpretation") or ""),
                str(row.get("expr") or ""),
            ]
        ).lower()
        nav_items.append(
            (
                f'<button class="factor-nav-item" type="button" data-target="{escape(target_id)}" '
                f'data-category="{escape(row["category"])}" data-index="{row["index"]}" '
                f'data-name="{escape(str(row["name"]).lower())}" data-recent-activity="{row["recent_activity"]}" '
                f'data-search="{escape(search_text)}">'
                f'<span class="factor-nav-name">{escape(row["name"])}</span>'
                f'<span class="factor-nav-short">{escape(row["short_name"])}</span>'
                f'<span class="factor-nav-summary">{escape(row["interpretation"])}</span>'
                f"</button>"
            )
        )
        stale_note = '<span class="factor-stale">Interpretation may be stale</span>' if row.get("interpretation_stale") else ""
        cards.append(
            (
                f'<details class="factor-card" id="{escape(target_id)}" data-category="{escape(row["category"])}" '
                f'data-index="{row["index"]}" data-name="{escape(str(row["name"]).lower())}" '
                f'data-recent-activity="{row["recent_activity"]}" data-search="{escape(search_text)}">'
                f'<summary><span class="factor-card-title">{escape(row["name"])}</span>'
                f'<span class="factor-chip">{escape(row["category"].upper())}</span>'
                f'<span class="factor-card-subtitle">{escape(row["short_name"])}</span></summary>'
                f'<div class="factor-card-body">'
                f'<section class="factor-section"><h4>Index &amp; Meta</h4>'
                f'<p><strong>Index:</strong> {row["index"]}</p>'
                f'<p><strong>Category:</strong> {escape(row["category"].upper())}</p>'
                f'<p><strong>Short Name:</strong> {escape(row["short_name"])}</p>'
                f'<p><strong>Data Date:</strong> {escape(row["data_as_of_date"])}</p>{stale_note}</section>'
                f'<section class="factor-section"><h4>Expr &amp; Interpretation</h4>'
                f'<p><code class="inline">{escape(row["expr"])}</code></p>'
                f'<p>{escape(row["interpretation"])}</p>'
                f'<p>{escape(row["extended_interpretation"])}</p>'
                f'<p><strong>Direction:</strong> {escape(row["direction_note"])}</p></section>'
                f'<section class="factor-section"><h4>20-Day Holding Series</h4>{_render_history_table(row.get("history_20d") or [])}</section>'
                f'<section class="factor-section"><h4>Latest Top 10</h4>{_render_rank_table((row.get("latest") or {}).get("top") or [])}</section>'
                f'<section class="factor-section"><h4>Latest Bottom 10</h4>{_render_rank_table((row.get("latest") or {}).get("bottom") or [])}</section>'
                f"</div></details>"
            )
        )

    return (
        '<section class="factor-atlas" id="factor-atlas">'
        '<div class="factor-atlas-header">'
        "<h2>Factor Atlas</h2>"
        f'<p class="factor-atlas-subtitle">Searchable factor index for {escape(report_date)}.</p>'
        "</div>"
        '<div class="factor-atlas-shell">'
        '<aside class="factor-sidebar">'
        '<div class="factor-sidebar-controls">'
        '<div class="factor-filter-group">'
        '<button type="button" class="factor-filter is-active" data-filter="all">All</button>'
        '<button type="button" class="factor-filter" data-filter="cma">CMA</button>'
        '<button type="button" class="factor-filter" data-filter="actionable">Actionable</button>'
        "</div>"
        '<input id="factor-search" class="factor-search" type="search" '
        'placeholder="Search factor, short name, interpretation, expr" />'
        '<label class="factor-sort-label">Sort'
        '<select id="factor-sort" class="factor-sort">'
        '<option value="index">Index</option>'
        '<option value="name">A-Z</option>'
        '<option value="recent_activity">Recent Activity</option>'
        "</select></label>"
        f'<p id="factor-count" class="factor-count">{len(factor_atlas_rows)} factors</p>'
        "</div>"
        f'<div class="factor-nav-list">{"".join(nav_items)}</div>'
        "</aside>"
        f'<div class="factor-card-list">{"".join(cards)}</div>'
        "</div>"
        "</section>"
    )


def md_to_html(md_text: str, report_date: str, factor_atlas_rows: Optional[list[dict]] = None) -> str:
    """Convert markdown report to a styled HTML page."""

    def _esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = md_text.split("\n")
    html_lines = []
    in_code = False
    in_table = False
    in_ul = False
    skipping_factor_tables = False
    factor_atlas_html = build_factor_atlas_html(factor_atlas_rows or [], report_date)

    def _close_open_blocks() -> None:
        nonlocal in_table, in_ul
        if in_ul:
            html_lines.append("</ul>")
            in_ul = False
        if in_table:
            html_lines.append("</tbody></table></div>")
            in_table = False

    for line in lines:
        stripped = line.strip()

        if factor_atlas_html and stripped == "## Factor Selection Tables":
            _close_open_blocks()
            html_lines.append("<!-- FACTOR_ATLAS -->")
            skipping_factor_tables = True
            continue

        if skipping_factor_tables:
            if stripped == "---":
                skipping_factor_tables = False
                html_lines.append("<hr>")
            continue

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

        if "<details>" in stripped:
            html_lines.append("<details>")
            continue
        if "</details>" in stripped:
            html_lines.append("</details>")
            continue
        if "<summary>" in stripped:
            summary_text = re.sub(r"</?summary>", "", stripped)
            html_lines.append(f"<summary>{_esc(summary_text)}</summary>")
            continue

        if not stripped:
            _close_open_blocks()
            html_lines.append("")
            continue

        if stripped == "---":
            _close_open_blocks()
            html_lines.append("<hr>")
            continue

        if stripped.startswith("# "):
            html_lines.append(f"<h1>{_esc(stripped[2:])}</h1>")
            continue
        if stripped.startswith("## "):
            html_lines.append(f"<h2>{_esc(stripped[3:])}</h2>")
            continue
        if stripped.startswith("### "):
            html_lines.append(f"<h3>{_esc(stripped[4:])}</h3>")
            continue
        if stripped.startswith("#### "):
            html_lines.append(f"<h4>{_esc(stripped[5:])}</h4>")
            continue

        if "|" in stripped and stripped.startswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if all(set(c) <= {"-", ":", " "} for c in cells):
                continue
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

        processed = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", stripped)
        processed = re.sub(r"`([^`]+)`", r'<code class="inline">\1</code>', processed)

        if processed.startswith("- ") or processed.startswith("* "):
            if not in_ul:
                html_lines.append("<ul>")
                in_ul = True
            html_lines.append(f"<li>{processed[2:]}</li>")
            continue

        if in_ul:
            html_lines.append("</ul>")
            in_ul = False
        html_lines.append(f"<p>{processed}</p>")

    _close_open_blocks()

    body = "\n".join(html_lines)
    if factor_atlas_html:
        if "<!-- FACTOR_ATLAS -->" in body:
            body = body.replace("<!-- FACTOR_ATLAS -->", factor_atlas_html)
        else:
            body = f"{body}\n{factor_atlas_html}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaSAGE Report - {report_date}</title>
<style>
  :root {{
    --bg: #0d1117; --surface: #161b22; --surface-2: #0f141a; --sidebar: #11161d;
    --border: #30363d; --text: #e6edf3; --text-muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', -apple-system, sans-serif; background: var(--bg); color: var(--text); max-width: 1320px; margin: 0 auto; padding: 24px 20px 40px; line-height: 1.6; }}
  h1 {{ color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 20px; font-size: 1.6em; }}
  h2 {{ color: var(--green); margin: 28px 0 12px; font-size: 1.25em; }}
  h3 {{ color: var(--yellow); margin: 20px 0 8px; }}
  h4 {{ color: var(--yellow); margin: 16px 0 8px; }}
  p {{ margin: 8px 0; }}
  hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}
  ul {{ padding-left: 24px; margin: 8px 0; }}
  li {{ margin: 4px 0; }}
  strong {{ color: #f0f6fc; }}
  code.inline {{ background: var(--surface); border: 1px solid var(--border); padding: 2px 6px; border-radius: 4px; font-size: 0.9em; color: var(--accent); }}
  pre {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; overflow-x: auto; margin: 12px 0; font-size: 0.85em; }}
  pre code {{ color: var(--text); }}
  img.report-chart {{ display: block; max-width: 100%; height: auto; margin: 12px 0 18px; border: 1px solid var(--border); border-radius: 8px; background: #ffffff; padding: 6px; }}
  .table-wrap {{ overflow-x: auto; margin: 12px 0; }}
  table {{ border-collapse: collapse; width: 100%; background: var(--surface); border-radius: 8px; overflow: hidden; }}
  th {{ background: #1c2128; color: var(--accent); text-align: left; padding: 10px 12px; font-size: 0.85em; white-space: nowrap; }}
  td {{ padding: 8px 12px; border-top: 1px solid var(--border); font-size: 0.85em; vertical-align: top; }}
  tr:hover td {{ background: #1c2128; }}
  details {{ margin: 16px 0; }}
  summary {{ cursor: pointer; color: var(--text-muted); font-size: 0.9em; }}
  .meta {{ color: var(--text-muted); font-size: 0.85em; margin-bottom: 20px; }}
  .nav {{ display: flex; gap: 16px; margin-bottom: 20px; font-size: 0.9em; }}
  .nav a {{ color: var(--accent); text-decoration: none; }}
  .nav a:hover {{ text-decoration: underline; }}
  .factor-atlas {{ margin: 28px 0; padding: 20px; background: linear-gradient(180deg, rgba(88,166,255,0.08), rgba(22,27,34,0.96)); border: 1px solid var(--border); border-radius: 18px; }}
  .factor-atlas-header {{ margin-bottom: 16px; }}
  .factor-atlas-subtitle {{ color: var(--text-muted); margin-top: 6px; }}
  .factor-atlas-shell {{ display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 18px; align-items: start; }}
  .factor-sidebar {{ position: sticky; top: 16px; align-self: start; background: var(--sidebar); border: 1px solid var(--border); border-radius: 16px; padding: 14px; }}
  .factor-sidebar-controls {{ display: grid; gap: 12px; margin-bottom: 14px; }}
  .factor-filter-group {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .factor-filter {{ border: 1px solid var(--border); background: transparent; color: var(--text-muted); border-radius: 999px; padding: 6px 10px; cursor: pointer; }}
  .factor-filter.is-active {{ background: var(--accent); color: #08131f; border-color: var(--accent); font-weight: 600; }}
  .factor-search, .factor-sort {{ width: 100%; background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px; }}
  .factor-sort-label {{ display: grid; gap: 6px; color: var(--text-muted); font-size: 0.85em; }}
  .factor-count {{ color: var(--text-muted); font-size: 0.85em; }}
  .factor-nav-list {{ display: grid; gap: 10px; max-height: calc(100vh - 180px); overflow-y: auto; padding-right: 4px; }}
  .factor-nav-item {{ width: 100%; text-align: left; background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px; cursor: pointer; display: grid; gap: 4px; }}
  .factor-nav-item:hover, .factor-nav-item.is-active {{ border-color: var(--accent); background: #182434; }}
  .factor-nav-name {{ font-weight: 700; color: #f0f6fc; }}
  .factor-nav-short {{ color: var(--accent); font-size: 0.9em; }}
  .factor-nav-summary {{ color: var(--text-muted); font-size: 0.82em; line-height: 1.45; }}
  .factor-card-list {{ display: grid; gap: 14px; }}
  .factor-card {{ background: var(--surface-2); border: 1px solid var(--border); border-radius: 16px; overflow: hidden; }}
  .factor-card summary {{ list-style: none; display: grid; grid-template-columns: 1fr auto; gap: 6px 10px; align-items: center; padding: 14px 16px; cursor: pointer; color: var(--text); }}
  .factor-card summary::-webkit-details-marker {{ display: none; }}
  .factor-card-title {{ font-size: 1.05em; font-weight: 700; color: #f0f6fc; }}
  .factor-chip {{ justify-self: end; border-radius: 999px; background: rgba(63,185,80,0.16); color: #86efac; padding: 4px 9px; font-size: 0.75em; }}
  .factor-card-subtitle {{ grid-column: 1 / -1; color: var(--text-muted); }}
  .factor-card-body {{ border-top: 1px solid var(--border); padding: 14px 16px 18px; display: grid; gap: 16px; }}
  .factor-section p {{ margin: 6px 0; }}
  .factor-stale {{ display: inline-block; margin-top: 6px; border-radius: 999px; padding: 4px 8px; background: rgba(210,153,34,0.14); color: #facc15; font-size: 0.8em; }}
  .factor-empty {{ color: var(--text-muted); font-style: italic; }}
  .factor-hidden {{ display: none !important; }}
  @media (max-width: 980px) {{
    .factor-atlas-shell {{ grid-template-columns: 1fr; }}
    .factor-sidebar {{ position: static; }}
    .factor-nav-list {{ max-height: none; }}
  }}
</style>
</head>
<body>
<div class="nav">
  <a href="index.html">Back to all reports</a>
  <span style="color:var(--text-muted)">AlphaSAGE Daily Factor Report</span>
</div>
<div class="meta">Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')} via publish_daily_report.py</div>
{body}
<script>
(() => {{
  const navItems = Array.from(document.querySelectorAll('.factor-nav-item'));
  const cards = Array.from(document.querySelectorAll('.factor-card'));
  if (!navItems.length || !cards.length) return;
  const countEl = document.getElementById('factor-count');
  const searchEl = document.getElementById('factor-search');
  const sortEl = document.getElementById('factor-sort');
  const filterButtons = Array.from(document.querySelectorAll('.factor-filter'));
  const navList = document.querySelector('.factor-nav-list');
  const cardList = document.querySelector('.factor-card-list');
  let activeFilter = 'all';
  const sorters = {{
    index: (a, b) => Number(a.dataset.index || 0) - Number(b.dataset.index || 0),
    name: (a, b) => String(a.dataset.name || '').localeCompare(String(b.dataset.name || '')),
    recent_activity: (a, b) => Number(b.dataset.recentActivity || 0) - Number(a.dataset.recentActivity || 0) || Number(a.dataset.index || 0) - Number(b.dataset.index || 0),
  }};
  function matches(el) {{
    const searchText = String(searchEl?.value || '').trim().toLowerCase();
    const haystack = String(el.dataset.search || '');
    const matchesSearch = !searchText || haystack.includes(searchText);
    const matchesFilter = activeFilter === 'all' || el.dataset.category === activeFilter;
    return matchesSearch && matchesFilter;
  }}
  function applyVisibility() {{
    navItems.forEach((item) => item.classList.toggle('factor-hidden', !matches(item)));
    cards.forEach((card) => card.classList.toggle('factor-hidden', !matches(card)));
    const visibleCount = cards.filter((card) => !card.classList.contains('factor-hidden')).length;
    if (countEl) countEl.textContent = `${{visibleCount}} factor${{visibleCount === 1 ? '' : 's'}}`;
  }}
  function applySort() {{
    const sorter = sorters[sortEl?.value || 'index'] || sorters.index;
    navItems.sort(sorter).forEach((item) => navList?.appendChild(item));
    cards.sort(sorter).forEach((card) => cardList?.appendChild(card));
  }}
  filterButtons.forEach((button) => {{
    button.addEventListener('click', () => {{
      activeFilter = button.dataset.filter || 'all';
      filterButtons.forEach((item) => item.classList.toggle('is-active', item === button));
      applyVisibility();
    }});
  }});
  searchEl?.addEventListener('input', applyVisibility);
  sortEl?.addEventListener('change', () => {{ applySort(); applyVisibility(); }});
  navItems.forEach((item) => {{
    item.addEventListener('click', () => {{
      const target = document.getElementById(item.dataset.target || '');
      if (!target) return;
      navItems.forEach((nav) => nav.classList.toggle('is-active', nav === item));
      target.open = true;
      target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }});
  }});
  applySort();
  applyVisibility();
}})();
</script>
</body>
</html>"""


def build_index(report_dir: Path) -> str:
    """Build an index.html listing all report files."""
    html_files = sorted(report_dir.glob("*.html"), reverse=True)
    html_files = [path for path in html_files if path.name != "index.html"]
    rows = [f'<li><a href="{path.name}">{path.stem}</a></li>' for path in html_files[:90]]
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
<h1>AlphaSAGE Daily Reports</h1>
<p class="count">{len(html_files)} reports available</p>
<ul>
{"".join(rows)}
</ul>
</body>
</html>"""


def git_push(project_root: Path, report_date: str) -> bool:
    """Git add, commit, and push docs/reports."""
    try:
        subprocess.run(["git", "add", "docs/reports/"], cwd=str(project_root), check=True, capture_output=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(project_root), capture_output=True)
        if result.returncode == 0:
            print("[Publish] No changes to commit")
            return True
        subprocess.run(["git", "commit", "-m", f"report: daily factor report {report_date}"], cwd=str(project_root), check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=str(project_root), check=True, capture_output=True, timeout=60)
        print("[Publish] Pushed to GitHub")
        return True
    except Exception as exc:
        print(f"[Publish] Git push failed: {exc}", file=sys.stderr)
        return False


def send_bark(bark_key: str, title: str, body: str, url: str) -> bool:
    """Send a Bark push notification."""
    try:
        encoded_title = urllib.parse.quote(title)
        encoded_body = urllib.parse.quote(body)
        bark_url = f"https://api.day.app/{bark_key}/{encoded_title}/{encoded_body}?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(bark_url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[Bark] Push sent (status {resp.status})")
            return resp.status == 200
    except Exception as exc:
        print(f"[Bark] Push failed: {exc}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish daily report to HTML, docs, git, and Bark")
    parser.add_argument("--date", default="today", help="Report date (YYYY-MM-DD or 'today')")
    parser.add_argument("--skip-push", action="store_true", help="Skip git push")
    parser.add_argument("--skip-bark", action="store_true", help="Skip Bark notification")
    parser.add_argument("--bark-key", default=DEFAULT_BARK_KEY, help="Bark push key")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Report config JSON path")
    parser.add_argument("--storage", default=None, help="Override storage root path for factor atlas data")
    args = parser.parse_args()

    report_date = date.today().strftime("%Y-%m-%d") if args.date == "today" else args.date
    md_path = REPORT_SOURCE_DIR / f"{report_date}.md"
    if not md_path.exists():
        print(f"[Publish] Report not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    md_text = md_path.read_text(encoding="utf-8-sig")
    print(f"[Publish] Read {md_path} ({len(md_text)} bytes)")

    config = _load_publish_config(Path(args.config))
    storage_root = Path(args.storage) if args.storage else Path(config.get("storage_root", PROJECT_ROOT / "storage" / "alphasage"))
    if not storage_root.is_absolute():
        storage_root = PROJECT_ROOT / storage_root
    factor_interpretation_path = Path(config.get("factor_interpretation_path", PROJECT_ROOT / "scripts" / "factor_interpretation.json"))
    if not factor_interpretation_path.is_absolute():
        factor_interpretation_path = PROJECT_ROOT / factor_interpretation_path
    factor_atlas_rows = build_factor_atlas_rows(report_date=report_date, storage_root=storage_root, factor_interpretation_path=factor_interpretation_path)

    html_content = md_to_html(md_text, report_date, factor_atlas_rows=factor_atlas_rows)

    DOCS_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = DOCS_REPORT_DIR / f"{report_date}.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"[Publish] Wrote {html_path}")

    index_path = DOCS_REPORT_DIR / "index.html"
    index_path.write_text(build_index(DOCS_REPORT_DIR), encoding="utf-8")
    print(f"[Publish] Updated {index_path}")

    pages_url = f"{PAGES_BASE}/{report_date}.html"
    if not args.skip_push:
        git_push(PROJECT_ROOT, report_date)
    else:
        print("[Publish] Skipping git push")

    if not args.skip_bark:
        send_bark(bark_key=args.bark_key, title=f"AlphaSAGE {report_date}", body="Daily factor report ready", url=pages_url)
    else:
        print("[Publish] Skipping Bark push")

    print(f"\n[Publish] Done! URL: {pages_url}")


if __name__ == "__main__":
    main()
