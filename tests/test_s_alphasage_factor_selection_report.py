from pathlib import Path
import sys
import types

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "S_alphasage"))

if "AlgorithmImports" not in sys.modules:
    sys.modules["AlgorithmImports"] = types.ModuleType("AlgorithmImports")

from factor_selection_report import build_factor_selection_daily_report


def test_build_factor_selection_daily_report_includes_latest_and_history():
    dates = pd.date_range("2026-03-02", periods=4, freq="B")
    cma_panels = {
        "alpha_0": pd.DataFrame(
            [
                [1.0, 2.0, -1.0],
                [1.5, 3.0, -0.5],
                [2.0, 4.0, -2.0],
                [3.0, 5.0, -3.0],
            ],
            index=dates,
            columns=["AAPL", "MSFT", "NVDA"],
        )
    }
    actionable_panels = {
        "actionable_alpha_4": pd.DataFrame(
            [
                [0.1, 0.6, -0.2],
                [0.2, 0.7, -0.1],
                [0.3, 0.8, -0.4],
                [0.4, 0.9, -0.5],
            ],
            index=dates,
            columns=["AAPL", "MSFT", "NVDA"],
        )
    }

    report = build_factor_selection_daily_report(
        as_of_date="2026-03-05",
        data_as_of_date="2026-03-05",
        source_mode="warmup_preview",
        cma_factor_panels=cma_panels,
        cma_factor_context=[
            {
                "name": "alpha_0",
                "expr": "Rank(close)",
                "expr_hash": "abc123",
                "effective_direction": 1,
            }
        ],
        actionable_factor_panels=actionable_panels,
        actionable_factor_context=[
            {
                "name": "actionable_alpha_4",
                "expr": "TsRank(close, 5)",
                "expr_hash": "def456",
            }
        ],
        selection_size=2,
        lookback_days=3,
    )

    assert report["source_mode"] == "warmup_preview"
    assert report["selection_size"] == 2
    assert report["lookback_days"] == 3

    cma_row = report["cma_factors"][0]
    assert cma_row["name"] == "alpha_0"
    assert cma_row["latest"]["top"][0]["symbol"] == "MSFT"
    assert cma_row["latest"]["bottom"][0]["symbol"] == "NVDA"
    assert [row["date"] for row in cma_row["history_20d"]] == [
        "2026-03-03",
        "2026-03-04",
        "2026-03-05",
    ]

    actionable_row = report["actionable_factors"][0]
    assert actionable_row["name"] == "actionable_alpha_4"
    assert actionable_row["latest"]["top"][0]["symbol"] == "MSFT"
    assert actionable_row["latest"]["bottom"][0]["symbol"] == "NVDA"
    assert len(actionable_row["history_20d"]) == 3


def test_build_factor_selection_daily_report_dedupes_same_day_history_and_uses_latest_data_date():
    dates = pd.to_datetime(
        [
            "2026-03-12 14:00:00",
            "2026-03-12 15:55:00",
            "2026-03-13 14:00:00",
            "2026-03-13 15:55:00",
        ]
    )
    cma_panels = {
        "alpha_0": pd.DataFrame(
            [
                [1.0, 2.0, -1.0],
                [3.0, 1.0, -2.0],
                [1.5, 2.5, -1.5],
                [0.5, 4.0, -3.0],
            ],
            index=dates,
            columns=["AAPL", "MSFT", "NVDA"],
        )
    }

    report = build_factor_selection_daily_report(
        as_of_date="2026-03-14",
        data_as_of_date="2026-03-14",
        source_mode="finalize",
        cma_factor_panels=cma_panels,
        cma_factor_context=[
            {
                "name": "alpha_0",
                "expr": "Rank(close)",
                "expr_hash": "abc123",
                "effective_direction": 1,
            }
        ],
        actionable_factor_panels={},
        actionable_factor_context=[],
        selection_size=2,
        lookback_days=5,
    )

    assert report["data_as_of_date"] == "2026-03-13"
    history = report["cma_factors"][0]["history_20d"]
    assert [row["date"] for row in history] == ["2026-03-12", "2026-03-13"]
    assert history[0]["top"][0]["symbol"] == "AAPL"
    assert history[1]["top"][0]["symbol"] == "MSFT"
