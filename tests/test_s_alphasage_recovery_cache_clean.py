from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sys
import types

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "S_alphasage"))
sys.modules.pop("main", None)

mod = sys.modules.get("AlgorithmImports")
if mod is None:
    mod = types.ModuleType("AlgorithmImports")
    sys.modules["AlgorithmImports"] = mod


class QCAlgorithm:
    pass


class Slice:
    pass


class SecurityChanges:
    pass


mod.QCAlgorithm = QCAlgorithm
mod.Slice = Slice
mod.SecurityChanges = SecurityChanges


def _stub_module(module_name: str, **attrs) -> None:
    stub = types.ModuleType(module_name)
    for name, value in attrs.items():
        setattr(stub, name, value)
    sys.modules[module_name] = stub


_stub_module("universe_selection", AlphaSAGEUniverseSelectionModel=type("AlphaSAGEUniverseSelectionModel", (), {}))
_stub_module("alpha_model", AlphaSAGEAlphaModel=type("AlphaSAGEAlphaModel", (), {}))
_stub_module(
    "risk_management",
    TimingExitRiskModel=type("TimingExitRiskModel", (), {}),
    ActionableFactorTimingRisk=type("ActionableFactorTimingRisk", (), {}),
)
_stub_module("portfolio_construction", AlphaSAGEPortfolioConstructionModel=type("AlphaSAGEPortfolioConstructionModel", (), {}))
_stub_module("minute_consolidator", IntradayFeatureConsolidator=type("IntradayFeatureConsolidator", (), {}))
_stub_module("early_close", EarlyCloseDetector=type("EarlyCloseDetector", (), {}))
_stub_module("cache_persistence", CachePersistenceManager=type("CachePersistenceManager", (), {}))
_stub_module("recovery_history", request_history_with_symbol_fallback=lambda *args, **kwargs: (None, False))

import main as main_module
from main import AlphaSAGE_MultiFactors


for _name in [
    "universe_selection",
    "alpha_model",
    "risk_management",
    "portfolio_construction",
    "minute_consolidator",
    "early_close",
    "cache_persistence",
    "recovery_history",
]:
    sys.modules.pop(_name, None)


def _make_factor_cache(prefix: str):
    return types.SimpleNamespace(
        factor_values={
            f"{prefix}_keep": pd.DataFrame(
                {"AAPL": [1.0, 2.0, 3.0]},
                index=pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
            ),
            f"{prefix}_drop": pd.DataFrame(
                {"AAPL": [9.0]},
                index=pd.to_datetime(["2025-01-03"]),
            ),
        },
        daily_ic={
            f"{prefix}_keep": pd.Series(
                [0.1, 0.2],
                index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
            ),
            f"{prefix}_drop": pd.Series(
                [0.8],
                index=pd.to_datetime(["2025-01-03"]),
            ),
        },
        daily_ls_returns={
            f"{prefix}_keep": pd.Series(
                [0.01, 0.02],
                index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
            ),
            f"{prefix}_drop": pd.Series(
                [0.05],
                index=pd.to_datetime(["2025-01-03"]),
            ),
        },
        last_date={
            f"{prefix}_keep": pd.Timestamp("2025-01-03"),
            f"{prefix}_drop": pd.Timestamp("2025-01-03"),
        },
    )


def test_clip_backtest_caches_to_date_removes_future_rows_for_all_caches():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)

    market_cache = types.SimpleNamespace(
        frames={
            "close": pd.DataFrame(
                {"AAPL": [100.0, 101.0, 102.0]},
                index=pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
            ),
            "volume": pd.DataFrame(
                {"AAPL": [1000.0, 1100.0, 1200.0]},
                index=pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
            ),
        }
    )
    feature_history = types.SimpleNamespace(
        history={
            "vol_fft": pd.DataFrame(
                {"AAPL": [0.5, 0.6]},
                index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
            )
        }
    )

    algo.alpha_model = types.SimpleNamespace(
        market_cache=market_cache,
        factor_cache=_make_factor_cache("base"),
        actionable_factor_cache=_make_factor_cache("timing"),
    )
    algo.intraday_consolidator = types.SimpleNamespace(feature_history=feature_history)

    algo._clip_backtest_caches_to_date(date(2025, 1, 3))

    assert list(market_cache.frames["close"].index) == [
        pd.Timestamp("2025-01-01"),
        pd.Timestamp("2025-01-02"),
    ]
    assert list(market_cache.frames["volume"].index) == [
        pd.Timestamp("2025-01-01"),
        pd.Timestamp("2025-01-02"),
    ]

    for cache_name in ("factor_cache", "actionable_factor_cache"):
        cache = getattr(algo.alpha_model, cache_name)
        keep_key = [k for k in cache.factor_values if k.endswith("_keep")][0]
        drop_key = [k for k in cache.last_date if k.endswith("_drop")]
        assert list(cache.factor_values[keep_key].index) == [
            pd.Timestamp("2025-01-01"),
            pd.Timestamp("2025-01-02"),
        ]
        assert not drop_key
        assert len(cache.daily_ic) == 1
        assert len(cache.daily_ls_returns) == 1

    assert list(feature_history.history["vol_fft"].index) == [pd.Timestamp("2025-01-02")]


def test_trigger_timing_preview_persistence_calls_manage_risk_once():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    calls = []
    logs = []

    class _TimingModel:
        def ManageRisk(self, algorithm, targets):
            calls.append((algorithm, list(targets)))
            return ["preview_target"]

    algo.actionable_timing_model = _TimingModel()
    algo.Debug = lambda message: logs.append(str(message))

    algo._trigger_timing_preview_persistence()

    assert len(calls) == 1
    assert calls[0][1] == []
    assert any("Timing preview evaluation completed" in line for line in logs)


def test_preview_timing_override_timestamp_skips_previous_session_replay_for_post_market_restart():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    algo.Time = pd.Timestamp("2026-03-18 18:21:02")
    algo._is_post_market = True
    algo.alpha_model = types.SimpleNamespace(
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: pd.DataFrame(
                {"AAPL": [101.0]},
                index=pd.to_datetime(["2026-03-17 15:55:00"]),
            )
            if key == "close"
            else default
        )
    )

    forced_time = algo._preview_timing_override_timestamp(date(2026, 3, 17))

    assert forced_time is None


def test_persist_structured_reports_saves_current_timing_summary():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    logs = []
    timing_saves = []

    class _CacheManager:
        def save_timing_daily_report(self, as_of_date, payload):
            timing_saves.append((as_of_date, payload))

    algo.cache_manager = _CacheManager()
    algo.actionable_timing_model = types.SimpleNamespace(
        last_timing_summary={
            "as_of_date": "2025-01-03",
            "score_source": "actionable_composite",
            "fallback_reason": "",
            "bottom_n": 8,
            "total_scored": 68,
            "exits": [],
            "restores": [],
            "blocked": [],
            "deferred_restores": [],
        }
    )
    algo.Debug = lambda message: logs.append(str(message))
    algo._compute_market_summary = lambda window_days, as_of_date: None

    algo._persist_structured_reports(date(2025, 1, 3))

    assert len(timing_saves) == 1
    saved_date, payload = timing_saves[0]
    assert saved_date == date(2025, 1, 3)
    assert payload["as_of_date"] == "2025-01-03"
    assert payload["score_source"] == "actionable_composite"


def test_save_runtime_caches_passes_overwrite_dates_to_cache_manager():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    calls = []

    class _CacheManager:
        def save_all(self, alpha_model, consolidator, overwrite_dates=None):
            calls.append((alpha_model, consolidator, overwrite_dates))

    algo.cache_manager = _CacheManager()
    algo.alpha_model = object()
    algo.intraday_consolidator = object()
    algo.LiveMode = True
    algo.backtest_fast_recovery = False

    target_date = date(2026, 3, 19)
    algo._save_runtime_caches(overwrite_dates=[target_date], live_only=True)

    assert calls == [
        (algo.alpha_model, algo.intraday_consolidator, [target_date]),
    ]


def test_persist_structured_reports_saves_factor_selection_daily():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    factor_selection_saves = []

    class _CacheManager:
        def save_timing_daily_report(self, as_of_date, payload):
            return None

        def save_factor_selection_daily_report(self, as_of_date, payload):
            factor_selection_saves.append((as_of_date, payload))

    algo.cache_manager = _CacheManager()
    algo.actionable_timing_model = types.SimpleNamespace(
        last_timing_summary={
            "as_of_date": "2025-01-03",
            "data_as_of_date": "2025-01-03",
            "score_source": "actionable_composite",
            "fallback_reason": "",
            "bottom_n": 8,
            "total_scored": 68,
            "exits": [],
            "restores": [],
            "blocked": [],
            "deferred_restores": [],
        }
    )
    algo.alpha_model = types.SimpleNamespace(
        factor_cache=types.SimpleNamespace(
            factor_values={
                "alpha_0": pd.DataFrame(
                    {"AAPL": [1.0, 2.0], "MSFT": [2.0, 3.0]},
                    index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
                )
            }
        ),
        actionable_factor_cache=types.SimpleNamespace(
            factor_values={
                "actionable_alpha_4": pd.DataFrame(
                    {"AAPL": [0.1, 0.2], "MSFT": [0.3, 0.4]},
                    index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
                )
            }
        ),
        factor_defs=[{"name": "alpha_0", "expr": "Rank(close)"}],
        actionable_factor_defs=[{"name": "actionable_alpha_4", "expr": "TsRank(close, 5)"}],
    )
    algo.Debug = lambda message: None
    algo._compute_market_summary = lambda window_days, as_of_date: None

    algo._persist_structured_reports(date(2025, 1, 3))

    assert len(factor_selection_saves) == 1
    saved_date, payload = factor_selection_saves[0]
    assert saved_date == date(2025, 1, 3)
    assert payload["as_of_date"] == "2025-01-03"
    assert payload["source_mode"] == "finalize"
    assert payload["cma_factors"][0]["name"] == "alpha_0"
    assert payload["actionable_factors"][0]["name"] == "actionable_alpha_4"


def test_persist_structured_reports_preserves_explicit_warmup_source_mode():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    factor_selection_saves = []

    class _CacheManager:
        def save_timing_daily_report(self, as_of_date, payload):
            return None

        def save_factor_selection_daily_report(self, as_of_date, payload):
            factor_selection_saves.append((as_of_date, payload))

    algo.cache_manager = _CacheManager()
    algo.actionable_timing_model = types.SimpleNamespace(
        last_timing_summary={
            "as_of_date": "2025-01-03",
            "data_as_of_date": "2025-01-03",
            "score_source": "actionable_composite",
            "fallback_reason": "",
            "bottom_n": 8,
            "total_scored": 68,
            "exits": [],
            "restores": [],
            "blocked": [],
            "deferred_restores": [],
        }
    )
    algo.alpha_model = types.SimpleNamespace(
        factor_cache=types.SimpleNamespace(
            factor_values={
                "alpha_0": pd.DataFrame(
                    {"AAPL": [1.0], "MSFT": [2.0]},
                    index=pd.to_datetime(["2025-01-03"]),
                )
            }
        ),
        actionable_factor_cache=types.SimpleNamespace(
            factor_values={
                "actionable_alpha_4": pd.DataFrame(
                    {"AAPL": [0.1], "MSFT": [0.3]},
                    index=pd.to_datetime(["2025-01-03"]),
                )
            }
        ),
        factor_defs=[{"name": "alpha_0", "expr": "Rank(close)"}],
        actionable_factor_defs=[{"name": "actionable_alpha_4", "expr": "TsRank(close, 5)"}],
    )
    algo.Debug = lambda message: None
    algo._compute_market_summary = lambda window_days, as_of_date: None

    algo._persist_structured_reports(date(2025, 1, 3), source_mode="warmup_preview")

    assert factor_selection_saves[0][1]["source_mode"] == "warmup_preview"


def test_persist_structured_reports_backfills_timing_summary_when_missing():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    logs = []
    timing_saves = []
    calls = []

    class _CacheManager:
        def save_timing_daily_report(self, as_of_date, payload):
            timing_saves.append((as_of_date, payload))

    class _TimingModel:
        def __init__(self):
            self.last_timing_summary = None

        def ManageRisk(self, algorithm, targets):
            calls.append(list(targets))
            self.last_timing_summary = {
                "as_of_date": "2025-01-03",
                "score_source": "actionable_composite",
                "fallback_reason": "",
                "bottom_n": 8,
                "total_scored": 68,
                "exits": [],
                "restores": [],
                "blocked": [],
                "deferred_restores": [],
            }
            return []

    algo.cache_manager = _CacheManager()
    algo.actionable_timing_model = _TimingModel()
    algo.Debug = lambda message: logs.append(str(message))
    algo._compute_market_summary = lambda window_days, as_of_date: None

    algo._persist_structured_reports(date(2025, 1, 3))

    assert calls == [[]]
    assert len(timing_saves) == 1
    assert timing_saves[0][1]["as_of_date"] == "2025-01-03"


def test_persist_structured_reports_backfills_previous_session_timing_summary_overnight_without_mutating_time():
    class _ReadOnlyTimeAlgorithm(AlphaSAGE_MultiFactors):
        @property
        def Time(self):
            return self._time

    algo = _ReadOnlyTimeAlgorithm.__new__(_ReadOnlyTimeAlgorithm)
    logs = []
    timing_saves = []
    observed_times = []

    class _CacheManager:
        def save_timing_daily_report(self, as_of_date, payload):
            timing_saves.append((as_of_date, payload))

    class _TimingModel:
        def __init__(self):
            self.last_timing_summary = None

        def ManageRisk(self, algorithm, targets, as_of_time=None):
            effective_time = as_of_time or algorithm.Time
            observed_times.append(effective_time)
            self.last_timing_summary = {
                "as_of_date": str(effective_time.date()),
                "data_as_of_date": str(effective_time.date()),
                "score_source": "actionable_composite",
                "fallback_reason": "",
                "bottom_n": 8,
                "total_scored": 68,
                "exits": [],
                "restores": [],
                "blocked": [],
                "deferred_restores": [],
            }
            return []

    algo.cache_manager = _CacheManager()
    algo.actionable_timing_model = _TimingModel()
    algo._time = datetime(2025, 1, 4, 1, 49)
    algo.alpha_model = types.SimpleNamespace(
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: pd.DataFrame(
                {"AAPL": [100.0]},
                index=pd.to_datetime(["2025-01-03 15:55:00"]),
            ) if key == "close" else default
        )
    )
    algo.Debug = lambda message: logs.append(str(message))
    algo._compute_market_summary = lambda window_days, as_of_date: None

    algo._persist_structured_reports(date(2025, 1, 3))

    assert observed_times == [datetime(2025, 1, 3, 15, 56)]
    assert algo.Time == datetime(2025, 1, 4, 1, 49)
    assert len(timing_saves) == 1
    assert timing_saves[0][0] == date(2025, 1, 3)
    assert timing_saves[0][1]["as_of_date"] == "2025-01-03"


def test_build_runtime_validation_snapshot_reports_finalize_ready_state():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    algo.Time = pd.Timestamp("2025-01-03 15:55:00")
    algo.alpha_model = types.SimpleNamespace(
        last_score_snapshot={"date": "2025-01-03", "scores": {"AAPL": 0.8, "MSFT": 0.2}},
        last_score_mode="full",
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: pd.DataFrame(
                {"AAPL": [100.0], "MSFT": [200.0]},
                index=pd.to_datetime(["2025-01-03 15:55:00"]),
            ) if key == "close" else default
        ),
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        last_timing_summary={
            "as_of_date": "2025-01-03",
            "score_source": "actionable_composite",
        }
    )
    algo.intraday_consolidator = types.SimpleNamespace(
        _bar_counts={"AAPL": 385, "MSFT": 385, "NVDA": 0}
    )
    algo._pending_insights_symbols = ["AAPL", "MSFT"]

    snapshot = algo._build_runtime_validation_snapshot(date(2025, 1, 3))

    assert snapshot["current_date"] == "2025-01-03"
    assert snapshot["cma_ready"] is True
    assert snapshot["cma_date"] == "2025-01-03"
    assert snapshot["score_mode"] == "full"
    assert snapshot["scored_universe_size"] == 2
    assert snapshot["timing_ready"] is True
    assert snapshot["timing_date"] == "2025-01-03"
    assert snapshot["timing_source"] == "actionable_composite"
    assert snapshot["market_ready"] is True
    assert snapshot["market_cache_last"] == "2025-01-03 15:55"
    assert snapshot["bar_symbols_ready"] == 2
    assert snapshot["bar_symbols_total"] == 3
    assert snapshot["pending_insights"] == 2
    assert snapshot["report_inputs_ready"] is True


def test_build_runtime_validation_snapshot_reports_missing_timing_state():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    algo.Time = pd.Timestamp("2025-01-03 16:06:00")
    algo.alpha_model = types.SimpleNamespace(
        last_score_snapshot={"date": "2025-01-03", "scores": {"AAPL": 0.8}},
        last_score_mode="light",
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: pd.DataFrame(
                {"AAPL": [100.0]},
                index=pd.to_datetime(["2025-01-02 15:55:00"]),
            ) if key == "close" else default
        ),
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        last_timing_summary={
            "as_of_date": "2025-01-02",
            "score_source": "alpha_snapshot",
        }
    )
    algo.intraday_consolidator = types.SimpleNamespace(
        _bar_counts={"AAPL": 0, "MSFT": 0}
    )
    algo._pending_insights_symbols = []

    snapshot = algo._build_runtime_validation_snapshot(date(2025, 1, 3))

    assert snapshot["cma_ready"] is True
    assert snapshot["timing_ready"] is False
    assert snapshot["timing_date"] == "2025-01-02"
    assert snapshot["market_ready"] is False
    assert snapshot["market_cache_last"] == "2025-01-02 15:55"
    assert snapshot["bar_symbols_ready"] == 0
    assert snapshot["pending_insights"] == 0
    assert snapshot["report_inputs_ready"] is False


def test_build_runtime_validation_snapshot_uses_persisted_market_reports_when_cache_is_stale():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    algo.Time = pd.Timestamp("2025-01-03 16:06:00")
    algo.alpha_model = types.SimpleNamespace(
        last_score_snapshot={"date": "2025-01-03", "scores": {"AAPL": 0.8}},
        last_score_mode="full",
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: pd.DataFrame(
                {"AAPL": [100.0]},
                index=pd.to_datetime(["2025-01-02 15:55:00"]),
            ) if key == "close" else default
        ),
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        last_timing_summary={
            "as_of_date": "2025-01-03",
            "score_source": "market_regime_gate",
        }
    )
    algo.cache_manager = types.SimpleNamespace(
        has_cma_daily_report=lambda as_of_date: True,
        has_timing_daily_report=lambda as_of_date: True,
        has_market_summary_daily_report=lambda as_of_date: True,
        has_market_summary_weekly_report=lambda as_of_date: True,
    )
    algo.intraday_consolidator = types.SimpleNamespace(
        _bar_counts={"AAPL": 0, "MSFT": 0}
    )
    algo._pending_insights_symbols = []

    snapshot = algo._build_runtime_validation_snapshot(date(2025, 1, 3))

    assert snapshot["cma_ready"] is True
    assert snapshot["timing_ready"] is True
    assert snapshot["market_ready"] is True
    assert snapshot["report_inputs_ready"] is True
    assert snapshot["market_cache_last"] == "2025-01-02 15:55"


def test_try_live_regime_storage_fast_path_skips_external_history_when_cache_ready():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    logs = []
    load_calls = []

    algo.LiveMode = True
    algo.Time = pd.Timestamp("2025-01-03 08:00:00")
    algo.alpha_model = types.SimpleNamespace()
    algo.intraday_consolidator = types.SimpleNamespace()
    algo.Debug = lambda message: logs.append(str(message))
    algo.cache_manager = types.SimpleNamespace(
        load_all=lambda alpha_model, consolidator: load_calls.append((alpha_model, consolidator)) or True
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        enable_market_regime_gate=True,
        evaluate_market_regime_diagnostics=lambda algorithm, current_date, **kwargs: (
            {"available": True, "blocked": False, "reason": ""},
            {"availability": True, "available_row_count": 253},
        ),
    )

    result = algo._try_live_regime_storage_fast_path()

    assert result["used_storage_first"] is True
    assert result["storage_hit"] is True
    assert load_calls == [(algo.alpha_model, algo.intraday_consolidator)]
    assert any("Storage-first regime cache hit" in line for line in logs)


def test_on_securities_changed_defers_live_startup_recovery_until_stable():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    logs = []
    recovery_calls = []

    class _FakeConsolidator:
        def __init__(self, algorithm, symbols, **kwargs):
            self.algorithm = algorithm
            self.symbols = list(symbols)

        def set_early_close_detector(self, detector):
            self.detector = detector

        def add_symbol(self, symbol):
            self.symbols.append(symbol)

        def remove_symbol(self, symbol):
            self.symbols = [item for item in self.symbols if item != symbol]

    class _Symbol:
        def __init__(self, value):
            self.Value = value
            self.SecurityType = "Equity"

        def __str__(self):
            return self.Value

    class _Security:
        def __init__(self, value):
            self.Symbol = _Symbol(value)

    original_consolidator = main_module.IntradayFeatureConsolidator
    original_security_type = getattr(main_module, "SecurityType", None)
    main_module.IntradayFeatureConsolidator = _FakeConsolidator
    main_module.SecurityType = types.SimpleNamespace(Equity="Equity")
    try:
        algo.LiveMode = True
        algo.Time = pd.Timestamp("2025-01-03 08:00:00")
        algo.feature_update_mode = "incremental"
        algo.feature_fft_intraday_interval = 30
        algo.feature_shadow_mode = False
        algo.feature_shadow_diff_tol = 1e-6
        algo.early_close_detector = object()
        algo.alpha_model = types.SimpleNamespace(intraday_consolidator=None)
        algo.intraday_consolidator = None
        algo._run_deployment_preview = True
        algo._pending_startup_recovery = False
        algo._startup_recovery_due_time = None
        algo._startup_recovery_stability_minutes = 1
        algo.Debug = lambda message: logs.append(str(message))
        algo._run_recovery_mode = lambda symbols: recovery_calls.append(list(symbols))

        changes = types.SimpleNamespace(
            AddedSecurities=[_Security("AAPL"), _Security("MSFT")],
            RemovedSecurities=[],
        )

        algo.OnSecuritiesChanged(changes)

        assert recovery_calls == []
        assert algo._pending_startup_recovery is True
        assert algo._run_deployment_preview is True
        assert algo._startup_recovery_due_time == pd.Timestamp("2025-01-03 08:01:00")
        assert any("Deferred startup recovery scheduled" in line for line in logs)
    finally:
        main_module.IntradayFeatureConsolidator = original_consolidator
        if original_security_type is None:
            delattr(main_module, "SecurityType")
        else:
            main_module.SecurityType = original_security_type


def test_run_pending_startup_recovery_uses_current_consolidator_symbols_once_due():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    recovery_calls = []

    algo.LiveMode = True
    algo.Time = pd.Timestamp("2025-01-03 08:02:00")
    algo._run_deployment_preview = True
    algo._pending_startup_recovery = True
    algo._startup_recovery_due_time = pd.Timestamp("2025-01-03 08:01:00")
    algo._startup_recovery_attempted = False
    algo.intraday_consolidator = types.SimpleNamespace(symbols=["AAPL", "MSFT", "NVDA"])
    algo.Debug = lambda message: None
    algo._run_recovery_mode = lambda symbols: recovery_calls.append(list(symbols))

    algo._run_pending_startup_recovery_if_ready()

    assert recovery_calls == [["AAPL", "MSFT", "NVDA"]]
    assert algo._pending_startup_recovery is False
    assert algo._run_deployment_preview is False
    assert algo._startup_recovery_attempted is True


def test_build_storage_first_backfill_plan_limits_request_to_missing_symbol_and_internal_gap_dates():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    dates = pd.to_datetime(
        [
            "2025-01-01",
            "2025-01-02",
            "2025-01-03",
            "2025-01-06",
            "2025-01-07",
        ]
    )
    close_df = pd.DataFrame(
        {
            "AAPL": [100.0, 101.0, 102.0, 103.0, 104.0],
            "MSFT": [200.0, 201.0, float("nan"), 203.0, 204.0],
        },
        index=dates,
    )
    volume_df = pd.DataFrame(
        {
            "AAPL": [1_000.0, 1_000.0, 1_000.0, 1_000.0, 1_000.0],
            "MSFT": [900.0, 900.0, 0.0, 900.0, 900.0],
        },
        index=dates,
    )

    algo.Time = pd.Timestamp("2025-01-08 08:00:00")
    algo.alpha_model = types.SimpleNamespace(
        cma_min_valid_days=5,
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: {"close": close_df, "volume": volume_df}.get(key, default)
        ),
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        enable_market_regime_gate=True,
        market_regime_train_days=3,
        _build_market_regime_feature_frame=lambda am: pd.DataFrame(
            {
                "actionable_aggregate": [0.1, 0.2, 0.3, 0.4, 0.5],
                "TsMean(CSStd(TsRet($close,1)),20)": [1.0, 1.0, 1.0, 1.0, 1.0],
                "TsMean(CSBreadthPos(TsRet($close,5)),10)": [0.4, 0.4, 0.4, 0.4, 0.4],
                "TsZScore(CSQuantileSpread(TsRet($close,20),0.8,0.2),20)": [0.2, 0.2, 0.2, 0.2, 0.2],
            },
            index=dates,
        ),
    )

    plan = algo._build_storage_first_backfill_plan(["AAPL", "MSFT"], lookback_days=5, history_span_days=120)

    assert plan["history_symbols"] == ["MSFT"]
    assert plan["requested_external_history"] is True
    assert plan["history_start"] == pd.Timestamp("2025-01-03")
    assert plan["history_end"] == pd.Timestamp("2025-01-04")


def test_build_storage_first_backfill_plan_limits_request_to_missing_front_fill_window():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    dates = pd.to_datetime(
        [
            "2025-01-01",
            "2025-01-02",
            "2025-01-03",
            "2025-01-06",
            "2025-01-07",
        ]
    )
    close_df = pd.DataFrame(
        {
            "AAPL": [100.0, 101.0, 102.0, 103.0, 104.0],
            "MSFT": [float("nan"), float("nan"), 202.0, 203.0, 204.0],
        },
        index=dates,
    )
    volume_df = pd.DataFrame(
        {
            "AAPL": [1_000.0, 1_000.0, 1_000.0, 1_000.0, 1_000.0],
            "MSFT": [0.0, 0.0, 900.0, 900.0, 900.0],
        },
        index=dates,
    )

    algo.Time = pd.Timestamp("2025-01-08 08:00:00")
    algo.alpha_model = types.SimpleNamespace(
        cma_min_valid_days=5,
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: {"close": close_df, "volume": volume_df}.get(key, default)
        ),
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        enable_market_regime_gate=True,
        market_regime_train_days=3,
        _build_market_regime_feature_frame=lambda am: pd.DataFrame(
            {
                "actionable_aggregate": [0.1, 0.2, 0.3, 0.4, 0.5],
                "TsMean(CSStd(TsRet($close,1)),20)": [1.0, 1.0, 1.0, 1.0, 1.0],
                "TsMean(CSBreadthPos(TsRet($close,5)),10)": [0.4, 0.4, 0.4, 0.4, 0.4],
                "TsZScore(CSQuantileSpread(TsRet($close,20),0.8,0.2),20)": [0.2, 0.2, 0.2, 0.2, 0.2],
            },
            index=dates,
        ),
    )

    plan = algo._build_storage_first_backfill_plan(["AAPL", "MSFT"], lookback_days=5, history_span_days=120)

    assert plan["history_symbols"] == ["MSFT"]
    assert plan["requested_external_history"] is True
    assert plan["history_end"] == pd.Timestamp("2025-01-04")
    assert plan["history_start"] < pd.Timestamp("2025-01-03")
    assert plan["history_end"] < algo.Time


def test_build_storage_first_backfill_plan_expands_symbols_for_regime_front_fill():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    dates = pd.to_datetime(
        [
            "2025-01-01",
            "2025-01-02",
            "2025-01-03",
            "2025-01-06",
            "2025-01-07",
        ]
    )
    close_df = pd.DataFrame(
        {
            "AAPL": [100.0, 101.0, 102.0, 103.0, 104.0],
            "MSFT": [200.0, 201.0, float("nan"), 203.0, 204.0],
        },
        index=dates,
    )
    volume_df = pd.DataFrame(
        {
            "AAPL": [1_000.0, 1_000.0, 1_000.0, 1_000.0, 1_000.0],
            "MSFT": [900.0, 900.0, 0.0, 900.0, 900.0],
        },
        index=dates,
    )

    algo.Time = pd.Timestamp("2025-01-08 08:00:00")
    algo.alpha_model = types.SimpleNamespace(
        cma_min_valid_days=5,
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: {"close": close_df, "volume": volume_df}.get(key, default)
        ),
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        enable_market_regime_gate=True,
        market_regime_train_days=3,
        _build_market_regime_feature_frame=lambda am: pd.DataFrame(
            {
                "actionable_aggregate": [float("nan")] * len(dates),
                "TsMean(CSStd(TsRet($close,1)),20)": [1.0, 1.0, 1.0, 1.0, 1.0],
                "TsMean(CSBreadthPos(TsRet($close,5)),10)": [0.4, 0.4, 0.4, 0.4, 0.4],
                "TsZScore(CSQuantileSpread(TsRet($close,20),0.8,0.2),20)": [0.2, 0.2, 0.2, 0.2, 0.2],
            },
            index=dates,
        ),
    )

    plan = algo._build_storage_first_backfill_plan(["AAPL", "MSFT"], lookback_days=5, history_span_days=120)

    assert plan["history_symbols"] == ["MSFT", "AAPL"]
    assert plan["requested_external_history"] is True
    assert plan["history_start"] < pd.Timestamp("2025-01-01")
    assert plan["history_end"] == pd.Timestamp("2025-01-04")


def test_persist_market_regime_debug_and_validate_raises_when_live_regime_unavailable():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    debug_saves = []

    algo.LiveMode = True
    algo.Debug = lambda message: None
    algo.cache_manager = types.SimpleNamespace(
        save_market_regime_debug_report=lambda as_of_date, payload: debug_saves.append((as_of_date, payload))
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        enable_market_regime_gate=True,
        evaluate_market_regime_diagnostics=lambda algorithm, current_date, **kwargs: (
            {
                "available": False,
                "blocked": False,
                "label": None,
                "reason": "insufficient_feature_history",
            },
            {
                "as_of_date": "2025-01-03",
                "availability": False,
                "failure_reason": "insufficient_feature_history",
                "requested_external_history": True,
            },
        ),
    )

    with pytest.raises(RuntimeError, match="insufficient_feature_history"):
        algo._persist_market_regime_debug_and_validate(
            date(2025, 1, 3),
            used_storage_first=True,
            storage_hit=False,
            requested_external_history=True,
        )

    assert len(debug_saves) == 1
    assert debug_saves[0][0] == date(2025, 1, 3)
    assert debug_saves[0][1]["failure_reason"] == "insufficient_feature_history"


def test_persist_market_regime_debug_and_validate_skips_raise_for_no_regime_eligible_symbols():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    debug_saves = []
    debug_logs = []

    algo.LiveMode = True
    algo.Debug = lambda message: debug_logs.append(str(message))
    algo.cache_manager = types.SimpleNamespace(
        save_market_regime_debug_report=lambda as_of_date, payload: debug_saves.append((as_of_date, payload))
    )
    algo.actionable_timing_model = types.SimpleNamespace(
        enable_market_regime_gate=True,
        evaluate_market_regime_diagnostics=lambda algorithm, current_date, **kwargs: (
            {
                "available": False,
                "blocked": False,
                "label": None,
                "reason": "no_regime_eligible_symbols",
            },
            {
                "as_of_date": "2025-01-03",
                "availability": False,
                "failure_reason": "no_regime_eligible_symbols",
                "requested_external_history": False,
                "regime_eligible_symbol_count": 0,
                "regime_excluded_young_symbol_count": 2,
            },
        ),
    )

    state = algo._persist_market_regime_debug_and_validate(
        date(2025, 1, 3),
        used_storage_first=True,
        storage_hit=False,
        requested_external_history=False,
    )

    assert state["available"] is False
    assert state["reason"] == "no_regime_eligible_symbols"
    assert len(debug_saves) == 1
    assert debug_saves[0][1]["failure_reason"] == "no_regime_eligible_symbols"
    assert any("no_regime_eligible_symbols" in message for message in debug_logs)


def test_run_recovery_mode_rebuilds_from_storage_before_validating_empty_backfill():
    algo = AlphaSAGE_MultiFactors.__new__(AlphaSAGE_MultiFactors)
    events = []

    market_cache_frames = {
        "close": pd.DataFrame(
            {"AAPL": [100.0], "ATGE": [float("nan")]},
            index=pd.to_datetime(["2025-01-02 15:55:00"]),
        ),
        "volume": pd.DataFrame(
            {"AAPL": [1_000.0], "ATGE": [0.0]},
            index=pd.to_datetime(["2025-01-02 15:55:00"]),
        ),
    }

    algo.Time = pd.Timestamp("2025-01-03 08:00:00")
    algo.LiveMode = True
    algo.eval_window = 80
    algo.prediction_horizon = 1
    algo.backtest_fast_recovery = False
    algo._is_post_market = False
    algo.Debug = lambda message: None
    algo.Log = lambda message: None
    algo.GetParameter = lambda name: None
    algo._try_live_regime_storage_fast_path = lambda: {
        "used_storage_first": True,
        "storage_hit": False,
        "requested_external_history": False,
    }
    algo._build_storage_first_backfill_plan = lambda symbols, lookback_days, history_span_days: {
        "history_symbols": ["ATGE"],
        "history_start": pd.Timestamp("2024-12-01 00:00:00"),
        "history_end": pd.Timestamp("2025-01-03 00:00:00"),
        "requested_external_history": True,
    }
    algo._persist_market_regime_debug_and_validate = lambda current_date, **kwargs: events.append(
        ("validate", current_date, dict(kwargs))
    )
    algo._persist_structured_reports = lambda current_date, source_mode="finalize": events.append(
        ("persist_reports", current_date, source_mode)
    )
    algo._trigger_timing_preview_persistence = lambda current_date=None: events.append(
        ("timing_preview", current_date)
    )
    algo.cache_manager = types.SimpleNamespace(
        save_all=lambda alpha_model, consolidator, overwrite_dates=None: events.append(
            ("save_all", overwrite_dates)
        )
    )
    algo.intraday_consolidator = types.SimpleNamespace(symbols=["AAPL", "ATGE"])
    algo.alpha_model = types.SimpleNamespace(
        lookback_days=120,
        factor_lookback_days=41,
        cma_min_valid_days=60,
        market_cache=types.SimpleNamespace(
            get=lambda key, default=None: market_cache_frames.get(key, default)
        ),
        generate_insights_on_schedule=lambda algorithm: events.append(("generate", algorithm.Time.date())) or [],
        last_score_snapshot={"date": "2025-01-03"},
        is_pre_market=True,
    )

    original_request_history = main_module.request_history_with_symbol_fallback
    original_datetime = getattr(main_module, "datetime", None)
    main_module.datetime = datetime
    main_module.request_history_with_symbol_fallback = (
        lambda request_symbols, multi_fetcher, single_fetcher, debug=None: (pd.DataFrame(), False)
    )
    try:
        algo._run_recovery_mode(["AAPL", "ATGE"])
    finally:
        main_module.request_history_with_symbol_fallback = original_request_history
        if original_datetime is None:
            delattr(main_module, "datetime")
        else:
            main_module.datetime = original_datetime

    assert events[0] == ("generate", date(2025, 1, 3))
    assert events[1][0] == "validate"
    assert events[1][1] == date(2025, 1, 3)
    assert events[1][2]["used_storage_first"] is True
    assert events[1][2]["storage_hit"] is False
    assert events[1][2]["requested_external_history"] is True
