# Factor Selection Report Design

**Date:** 2026-03-16

**Goal**

Add a deterministic factor-selection report artifact so the daily end report can show:
- each CMA factor's latest top 10 and bottom 10 picks
- each actionable factor's latest top 10 and bottom 10 picks
- each factor's recent 20-trading-day daily holding timeseries

This data must be appended host-side to the markdown report and must not be sent to the LLM.

## Why

The current report discusses recommended factors, but it does not expose the concrete stock selections behind those factors. That makes it hard to compare narrative claims with actual per-factor picks, especially across recent days. The new artifact makes the report directly auditable against the strategy's structured state.

## Scope

In scope:
- new deployment-scoped structured JSON artifact for factor selection
- generation during normal 15:55 finalize flow
- generation during deployment preview, recovery, and rerun preparation after warmup/recovery completes
- host-side report appendix rendering for latest picks and 20-day timeseries
- strict date-aligned loading in the report script

Out of scope:
- changing LLM prompts to reason over these tables
- full-universe per-factor rankings in the report
- storing more than top 10 and bottom 10 per factor
- storing more than the most recent 20 trading days of per-factor history

## Architecture

The design introduces a new deployment-scoped artifact:

- `reports/factor_selection_daily/{date}.json`

This artifact is built from live strategy state after `generate_insights_on_schedule(...)` has prepared the latest factor values and score snapshots. It is persisted from the same orchestration path that already writes `timing_daily` and market summaries, so the report consumer reads one deterministic source of truth that matches the strategy's current finalized or preview state.

The report generator remains host-driven. It loads the new JSON, renders markdown tables, and appends them to the final report without involving the LLM.

## Producer Responsibilities

### Main Orchestration

`S_alphasage/main.py` remains the orchestration entry point.

`_persist_structured_reports(current_date)` will be extended to also call:

- `_persist_factor_selection_daily_report(current_date)`

This call should happen after `_persist_timing_daily_report(current_date)` so that timing preview persistence has already refreshed any stale actionable timing state before factor-selection persistence runs.

### Selection Report Builder

Add a focused helper module:

- `S_alphasage/factor_selection_report.py`

Responsibilities:
- build the deployment-scoped factor selection payload
- rank daily factor panels into top 10 and bottom 10 selections
- build 20-day history rows from cached factor panels
- keep producer logic out of `main.py`

### Persistence

Extend `S_alphasage/cache_persistence.py` with:

- `FACTOR_SELECTION_DAILY_PREFIX = "reports/factor_selection_daily"`
- `save_factor_selection_daily_report(as_of_date, report_payload)`

Persistence behavior should match the existing daily report pattern:
- save `{date}.json`
- save `latest.json`

## Data Sources

### CMA Factors

Source state:
- `alpha_model` latest CMA factor definitions and metadata
- current factor values prepared during the run
- `alpha_model.factor_cache` for recent history

For each CMA factor:
- use the latest daily cross-sectional factor values to compute `top 10` and `bottom 10`
- use cached daily factor panels to compute the most recent 20 trading days of `top 10` and `bottom 10`
- carry through metadata already known to the strategy:
  - `name`
  - `expr`
  - `expr_hash`
  - `effective_direction`

### Actionable Factors

Source state:
- actionable factor definitions from `alpha_model`
- current actionable factor values
- `alpha_model.actionable_factor_cache` for recent history

For each actionable factor:
- compute latest `top 10` and `bottom 10`
- compute recent 20-trading-day `top 10` and `bottom 10` history
- include metadata:
  - `name`
  - `expr`
  - `expr_hash`

The current actionable timing summary remains separate. This new artifact is not a timing summary; it is a factor-level selection surface for report auditing.

## Structured Contract

Path:

- `reports/factor_selection_daily/{date}.json`

Schema:

```json
{
  "as_of_date": "2026-03-16",
  "data_as_of_date": "2026-03-16",
  "source_mode": "finalize",
  "lookback_days": 20,
  "selection_size": 10,
  "cma_factors": [
    {
      "name": "alpha_0",
      "expr": "Rank(...)",
      "expr_hash": "abc123",
      "effective_direction": 1,
      "latest": {
        "top": [
          { "symbol": "AAPL", "score": 1.234567, "rank": 1 }
        ],
        "bottom": [
          { "symbol": "NVDA", "score": -0.456789, "rank": 1 }
        ]
      },
      "history_20d": [
        {
          "date": "2026-03-16",
          "top": [
            { "symbol": "AAPL", "score": 1.234567, "rank": 1 }
          ],
          "bottom": [
            { "symbol": "NVDA", "score": -0.456789, "rank": 1 }
          ]
        }
      ]
    }
  ],
  "actionable_factors": [
    {
      "name": "actionable_alpha_4",
      "expr": "TsRelStrength(...)",
      "expr_hash": "def456",
      "latest": {
        "top": [
          { "symbol": "MSFT", "score": 0.987654, "rank": 1 }
        ],
        "bottom": [
          { "symbol": "META", "score": -0.654321, "rank": 1 }
        ]
      },
      "history_20d": [
        {
          "date": "2026-03-16",
          "top": [
            { "symbol": "MSFT", "score": 0.987654, "rank": 1 }
          ],
          "bottom": [
            { "symbol": "META", "score": -0.654321, "rank": 1 }
          ]
        }
      ]
    }
  ]
}
```

Contract rules:
- `selection_size` is fixed at `10`
- `lookback_days` is fixed at `20`
- `history_20d` is a trailing trading-day history, newest date included
- if fewer than 20 valid dates are available, emit the available history and do not synthesize rows
- ranking is deterministic and uses symbol name as the stable tie-break after score

## Source Modes

`source_mode` distinguishes how the artifact was produced:

- `finalize`
  - produced during the normal `BeforeMarketClose(5)` finalize flow
- `warmup_preview`
  - produced after deployment preview / recovery run when preparing immediately available report artifacts
- `warmup_rerun`
  - produced after a rerun-preparation path following warmup/recovery completion

The artifact date should remain the effective report date, not necessarily the runtime date. For overnight or pre-market replay, if the recovered finalized session is the previous trading day, the report is written under that previous day.

## Runtime Flow

### Normal Finalize

1. `scheduled_signal_check()` runs near 15:55.
2. `alpha_model.generate_insights_on_schedule(...)` computes current factor state.
3. `_persist_structured_reports(current_date)` runs.
4. `timing_daily` is refreshed first.
5. `factor_selection_daily` is built and persisted.
6. report script can consume the artifact after the run.

### Deployment Preview / Recovery

1. recovery/history backfill populates caches
2. preview pipeline runs with deferred insight emission
3. structured artifacts are persisted immediately after preview run
4. factor-selection artifact is written for the effective report date so the report script can run without waiting for the next live finalize

### Warmup Finish / Rerun Preparation

When warmup/recovery completes and the system prepares a rerun, the factor-selection artifact must already exist for the effective report date. The rerun path should therefore call the same persistence function rather than rebuilding report-only logic separately.

## Report Consumer Design

### Read Path

Add a read-only loader in `scripts/daily_report_tools.py` for:

- `reports/factor_selection_daily/{date}.json`

The loader should follow the same strict-date behavior as other report artifacts:
- exact-date first
- optionally `latest.json` only when the caller explicitly allows stale behavior
- include source path metadata for diagnostics

### Rendering Path

Extend `scripts/generate_daily_report.py` to append a new deterministic appendix section:

- `## Factor Selection Tables`
- `### CMA Factors`
- `### Actionable Factors`

For each factor render:
- `#### <factor name>`
- one table for latest `top 10`
- one table for latest `bottom 10`
- one long-form timeseries table covering the recent 20 trading days

Suggested timeseries format:

| Date | Side | Symbols |
| --- | --- | --- |
| 2026-03-16 | Top | AAPL, MSFT, ... |
| 2026-03-16 | Bottom | NVDA, META, ... |

This long format is preferred over a wide matrix because it stays readable in markdown and HTML and scales across many factors.

## Error Handling

Producer rules:
- if factor metadata is partially unavailable, emit the factor row with best-effort metadata and do not block the report
- if a factor has no valid latest values, skip that factor and record the omission in debug logs
- if history contains fewer than 20 valid dates, emit the available subset

Consumer rules:
- if the artifact is missing, the report still renders without the new appendix
- if the artifact exists but schema validation fails, the report should omit the appendix and expose a diagnostics note rather than crash
- do not silently mix dates; the appendix date must align with the authoritative report date

## Testing Strategy

### Producer Tests

Add or extend tests to verify:
- factor-selection payload contains `cma_factors` and `actionable_factors`
- latest top/bottom 10 rows are present
- `history_20d` rows are present and date-ordered
- warmup preview / recovery flow also persists the artifact

### Persistence Tests

Verify:
- `save_factor_selection_daily_report(...)` writes `{date}.json`
- `latest.json` is updated
- deployment-scoped prefix matches the current active deployment layout

### Consumer Tests

Verify:
- exact-date load succeeds
- stale or missing data follows existing strict-date rules
- appendix markdown includes the section header, factor names, and timeseries rows

## Invariants

- keep the LLM blind to the new deterministic tables
- keep the artifact deployment-scoped and date-aligned
- keep selection size fixed at `10`
- keep history window fixed at `20` trading days
- prepare the artifact during warmup/recovery rerun preparation, not only at the regular finalize event
