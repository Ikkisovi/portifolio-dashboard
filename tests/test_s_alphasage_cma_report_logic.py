from pathlib import Path
import sys
import types
from datetime import date, time

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "S_alphasage"))
sys.modules.pop("alpha_model", None)

mod = sys.modules.get("AlgorithmImports")
if mod is None:
    mod = types.ModuleType("AlgorithmImports")
    sys.modules["AlgorithmImports"] = mod


class AlphaModel:
    pass


class QCAlgorithm:
    pass


class Slice:
    pass


class SecurityChanges:
    pass


class InsightDirection:
    UP = 1


class Insight:
    @staticmethod
    def Price(*args, **kwargs):
        return {"args": args, "kwargs": kwargs}


class SecurityType:
    Equity = 1


class Market:
    USA = "usa"


class Symbol:
    @staticmethod
    def Create(value, *_):
        return value


mod.AlphaModel = AlphaModel
mod.QCAlgorithm = QCAlgorithm
mod.Slice = Slice
mod.SecurityChanges = SecurityChanges
mod.InsightDirection = InsightDirection
mod.Insight = Insight
mod.SecurityType = SecurityType
mod.Market = Market
mod.Symbol = Symbol

from alpha_model import AlphaSAGEAlphaModel


class FakeCacheManager:
    def __init__(self):
        self.saved_report = None
        self.saved_state = None

    def save_cma_daily_report(self, as_of_date, report):
        self.saved_report = (as_of_date, report)

    def save_cma_state(self, payload):
        self.saved_state = payload


class FakeAlgorithm:
    def __init__(self):
        self.logs = []
        self.cache_manager = FakeCacheManager()

    def Debug(self, message):
        self.logs.append(str(message))


def test_score_with_last_cma_weights_and_daily_report_transitions():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
        cma_daily_report_top_n=10,
        cma_daily_report_bucket_size=2,
        cma_daily_report_sink="log+objectstore",
    )
    model.last_cma_weights = {"alpha_0": 0.7, "alpha_1": 0.3}
    symbols = pd.Index(["AAPL", "MSFT", "NVDA"])
    val_factors = [
        {"name": "alpha_0", "values": pd.Series([3.0, 2.0, 1.0], index=symbols)},
        {"name": "alpha_1", "values": pd.Series([1.0, 2.0, 3.0], index=symbols)},
    ]
    scores, used = model._score_with_last_cma_weights(val_factors, symbols)
    assert len(used) == 2
    assert scores.idxmax() == "AAPL"

    model.last_score_snapshot = {
        "date": "2026-01-07",
        "scores": {"AAPL": 0.50, "MSFT": 0.40, "NVDA": 0.30, "TSLA": 0.20},
    }
    current_scores = pd.Series(
        {"AAPL": 0.10, "MSFT": 0.90, "NVDA": 0.20, "TSLA": 0.80},
        dtype=float,
    )
    data_dict = {
        "$drift_factor": pd.DataFrame([{"AAPL": 1.0, "MSFT": 0.0, "NVDA": None, "TSLA": 2.0}]),
        "$amihud_mean": pd.DataFrame([{"AAPL": 0.0, "MSFT": 1.0, "NVDA": 2.0, "TSLA": 3.0}]),
        "$amihud_range": pd.DataFrame([{"AAPL": 1.0, "MSFT": 1.0, "NVDA": 1.0, "TSLA": 1.0}]),
        "$vol_fft": pd.DataFrame([{"AAPL": 0.1, "MSFT": 0.2, "NVDA": 0.3, "TSLA": 0.4}]),
        "$ohl": pd.DataFrame([{"AAPL": 0.0, "MSFT": 0.0, "NVDA": 0.0, "TSLA": 0.0}]),
        "$chl": pd.DataFrame([{"AAPL": 0.1, "MSFT": 0.2, "NVDA": 0.3, "TSLA": 0.4}]),
        "$ohlc": pd.DataFrame([{"AAPL": 0.1, "MSFT": 0.2, "NVDA": 0.3, "TSLA": 0.4}]),
        "$chlo": pd.DataFrame([{"AAPL": 0.1, "MSFT": 0.2, "NVDA": 0.3, "TSLA": 0.4}]),
    }
    algo = FakeAlgorithm()
    report = model._build_daily_cma_report(
        algo,
        current_date="2026-01-08",
        final_scores=current_scores,
        score_mode="light",
        rebalance_today=False,
        data_dict=data_dict,
    )
    assert report is not None
    assert report["score_mode"] == "light"
    assert "AAPL" in report["bucket_transitions"]["top_to_bottom"]
    assert "TSLA" in report["bucket_transitions"]["bottom_to_top"]

    model._persist_daily_cma_report(algo, report)
    assert algo.cache_manager.saved_report is not None
    model.last_score_mode = "light"
    model._persist_cma_state(algo, "2026-01-08")
    assert algo.cache_manager.saved_state is not None


def test_build_daily_cma_report_includes_per_factor_metrics_and_eval_window():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
        eval_window=80,
    )
    model.last_score_snapshot = {"date": "2026-01-07", "scores": {"AAPL": 0.1}}
    current_scores = pd.Series({"AAPL": 0.2, "MSFT": -0.1}, dtype=float)
    algo = FakeAlgorithm()
    data_dict = {}

    metrics = [
        {
            "factor": "alpha_0",
            "weight": 0.7,
            "log_ret": 0.1,
            "ic_mean": 0.01,
            "icir": 0.2,
            "fitness": 1.2,
            "sharpe": 0.8,
            "annual_return": 0.3,
            "cma_total": 1.31,
        }
    ]
    meta = {
        "window_days_used": 80,
        "score_mode": "full",
        "available": True,
        "unavailable_reason": "",
    }

    report = model._build_daily_cma_report(
        algo,
        current_date="2026-01-08",
        final_scores=current_scores,
        score_mode="full",
        rebalance_today=True,
        data_dict=data_dict,
        per_factor_cma_metrics=metrics,
        per_factor_cma_metrics_meta=meta,
    )
    assert report is not None
    assert report["per_factor_cma_metrics"] == metrics
    assert report["per_factor_cma_metrics_meta"]["available"] is True
    assert report["objective_config"]["eval_window"] == 80


def test_build_daily_cma_report_defaults_per_factor_meta_unavailable():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
    )
    model.last_score_snapshot = {"date": "2026-01-07", "scores": {"AAPL": 0.1}}
    current_scores = pd.Series({"AAPL": 0.2, "MSFT": -0.1}, dtype=float)
    algo = FakeAlgorithm()

    report = model._build_daily_cma_report(
        algo,
        current_date="2026-01-08",
        final_scores=current_scores,
        score_mode="light",
        rebalance_today=False,
        data_dict={},
    )
    assert report is not None
    assert report["per_factor_cma_metrics"] == []
    assert report["per_factor_cma_metrics_meta"]["available"] is False
    assert report["per_factor_cma_metrics_meta"]["unavailable_reason"]


def test_build_daily_cma_report_uses_explicit_data_as_of_date():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
    )
    model.last_score_snapshot = {"date": "2026-01-07", "scores": {"AAPL": 0.1}}
    current_scores = pd.Series({"AAPL": 0.2, "MSFT": -0.1}, dtype=float)
    algo = FakeAlgorithm()

    report = model._build_daily_cma_report(
        algo,
        current_date="2026-01-08",
        final_scores=current_scores,
        score_mode="light",
        rebalance_today=False,
        data_dict={},
        data_as_of_date="2026-01-06",
    )

    assert report is not None
    assert report["as_of_date"] == "2026-01-08"
    assert report["data_as_of_date"] == "2026-01-06"


def test_compute_per_factor_cma_metrics_uses_runtime_tensor_and_weights():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
    )
    rank_t = np.array(
        [
            [
                [0.9, 0.8, 0.2, 0.1],
                [0.8, 0.7, 0.3, 0.2],
                [0.85, 0.75, 0.25, 0.15],
                [0.88, 0.78, 0.28, 0.18],
            ],
            [
                [0.2, 0.3, 0.7, 0.8],
                [0.25, 0.35, 0.65, 0.75],
                [0.22, 0.32, 0.68, 0.78],
                [0.2, 0.3, 0.7, 0.8],
            ],
        ],
        dtype=float,
    )
    ret_t = np.array(
        [
            [0.03, 0.02, -0.01, -0.02],
            [0.02, 0.015, -0.005, -0.01],
            [0.025, 0.018, -0.006, -0.012],
            [0.028, 0.019, -0.007, -0.013],
        ],
        dtype=float,
    )
    metrics, meta = model._compute_per_factor_cma_metrics(
        rank_t=rank_t,
        ret_t=ret_t,
        factor_names=["alpha_0", "alpha_1"],
        weights_map={"alpha_0": 0.7, "alpha_1": 0.3},
        score_mode="light",
    )
    assert len(metrics) == 2
    assert meta["available"] is True
    assert meta["score_mode"] == "light"
    assert metrics[0]["factor"] == "alpha_0"
    assert "cma_total" in metrics[0]


def test_resolve_preview_report_date_prefers_latest_cache_date():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
    )
    model.market_cache.frames["close"] = pd.DataFrame(
        {"AAPL": [100.0, 101.0]},
        index=pd.to_datetime(["2026-03-03", "2026-03-04"]),
    )
    resolved = model._resolve_preview_report_date(date(2026, 3, 5))
    assert resolved == date(2026, 3, 4)


def test_resolve_preview_report_date_uses_runtime_date_for_post_market_deploy():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
    )
    model.market_cache.frames["close"] = pd.DataFrame(
        {"AAPL": [100.0, 101.0]},
        index=pd.to_datetime(["2026-03-03", "2026-03-04"]),
    )
    model.is_post_market = True

    resolved = model._resolve_preview_report_date(date(2026, 3, 5))

    assert resolved == date(2026, 3, 5)


def test_should_persist_preview_outputs_matches_finalize_rules():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
    )
    algo = types.SimpleNamespace(LiveMode=True)

    # Preview before finalize should not persist.
    assert (
        model._should_persist_preview_outputs(
            algorithm=algo,
            is_preview=True,
            current_time=time(15, 0),
            market_finalize=time(15, 55),
            is_pre_market_now=False,
        )
        is False
    )

    # Preview after finalize should persist.
    assert (
        model._should_persist_preview_outputs(
            algorithm=algo,
            is_preview=True,
            current_time=time(16, 0),
            market_finalize=time(15, 55),
            is_pre_market_now=False,
        )
        is True
    )

    # post-market deploy flag overrides intraday time check.
    model.is_post_market = True
    assert (
        model._should_persist_preview_outputs(
            algorithm=algo,
            is_preview=True,
            current_time=time(10, 0),
            market_finalize=time(15, 55),
            is_pre_market_now=False,
        )
        is True
    )


def test_should_persist_preview_outputs_for_overnight_previous_session_report():
    model = AlphaSAGEAlphaModel(
        weighting_method="cmaes",
        cma_daily_report_enabled=True,
    )
    algo = types.SimpleNamespace(LiveMode=True)
    model.market_cache.frames["close"] = pd.DataFrame(
        {"AAPL": [101.0]},
        index=pd.to_datetime(["2026-03-09 15:55:00"]),
    )

    assert (
        model._should_persist_preview_outputs(
            algorithm=algo,
            is_preview=True,
            current_time=time(1, 49),
            market_finalize=time(15, 55),
            is_pre_market_now=True,
            current_date=date(2026, 3, 10),
            report_date=date(2026, 3, 9),
        )
        is True
    )
