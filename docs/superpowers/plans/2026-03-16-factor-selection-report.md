# Factor Selection Report Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deployment-scoped factor-selection artifact and append deterministic per-factor stock-selection tables plus 20-day holding timeseries to the daily report.

**Architecture:** Build a new producer contract under `reports/factor_selection_daily`, generated from Lean strategy state during both normal finalize and warmup/recovery rerun preparation. Keep the report consumer host-driven: load the new JSON strictly by date and append markdown tables without sending the data to the LLM.

**Tech Stack:** Python, Lean strategy runtime, QuantConnect ObjectStore persistence, pytest, markdown report generation.

---

## File Structure

**Create**
- `S_alphasage/factor_selection_report.py`
- `tests/test_s_alphasage_factor_selection_report.py`

**Modify**
- `S_alphasage/main.py`
- `S_alphasage/cache_persistence.py`
- `scripts/daily_report_tools.py`
- `scripts/generate_daily_report.py`
- `tests/test_s_alphasage_cma_persistence.py`
- `tests/test_daily_report_tools.py`
- `tests/test_daily_report_collector.py`

**Responsibilities**
- `S_alphasage/factor_selection_report.py`: build factor-selection payloads from factor panels and metadata, including latest top/bottom 10 and trailing 20-day history.
- `S_alphasage/main.py`: orchestrate persistence from both normal finalize and warmup/recovery preview paths.
- `S_alphasage/cache_persistence.py`: persist `reports/factor_selection_daily/{date}.json` and `latest.json`.
- `scripts/daily_report_tools.py`: load the new artifact under strict-date rules.
- `scripts/generate_daily_report.py`: render deterministic factor-selection appendix blocks.
- `tests/...`: enforce red-green coverage for the producer, persistence, consumer, and final markdown output.

## Chunk 1: Producer Contract

### Task 1: Add Failing Producer Tests

**Files:**
- Create: `tests/test_s_alphasage_factor_selection_report.py`
- Modify: `tests/test_s_alphasage_cma_persistence.py`

- [ ] **Step 1: Write a failing unit test for latest CMA factor selections**

Add a test that builds a small CMA factor panel and metadata, then expects a payload row with:
- `name`
- `latest.top`
- `latest.bottom`
- `history_20d`

- [ ] **Step 2: Run the targeted test to verify it fails**

Run: `pytest tests/test_s_alphasage_factor_selection_report.py -q`
Expected: FAIL because `factor_selection_report.py` does not exist yet.

- [ ] **Step 3: Write a failing unit test for actionable factor selections**

Add a second test that uses an actionable factor panel and expects the same `latest` and `history_20d` structure.

- [ ] **Step 4: Run the targeted test to verify it fails for the right reason**

Run: `pytest tests/test_s_alphasage_factor_selection_report.py -q`
Expected: FAIL with missing module or missing builder functions.

- [ ] **Step 5: Write a failing persistence test for the new ObjectStore prefix**

Extend `tests/test_s_alphasage_cma_persistence.py` to assert:
- `reports/factor_selection_daily/{date}.json` is saved
- `reports/factor_selection_daily/latest.json` is saved

- [ ] **Step 6: Run the persistence test to verify it fails**

Run: `pytest tests/test_s_alphasage_cma_persistence.py -q`
Expected: FAIL because the new persistence API does not exist yet.

### Task 2: Implement the Producer Builder

**Files:**
- Create: `S_alphasage/factor_selection_report.py`

- [ ] **Step 1: Implement deterministic ranking helpers**

Add helpers that:
- coerce numeric rows
- rank by descending score for `top`
- rank by ascending score for `bottom`
- break ties by symbol name

- [ ] **Step 2: Implement per-factor history extraction**

Add helpers that:
- read a factor panel `DataFrame`
- take the trailing 20 valid dates
- build `top 10` and `bottom 10` rows for each date

- [ ] **Step 3: Implement the top-level payload builder**

Add a builder that returns:
- `as_of_date`
- `data_as_of_date`
- `source_mode`
- `lookback_days`
- `selection_size`
- `cma_factors`
- `actionable_factors`

- [ ] **Step 4: Run the producer test file to verify it passes**

Run: `pytest tests/test_s_alphasage_factor_selection_report.py -q`
Expected: PASS

## Chunk 2: Lean Integration

### Task 3: Wire Persistence Into Lean Orchestration

**Files:**
- Modify: `S_alphasage/cache_persistence.py`
- Modify: `S_alphasage/main.py`

- [ ] **Step 1: Add the new cache persistence prefix and save method**

In `S_alphasage/cache_persistence.py`, add:
- `FACTOR_SELECTION_DAILY_PREFIX`
- `save_factor_selection_daily_report(...)`

- [ ] **Step 2: Run the persistence test to verify it now passes**

Run: `pytest tests/test_s_alphasage_cma_persistence.py -q`
Expected: PASS

- [ ] **Step 3: Add a failing integration test for main-path persistence**

In `tests/test_s_alphasage_factor_selection_report.py`, add a small orchestration-focused test or helper-driven test that expects the builder to be invoked for:
- normal structured report persistence
- warmup/recovery preview persistence with a non-runtime `report_date`

- [ ] **Step 4: Run the new integration-focused test to verify it fails**

Run: `pytest tests/test_s_alphasage_factor_selection_report.py -q`
Expected: FAIL because `main.py` does not call the new persistence path yet.

- [ ] **Step 5: Implement `_persist_factor_selection_daily_report(current_date)` in `main.py`**

Use strategy state to:
- gather CMA factor metadata and panels
- gather actionable factor metadata and panels
- choose `source_mode`
- persist the payload through `cache_manager`

- [ ] **Step 6: Call the new persistence function from `_persist_structured_reports(current_date)`**

Keep ordering:
1. timing daily
2. factor selection daily
3. market summary daily/weekly

- [ ] **Step 7: Preserve warmup/recovery behavior**

Ensure the existing preview/recovery flow persists the new artifact using the effective `report_date`, not blindly `runtime_date`.

- [ ] **Step 8: Re-run the producer and persistence tests**

Run: `pytest tests/test_s_alphasage_factor_selection_report.py tests/test_s_alphasage_cma_persistence.py -q`
Expected: PASS

## Chunk 3: Report Consumer

### Task 4: Add Failing Consumer Tests

**Files:**
- Modify: `tests/test_daily_report_tools.py`

- [ ] **Step 1: Add a test fixture that writes `reports/factor_selection_daily/{date}.json`**

Use the mock deployment path and store at least:
- one CMA factor
- one actionable factor
- two history dates

- [ ] **Step 2: Add a failing test for exact-date loading**

Assert the tool returns:
- `ok == True`
- strict date alignment
- the expected factor names and latest picks

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_daily_report_tools.py -q`
Expected: FAIL because the loader does not exist yet.

### Task 5: Implement Consumer Loading

**Files:**
- Modify: `scripts/daily_report_tools.py`

- [ ] **Step 1: Add strict-date resolution for `factor_selection_daily`**

Follow the same exact-date and optional latest fallback rules already used for other structured report sections.

- [ ] **Step 2: Add a small read-only helper or exported tool-facing function**

Return a structured envelope that includes:
- data
- warnings
- diagnostics
- source path

- [ ] **Step 3: Re-run the daily report tools test file**

Run: `pytest tests/test_daily_report_tools.py -q`
Expected: PASS

## Chunk 4: Markdown Appendix

### Task 6: Add Failing Appendix Tests

**Files:**
- Modify: `tests/test_daily_report_collector.py`

- [ ] **Step 1: Add a fixture writer for the factor-selection artifact**

Store data with:
- one `alpha_*` factor
- one `actionable_alpha_*` factor
- trailing history rows

- [ ] **Step 2: Add a failing test for markdown appendix rendering**

Assert the final report markdown contains:
- `## Factor Selection Tables`
- `### CMA Factors`
- `### Actionable Factors`
- a factor heading
- at least one long-form history row

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_daily_report_collector.py -q`
Expected: FAIL because `generate_daily_report.py` does not yet render this appendix.

### Task 7: Implement Markdown Rendering

**Files:**
- Modify: `scripts/generate_daily_report.py`

- [ ] **Step 1: Add a loader for `factor_selection_daily`**

Load the artifact for the authoritative report date under the same strict-date assumptions as the rest of the report.

- [ ] **Step 2: Implement markdown render helpers**

Render:
- latest top table
- latest bottom table
- long-form 20-day timeseries table

- [ ] **Step 3: Append the new deterministic section to the final markdown**

Keep the new section host-side only and do not inject it into the writer prompt or fact sheet.

- [ ] **Step 4: Re-run the report collector test file**

Run: `pytest tests/test_daily_report_collector.py -q`
Expected: PASS

## Chunk 5: End-To-End Verification

### Task 8: Run Focused Verification

**Files:**
- Verify only

- [ ] **Step 1: Run the focused test suite**

Run: `pytest tests/test_s_alphasage_factor_selection_report.py tests/test_s_alphasage_cma_persistence.py tests/test_daily_report_tools.py tests/test_daily_report_collector.py -q`
Expected: PASS

- [ ] **Step 2: Run a dry report build if local fixtures or live storage are available**

Run: `python scripts/generate_daily_report.py --date today --dry-run`
Expected: exit code `0` and no regression in report generation flow.

- [ ] **Step 3: Inspect the generated markdown**

Verify it includes:
- factor selection section
- per-factor top/bottom 10 tables
- 20-day long-form history rows

- [ ] **Step 4: Commit implementation work in logical units**

Use non-interactive commits after each coherent chunk or at minimum after the full verified implementation.
