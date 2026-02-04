# Minimal Light React-Style Dashboard + Real Example Data Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Redesign the Streamlit dashboard UI to a light, minimal React-style look and replace example data with real portfolio data for MU, SNDK, CDE, and RKLB sourced from `e:\factor\lean_project\data\equity`.

**Architecture:** Keep Streamlit as the UI framework, inject a global CSS theme for the React-style look, and replace example-mode data loaders with a real-data portfolio builder that computes equity, holdings, and benchmarks from daily price files. Preserve live-mode behavior unchanged.

**Tech Stack:** Python, Streamlit, Pandas, Plotly.

---

**Important Changes / Public Interfaces**
- New config constants in `dashboard/config.py` for example data sources and styling.
- `dashboard/backend/data_loader.py` example data functions (`load_example_*`) will return real-data-derived values (with JSON fallback).
- Charts switch to light Plotly styling.

---

## Task 0: Worktree + Plan File

**Files:**
- Create: `docs/plans/2026-02-04-dashboard-minimal-ui.md`

**Step 1: Create isolated worktree**
Run: `superpowers:using-git-worktrees`
Expected: new worktree created for this feature

**Step 2: Save this plan to disk**
Create `docs/plans/2026-02-04-dashboard-minimal-ui.md` with this plan content.

**Step 3: Commit**
Run:
```bash
git add docs/plans/2026-02-04-dashboard-minimal-ui.md
git commit -m "docs: add minimal dashboard UI + real example data plan"
```

---

## Task 1: Config Updates (Theme + Example Data Settings)

**Files:**
- Modify: `dashboard/config.py`

**Step 1: Write failing test**
Create `tests/test_dashboard_config.py`:
```python
from dashboard import config

def test_example_data_settings_present():
    assert "MU" in config.EXAMPLE_TICKERS
    assert config.EXAMPLE_START_DATE == "2025-10-01"
    assert config.EXAMPLE_BASE_CAPITAL == 100000

def test_light_theme_colors():
    assert config.COLORS["background"] == "#f7f7f5"
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_dashboard_config.py -v`
Expected: FAIL because constants don't exist / colors differ.

**Step 3: Write minimal implementation**
Update `dashboard/config.py`:
- Add `EQUITY_DATA_DIR = BASE_PATH.parent / "data" / "equity" / "usa" / "daily"`
- Add `EXAMPLE_TICKERS = ["MU", "SNDK", "CDE", "RKLB"]`
- Add `EXAMPLE_START_DATE = "2025-10-01"`
- Add `EXAMPLE_END_DATE = "latest"`
- Add `EXAMPLE_BASE_CAPITAL = 100000`
- Add `EXAMPLE_PRICE_SCALE = 10000`
- Update `COLORS` to light minimal palette
- Set `PAGE_CONFIG["page_icon"] = None` (remove broken emoji)

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_dashboard_config.py -v`
Expected: PASS.

**Step 5: Commit**
```bash
git add dashboard/config.py tests/test_dashboard_config.py
git commit -m "feat: add example data settings and light theme colors"
```

---

## Task 2: Real Example Data Loader (Portfolio + Benchmark)

**Files:**
- Modify: `dashboard/backend/data_loader.py`
- Test: `tests/test_dashboard_example_data.py`

**Step 1: Write failing test**
Create `tests/test_dashboard_example_data.py`:
```python
from dashboard.backend import data_loader

def test_example_equity_from_real_data():
    equity = data_loader.load_example_equity()
    assert isinstance(equity, list)
    assert len(equity) > 10
    sample = equity[0]
    assert "datetime" in sample and "close" in sample

def test_example_positions_real_tickers():
    positions = data_loader.load_example_positions()
    symbols = {p["symbol"] for p in positions}
    assert {"MU", "SNDK", "CDE", "RKLB"}.issubset(symbols)
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_dashboard_example_data.py -v`
Expected: FAIL because example data still uses JSON and/or doesn't include new tickers.

**Step 3: Write minimal implementation**
In `dashboard/backend/data_loader.py`:
- Add a helper to read zipped daily files:
  - Path: `config.EQUITY_DATA_DIR / f"{ticker.lower()}.zip"`
  - Use `zipfile.ZipFile` + `pd.read_csv(..., header=None, names=[...])`
  - Parse date `YYYYMMDD 00:00` via `pd.to_datetime(..., format="%Y%m%d %H:%M", errors="coerce")`
  - Divide price columns by `EXAMPLE_PRICE_SCALE`
  - Return DataFrame with `datetime, open, high, low, close, volume`
- Add `build_example_portfolio_bundle()`:
  - Load all tickers
  - Filter `datetime >= EXAMPLE_START_DATE`
  - Compute common date index intersection
  - Use first common date as allocation date
  - Equal-dollar allocation from `EXAMPLE_BASE_CAPITAL`
  - Compute portfolio daily OHLC = sum(shares * OHLC)
  - Compute positions snapshot from latest date
  - Compute account/runtimeStatistics from portfolio
  - Load benchmark `SPY` and filter to same date range
- Add a small in-module cache for the bundle to avoid re-reading files.
- Update:
  - `load_example_equity()` to return bundle equity
  - `load_example_positions()` to return bundle positions
  - `load_example_account()` to return bundle account
  - `load_example_benchmarks()` to return bundle benchmark
- Keep JSON fallback if any data file missing or common range empty.

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_dashboard_example_data.py -v`
Expected: PASS.

**Step 5: Commit**
```bash
git add dashboard/backend/data_loader.py tests/test_dashboard_example_data.py
git commit -m "feat: derive example data from real equity files"
```

---

## Task 3: Wire Example Mode + Metrics Consistency

**Files:**
- Modify: `dashboard/app.py`
- Modify: `dashboard/backend/data_processor.py` (if needed for example-mode fallback)

**Step 1: Write failing test**
Add to `tests/test_dashboard_example_data.py`:
```python
from dashboard.backend import data_loader

def test_example_account_has_runtime_stats():
    account = data_loader.load_example_account()
    stats = account.get("runtimeStatistics", {})
    assert "Equity" in stats and "Holdings" in stats
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_dashboard_example_data.py::test_example_account_has_runtime_stats -v`
Expected: FAIL if account not computed yet.

**Step 3: Write minimal implementation**
- Ensure `build_example_portfolio_bundle()` populates:
  - `account` with `cash`, `holdings`, `equity`
  - `runtimeStatistics` fields consistent with `render_metrics_bar`
- In `dashboard/app.py`, ensure example mode uses these values consistently:
  - `account = load_example_account()`
  - `positions = load_example_positions()`
  - `results` uses those values
- Optional: show date range caption in example mode (small `st.caption`).

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_dashboard_example_data.py::test_example_account_has_runtime_stats -v`
Expected: PASS.

**Step 5: Commit**
```bash
git add dashboard/app.py dashboard/backend/data_loader.py tests/test_dashboard_example_data.py
git commit -m "feat: align example-mode metrics with real portfolio data"
```

---

## Task 4: Inject Global Light Minimal Theme

**Files:**
- Modify: `dashboard/frontend/components.py`
- Modify: `dashboard/app.py`
- Modify: `dashboard/config.py` (if additional tokens needed)

**Step 1: Write failing test**
Add `tests/test_dashboard_theme.py`:
```python
from dashboard.frontend import components

def test_theme_css_includes_font():
    css = components.get_global_styles()
    assert "Space Grotesk" in css
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_dashboard_theme.py -v`
Expected: FAIL because helper doesn't exist.

**Step 3: Write minimal implementation**
- In `dashboard/frontend/components.py`:
  - Add `get_global_styles()` returning a CSS string.
  - Add `inject_global_styles()` that calls `st.markdown(css, unsafe_allow_html=True)`.
- In `dashboard/app.py`, call `inject_global_styles()` immediately after `st.set_page_config`.
- CSS content:
  - Font imports: Space Grotesk + IBM Plex Sans
  - Page background `#f7f7f5`
  - Card surface `#ffffff`
  - Borders `#e6e6e2`
  - Primary text `#1a1a1a`, secondary `#6b6b6b`
  - Style Streamlit containers, tabs, buttons, radio, and tables

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_dashboard_theme.py -v`
Expected: PASS.

**Step 5: Commit**
```bash
git add dashboard/frontend/components.py dashboard/app.py tests/test_dashboard_theme.py
git commit -m "feat: add global light minimal theme"
```

---

## Task 5: Component-Level UI Refresh

**Files:**
- Modify: `dashboard/frontend/components.py`
- Modify: `dashboard/frontend/tables.py`

**Step 1: Write failing test**
Add a simple structural test:
```python
from dashboard.frontend import components

def test_metrics_bar_markup():
    html = components.build_metrics_bar_html(
        equity="$100", fees="$0", holdings="$50", net_profit="$10", psr="60%", unrealized="$5", cash="$50"
    )
    assert "metrics-card" in html
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_dashboard_theme.py::test_metrics_bar_markup -v`
Expected: FAIL.

**Step 3: Write minimal implementation**
- Refactor `render_metrics_bar` into:
  - `build_metrics_bar_html(...)` returning card-based HTML with class names.
  - `render_metrics_bar` uses `st.markdown` with that HTML.
- Update `render_server_stats_box`, `render_settings_panel`, `render_stop_button`, and `render_chart_selector`:
  - Remove emojis
  - Use minimal labels
  - Apply class hooks for CSS
- In `tables.py`, wrap `st.dataframe` with minimal title/spacing (if needed)

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_dashboard_theme.py::test_metrics_bar_markup -v`
Expected: PASS.

**Step 5: Commit**
```bash
git add dashboard/frontend/components.py dashboard/frontend/tables.py tests/test_dashboard_theme.py
git commit -m "feat: update UI components to minimal card layout"
```

---

## Task 6: Light Plotly Chart Styling

**Files:**
- Modify: `dashboard/frontend/charts.py`

**Step 1: Write failing test**
Add `tests/test_dashboard_charts.py`:
```python
from dashboard.frontend import charts

def test_plotly_template_is_light():
    assert charts.get_plotly_template_name() == "plotly_white"
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/test_dashboard_charts.py -v`
Expected: FAIL.

**Step 3: Write minimal implementation**
- Add `get_plotly_template_name()` returning `"plotly_white"`.
- Replace `template="plotly_dark"` with `template=get_plotly_template_name()`.
- Set `paper_bgcolor` and `plot_bgcolor` to transparent/white.
- Reduce gridline contrast.

**Step 4: Run test to verify it passes**
Run: `pytest tests/test_dashboard_charts.py -v`
Expected: PASS.

**Step 5: Commit**
```bash
git add dashboard/frontend/charts.py tests/test_dashboard_charts.py
git commit -m "feat: switch charts to light minimal styling"
```

---

## Manual QA Checklist

- Run: `python -m streamlit run dashboard/app.py`
- Confirm:
  - Light minimal UI applied globally
  - Metrics cards and tables are clean and readable
  - Example mode shows MU/SNDK/CDE/RKLB holdings
  - Equity curve and benchmark render without dark theme
  - No emoji artifacts

---

## Assumptions and Defaults

- Example data uses `e:\factor\lean_project\data\equity\usa\daily` via `BASE_PATH.parent / data / equity / usa / daily`.
- Prices are scaled by `EXAMPLE_PRICE_SCALE = 10000`.
- Equal-dollar allocation from `EXAMPLE_BASE_CAPITAL = 100000`.
- Example orders/insights/logs remain placeholder JSONs unless you want them derived from real trading logs.
- If real data missing or overlap is empty, example JSON fallback is used.

---

## Execution Options

Once you want implementation:
1. **Subagent-Driven (this session)** - I'll use `superpowers:subagent-driven-development` to implement task-by-task.
2. **Parallel Session** - Open a new session and use `superpowers:executing-plans`.
