# region imports
from AlgorithmImports import *
import pandas as pd
import numpy as np
import re
import json
import hashlib
from typing import Optional, Dict, List
from datetime import time, datetime, timedelta
from factors import FactorEngine, get_max_lookback, get_expression_warmup, TsCorr
from features import (
    calculate_sortino_ratio,
    calculate_ts_mom_rank,
    calculate_max_dd_ratio,
    calculate_rel_strength_ma,
    calculate_high_break_revert_45_5,
)
from cma_manual import cmaes_optimize
from chip_features import FEATURE_COLUMNS, calc_chip_features_incremental
# endregion

def zscore(a):
    return (a - np.mean(a)) / (np.std(a) + 1e-8)


class MarketDataCache:
    """
    Rolling cache for OHLCV data used in factor computation.
    """

    def __init__(self, max_days: int = 120):
        self.max_days = max_days
        self.frames = {
            "close": pd.DataFrame(),
            "open": pd.DataFrame(),
            "high": pd.DataFrame(),
            "low": pd.DataFrame(),
            "volume": pd.DataFrame(),
            "vwap": pd.DataFrame(),
        }
        self.logger = None

    def set_logger(self, logger) -> None:
        self.logger = logger

    def is_empty(self) -> bool:
        frame = self.frames.get("close")
        return frame is None or frame.empty

    def columns(self):
        frame = self.frames.get("close")
        if frame is None or frame.empty:
            return []
        return list(frame.columns)

    def last_timestamp(self):
        frame = self.frames.get("close")
        if frame is None or frame.empty:
            return None
        return frame.index[-1]

    def ensure_columns(self, symbols) -> None:
        if not symbols:
            return
        for key, frame in self.frames.items():
            if frame is None or frame.empty:
                self.frames[key] = pd.DataFrame(index=pd.Index([]), columns=symbols)
            else:
                self.frames[key] = frame.reindex(columns=frame.columns.union(symbols))

    def _merge_frame(self, key: str, new_df: pd.DataFrame) -> None:
        if new_df is None or new_df.empty:
            return
        new_df = new_df.sort_index()
        existing = self.frames.get(key)
        if existing is None or existing.empty:
            self.frames[key] = new_df.tail(self.max_days)
            return

        existing = existing.sort_index()
        all_cols = existing.columns.union(new_df.columns)
        existing = existing.reindex(columns=all_cols)
        new_df = new_df.reindex(columns=all_cols)
        overlap_idx = existing.index.intersection(new_df.index)

        if self.logger is not None and not overlap_idx.empty:
            try:
                existing_overlap = existing.loc[overlap_idx]
                new_overlap = new_df.loc[overlap_idx]
                nan_overwrites = existing_overlap.notna() & new_overlap.isna()
                overwrite_count = int(nan_overwrites.sum().sum())
                if overwrite_count > 0:
                    latest_ts = overlap_idx.max()
                    self.logger(
                        f"[MarketDataCache] {key}: avoiding {overwrite_count} NaN overwrites "
                        f"on {len(overlap_idx)} overlapping rows (latest={latest_ts})."
                    )
            except Exception as e:
                self.logger(f"[MarketDataCache] {key}: overlap check failed: {e}")

        combined = existing.reindex(index=existing.index.union(new_df.index), columns=all_cols)
        combined.update(new_df)
        self.frames[key] = combined.tail(self.max_days)

    def update_from_history(self, history: pd.DataFrame) -> None:
        if history is None or history.empty:
            return
        for key in self.frames.keys():
            if key in history.columns:
                df = history[key].unstack(level=0)
                self._merge_frame(key, df)

    def append_snapshot(self, timestamp, snapshot: dict) -> None:
        if not snapshot:
            return
        for key, snap_dict in snapshot.items():
            if not snap_dict:
                continue
            row = pd.Series(snap_dict)
            frame = self.frames.get(key)
            if frame is None or frame.empty:
                new_df = pd.DataFrame([row], index=[timestamp])
                frame = new_df
            else:
                cols = frame.columns.union(row.index)
                frame = frame.reindex(columns=cols)
                row = row.reindex(cols)

                if timestamp in frame.index:
                    frame.loc[timestamp] = frame.loc[timestamp].where(row.isna(), row)
                else:
                    frame.loc[timestamp] = row

            frame = frame.sort_index()
            self.frames[key] = frame.tail(self.max_days)

    def get(self, key: str) -> pd.DataFrame:
        return self.frames.get(key, pd.DataFrame())


class FactorDataCache:
    """
    Incremental cache for factor data to avoid recomputing everything daily.
    """

    def __init__(self, max_days: int = 500):
        self.max_days = max_days
        self.factor_values = {}
        self.daily_ic = {}
        self.daily_ls_returns = {}
        self.last_date = {}

    def get_new_dates(self, factor_name: str, available_dates) -> list:
        if factor_name not in self.last_date:
            return list(available_dates)
        last = self.last_date[factor_name]
        return [d for d in available_dates if d > last]

    def update_factor_values(self, factor_name: str, new_values: pd.DataFrame):
        if new_values is None or new_values.empty:
            return
        if factor_name in self.factor_values:
            existing = self.factor_values[factor_name]
            new_dates = [d for d in new_values.index if d not in existing.index]
            if new_dates:
                new_data = new_values.loc[new_dates]
                self.factor_values[factor_name] = pd.concat([existing, new_data]).tail(self.max_days)
        else:
            self.factor_values[factor_name] = new_values.tail(self.max_days)
        self.last_date[factor_name] = self.factor_values[factor_name].index[-1]

    def get_factor_values(self, factor_name: str) -> pd.DataFrame:
        return self.factor_values.get(factor_name)

    def update_daily_ic(self, factor_name: str, ic_series: pd.Series):
        if ic_series is None or ic_series.empty:
            return
        if factor_name in self.daily_ic:
            existing = self.daily_ic[factor_name]
            combined = pd.concat([existing, ic_series])
            combined = combined[~combined.index.duplicated(keep='last')].tail(self.max_days)
            self.daily_ic[factor_name] = combined
        else:
            self.daily_ic[factor_name] = ic_series.tail(self.max_days)

    def get_daily_ic(self, factor_name: str) -> pd.Series:
        return self.daily_ic.get(factor_name)

    def update_daily_ls(self, factor_name: str, ls_series: pd.Series):
        if ls_series is None or ls_series.empty:
            return
        if factor_name in self.daily_ls_returns:
            existing = self.daily_ls_returns[factor_name]
            combined = pd.concat([existing, ls_series])
            combined = combined[~combined.index.duplicated(keep='last')].tail(self.max_days)
            self.daily_ls_returns[factor_name] = combined
        else:
            self.daily_ls_returns[factor_name] = ls_series.tail(self.max_days)

    def get_daily_ls(self, factor_name: str) -> pd.Series:
        return self.daily_ls_returns.get(factor_name)

    def trim_all(self):
        for name in self.factor_values:
            self.factor_values[name] = self.factor_values[name].tail(self.max_days)
        for name in self.daily_ic:
            self.daily_ic[name] = self.daily_ic[name].tail(self.max_days)
        for name in self.daily_ls_returns:
            self.daily_ls_returns[name] = self.daily_ls_returns[name].tail(self.max_days)


class ChipFeatureCache:
    """
    Incremental cache for chip features to avoid recomputing full history daily.
    """

    def __init__(self, max_days: int = 500, feature_columns: Optional[List[str]] = None):
        self.max_days = max_days
        self.feature_columns = feature_columns or list(FEATURE_COLUMNS)
        self.feature_values = {name: pd.DataFrame() for name in self.feature_columns}
        self.state = {}
        self.last_date = {}

    def _ensure_frame(self, name: str, index, columns) -> None:
        frame = self.feature_values.get(name)
        if frame is None or frame.empty:
            frame = pd.DataFrame(index=pd.Index([]), columns=[])
        frame = frame.reindex(index=frame.index.union(index), columns=frame.columns.union(columns))
        self.feature_values[name] = frame

    def update_panel(
        self,
        lows: pd.DataFrame,
        highs: pd.DataFrame,
        closes: pd.DataFrame,
        volumes: pd.DataFrame,
        vwap: Optional[pd.DataFrame] = None,
        turnover_rate: Optional[pd.DataFrame] = None,
        symbols: Optional[List] = None,
        dates: Optional[List] = None,
        reset_state: bool = False,
    ) -> None:
        if closes is None or closes.empty:
            return
        base_index = closes.index
        base_columns = closes.columns
        if symbols is not None:
            base_columns = pd.Index(symbols)
        if dates is not None:
            date_index = pd.DatetimeIndex(dates)
            base_index = base_index.intersection(date_index)
        if base_index.empty or len(base_columns) == 0:
            return

        lows = lows.reindex(index=base_index, columns=base_columns)
        highs = highs.reindex(index=base_index, columns=base_columns)
        closes = closes.reindex(index=base_index, columns=base_columns)
        volumes = volumes.reindex(index=base_index, columns=base_columns)
        vwap = vwap.reindex(index=base_index, columns=base_columns) if vwap is not None else None
        turnover_rate = (
            turnover_rate.reindex(index=base_index, columns=base_columns)
            if turnover_rate is not None
            else None
        )

        for feat in self.feature_columns:
            self._ensure_frame(feat, base_index, base_columns)

        for sym in base_columns:
            if reset_state:
                new_dates = list(base_index)
                state = None
            else:
                last = self.last_date.get(sym)
                if last is None:
                    new_dates = list(base_index)
                else:
                    new_dates = [d for d in base_index if d > last]
                state = self.state.get(sym)
            if not new_dates:
                continue

            daily_df = pd.DataFrame(
                {
                    "low": lows[sym].reindex(new_dates),
                    "high": highs[sym].reindex(new_dates),
                    "close": closes[sym].reindex(new_dates),
                    "volume": volumes[sym].reindex(new_dates),
                },
                index=new_dates,
            )
            if vwap is not None:
                daily_df["vwap"] = vwap[sym].reindex(new_dates)
            if turnover_rate is not None:
                daily_df["turnover_rate"] = turnover_rate[sym].reindex(new_dates)

            chip_df, state = calc_chip_features_incremental(
                daily_df,
                state,
            )
            if chip_df is None or chip_df.empty:
                continue

            self.state[sym] = state
            self.last_date[sym] = chip_df.index[-1]
            for feat in self.feature_columns:
                frame = self.feature_values[feat]
                frame.loc[chip_df.index, sym] = chip_df[feat].values

        for feat in self.feature_columns:
            frame = self.feature_values.get(feat)
            if frame is not None and not frame.empty:
                self.feature_values[feat] = frame.sort_index().tail(self.max_days)

    def get_panel(self, index, columns, ffill: bool = False) -> Dict[str, pd.DataFrame]:
        results = {}
        for feat in self.feature_columns:
            frame = self.feature_values.get(feat)
            if frame is None or frame.empty:
                aligned = pd.DataFrame(np.nan, index=index, columns=columns)
            else:
                aligned = frame.reindex(index=index, columns=columns)
                if ffill:
                    aligned = aligned.ffill()
            results[feat] = aligned
        return results

    def is_empty(self) -> bool:
        return all(frame is None or frame.empty for frame in self.feature_values.values())


FACTOR_EXPRESSIONS = [
    "Mul(-1.0,Div($log_volume,Sub(SLog1p(Sub($log_volume,Rank($log_volume))),$log_volume)))",
    'TsQuantile(TsMomRank($log_volume,40),0.5,30)',
    'TsQuantile(Sub($log_volume,$log_close),0.67,10)',
    'Mul(-1.0,TsCorr(Div($log_volume,$open),$low,20))',
    "Mul(-1.0,Div(TsRank($vol_fft,40),$amihud_mean))",
    'Div($log_money,Sub($log_money,$ohl))',
    'Rank(Div(Sqrt($high),$vwap))',
    'TsQuantile(TsMad($log_money,20),0.33,10)',
    # 'Mul(-1.0,TsCorr(Sub($drift_factor,$amihud_range),$vwap,30))',
    'Add(Add($amihud_mean,TsCorr($vwap,$chip_vwap,15)),Add(Abs(Sub($chlo,SLog1p($amihud_range))),Add(Rank($chlo),Add($chlo,$ohlc))))',
    'Sub(Sqrt($chl),Add(SLog1p(Abs($ohlc)),Add(Div($ohl,Inv(SLog1p($ohlc))),Add(TsQuantile($ohlc,0.75,20),Add(TsQuantile(Sub($chlo,$ohlc),0.5,40),Add($chlo,$ohlc))))))',
    'Mul(-1.0,TsQuantile(Ret($open),0.25,40))',
    'TsMad($log_volume,30)',
    'Sub($ohlc,TsCorr($volume,$chip_near_support,10))',
]


ACTIONABLE_FACTOR_EXPRESSIONS = [
    "Div($chip_overlap,Sqrt($high))",
    "Mul($chl,Sub($close,$open))",
    "Mul(-1.0,TsCorr(TsNonLinearMom($vol_fft,40),$chip_vwap,15))",
    "TsRelStrength(TsIr($log_volume,30),20,40)",
    "Mul(-1.0,TsQuantile(TsDelta($chl,40),0.25,30))",
    "Sqrt(Abs(TsNonLinearMom($high_break_revert_45_5,20)))",
    "Div($chip_near_support,TsQuantile($log_close,0.5,40))",
    "Mul(-1.0,SLog1p(Div(Rank($ohl),$chlo)))",
    "Mul(-1.0,TsRelStrength($ohl,20,30))",
    "Mul(-1.0,TsCorr($drift_factor,Rank($chip_overlap),20))",
    "TsSortino($chip_near_support,40)",
    "Mul(-1.0,TsRelStrength(Inv($drift_factor),10,30))",
    "Mul(-1.0,Inv(TsRelStrength($amihud_range,10,15)))",
    "TsRet(Rank($high_break_revert_45_5),30)",
]

ACTIONABLE_AGGREGATE_FACTOR_INDEXES = [3, 4, 5, 9, 10, 11, 12, 13]
ACTIONABLE_AGGREGATE_TOP_N = 10


class AlphaSAGEAlphaModel(AlphaModel):
    """
    Alpha model that calculates factors and generates insights.
    Handles pre/post-market deployment with deferred rebalancing.
    """

    def __init__(
        self,
        weighting_method: str = "zscore",
        rebalance_days: int = 5,
        eval_window: int = 60,
        prediction_horizon: int = 1,
        prediction_lookback_days: Optional[int] = None,
        cma_turnover_penalty: float = 0.01,
        cma_icir_penalty: float = 0.01,
        cma_icir_floor: float = 0.0,
        cma_obj_logret_weight: float = 1.0,
        cma_obj_ic_weight: float = 1.0,
        cma_obj_fitness_weight: float = 1.0,
        cma_daily_report_enabled: bool = True,
        cma_daily_report_top_n: int = 20,
        cma_daily_report_bucket_size: int = 8,
        cma_daily_report_sink: str = "log+objectstore",
        cma_light_report_nonrebalance: bool = True,
        cma_light_score_source: str = "last_cma_weights",
    ):
        self.rebalance_trading_days = int(rebalance_days)
        self.next_rebalance_date = None
        self.last_signal_date = None
        self.eval_window = int(eval_window)
        self.prediction_horizon = int(prediction_horizon)
        self.close_buffer_minutes = 5
        self.cma_turnover_penalty = float(cma_turnover_penalty)
        self.cma_icir_penalty = float(cma_icir_penalty)
        self.cma_icir_floor = float(cma_icir_floor)
        self.cma_obj_logret_weight = float(cma_obj_logret_weight)
        self.cma_obj_ic_weight = float(cma_obj_ic_weight)
        self.cma_obj_fitness_weight = float(cma_obj_fitness_weight)
        self.cma_top_bottom = 8
        self.cma_forward_horizon = 20
        self.cma_min_valid_days = 60
        self.cma_factor_min_coverage = 0.6
        self.chip_backfill_days = max(self.eval_window, self.cma_min_valid_days, 20)
        self.last_cma_weights = None
        self.last_score_snapshot = None
        self.last_score_mode = None
        self.cmaes_weights = None
        self._cma_state_loaded = False
        self.cma_daily_report_enabled = bool(cma_daily_report_enabled)
        self.cma_daily_report_top_n = max(1, int(cma_daily_report_top_n))
        self.cma_daily_report_bucket_size = max(1, int(cma_daily_report_bucket_size))
        self.cma_daily_report_sink = (cma_daily_report_sink or "log+objectstore").strip().lower()
        if not self.cma_daily_report_sink:
            self.cma_daily_report_sink = "log+objectstore"
        self.cma_light_report_nonrebalance = bool(cma_light_report_nonrebalance)
        self.cma_light_score_source = (cma_light_score_source or "last_cma_weights").strip().lower()
        self.daily_cma_report_config = {
            "enabled": self.cma_daily_report_enabled,
            "top_n": self.cma_daily_report_top_n,
            "bucket_size": self.cma_daily_report_bucket_size,
            "sink": self.cma_daily_report_sink,
            "light_nonrebalance": self.cma_light_report_nonrebalance,
            "light_score_source": self.cma_light_score_source,
        }

        self.factor_lookback_days = get_max_lookback(FACTOR_EXPRESSIONS)
        factor_padding = 30

        allowed_methods = {"zscore", "cmaes", "equal", "fitness_sharpe"}
        self.weighting_method = weighting_method if weighting_method in allowed_methods else "cmaes"
        if prediction_lookback_days is not None and prediction_lookback_days > 0:
            min_lookback = self.eval_window + self.prediction_horizon + max(
                60,
                self.factor_lookback_days + factor_padding,
            )
            self.lookback_days = max(int(prediction_lookback_days), min_lookback)
        else:
            self.lookback_days = max(
                self.eval_window + self.prediction_horizon + max(
                    60,
                    self.factor_lookback_days + factor_padding,
                ),
                120,
            )

        self.intraday_consolidator = None
        self.factor_defs = [{"name": f"alpha_{i}", "expr": expr} for i, expr in enumerate(FACTOR_EXPRESSIONS)]
        self.factor_expected_warmup = {
            f_def["name"]: get_expression_warmup(f_def["expr"])
            for f_def in self.factor_defs
        }
        self.actionable_factor_defs = [
            {"name": f"actionable_alpha_{i}", "expr": expr}
            for i, expr in enumerate(ACTIONABLE_FACTOR_EXPRESSIONS)
        ]
        self.actionable_factor_expected_warmup = {
            f_def["name"]: get_expression_warmup(f_def["expr"])
            for f_def in self.actionable_factor_defs
        }
        self.actionable_aggregate_factor_names = [
            f"actionable_alpha_{idx}"
            for idx in ACTIONABLE_AGGREGATE_FACTOR_INDEXES
            if idx < len(ACTIONABLE_FACTOR_EXPRESSIONS)
        ]
        self.actionable_aggregate_top_n = ACTIONABLE_AGGREGATE_TOP_N
        self.regime_symbols = []
        self.market_cache = MarketDataCache(max_days=self.lookback_days)
        self.factor_cache = FactorDataCache(max_days=max(self.lookback_days + 100, 500))
        self.actionable_factor_cache = FactorDataCache(max_days=max(self.lookback_days + 100, 500))
        self.last_actionable_factor_snapshot = None
        self.chip_cache = ChipFeatureCache(max_days=self.lookback_days)
        self.backtest_chip_rebalance_only = True
        
        # Deployment flags - set by main.py during deployment detection
        self.defer_first_rebalance = False  # True on pre/post market deploy
        self.is_pre_market = False  # True if deployed before market open
        self.is_post_market = False  # True if deployed after market close

    @staticmethod
    def _normalize_weights(weights: np.ndarray) -> np.ndarray:
        """Normalize weights by clipping negatives to zero."""
        weights = np.maximum(weights, 0.0)
        total = weights.sum()
        if total <= 1e-8:
            return np.ones_like(weights) / max(len(weights), 1)
        return weights / total

    def _latest_market_cache_date(self):
        """Best-effort latest date from close cache for report date alignment."""
        try:
            close_df = self.market_cache.get("close")
        except Exception:
            close_df = None
        if close_df is None or close_df.empty:
            return None
        try:
            idx = pd.to_datetime(close_df.index, errors="coerce")
            idx = idx[~pd.isna(idx)]
            if len(idx) == 0:
                return None
            return idx.max().date()
        except Exception:
            return None

    def _latest_market_cache_timestamp(self):
        """Best-effort latest timestamp from close cache for preview/finalize gating."""
        try:
            close_df = self.market_cache.get("close")
        except Exception:
            close_df = None
        if close_df is None or close_df.empty:
            return None
        try:
            idx = pd.to_datetime(close_df.index, errors="coerce")
            idx = idx[~pd.isna(idx)]
            if len(idx) == 0:
                return None
            return idx.max()
        except Exception:
            return None

    def _resolve_preview_report_date(self, current_date):
        """Use latest cache date in preview to avoid writing partial/future-dated rows."""
        if bool(self.is_post_market):
            try:
                return pd.Timestamp(current_date).date()
            except Exception:
                return current_date
        latest = self._latest_market_cache_date()
        if latest is None:
            return current_date
        try:
            cd = pd.Timestamp(current_date).date()
        except Exception:
            return latest
        if latest > cd:
            return cd
        return latest

    def _should_persist_preview_outputs(
        self,
        algorithm: QCAlgorithm,
        is_preview: bool,
        current_time,
        market_finalize,
        is_pre_market_now: bool,
        current_date=None,
        report_date=None,
    ) -> bool:
        """
        Preview runs should persist reports only when data is effectively finalized
        (post-close / after 15:55 / backtest).
        """
        if not is_preview:
            return True
        if not bool(getattr(algorithm, "LiveMode", False)):
            return True
        if bool(self.is_post_market):
            return True
        try:
            resolved_current_date = pd.Timestamp(current_date).date() if current_date is not None else None
            resolved_report_date = pd.Timestamp(report_date).date() if report_date is not None else None
        except Exception:
            resolved_current_date = None
            resolved_report_date = None
        if (
            resolved_current_date is not None
            and resolved_report_date is not None
            and resolved_report_date < resolved_current_date
        ):
            latest_cache_ts = self._latest_market_cache_timestamp()
            if latest_cache_ts is not None:
                latest_cache_ts = pd.Timestamp(latest_cache_ts)
                if (
                    latest_cache_ts.date() == resolved_report_date
                    and latest_cache_ts.time() >= market_finalize
                ):
                    return True
        return (not bool(is_pre_market_now)) and (current_time >= market_finalize)

    @staticmethod
    def _compute_cma_portfolio_series(
        rank_t: np.ndarray,
        ret_t: np.ndarray,
        weights: np.ndarray,
        top_k: int,
    ) -> tuple:
        """Compute daily long-short returns and daily IC series for a weight vector."""
        rank_t = np.asarray(rank_t, dtype=float)
        ret_t = np.asarray(ret_t, dtype=float)
        weights = np.asarray(weights, dtype=float)
        if rank_t.ndim != 3 or ret_t.ndim != 2:
            return np.array([], dtype=float), np.array([], dtype=float)
        if rank_t.shape[1] == 0 or rank_t.shape[2] == 0:
            return np.array([], dtype=float), np.array([], dtype=float)
        if weights.ndim != 1 or weights.shape[0] != rank_t.shape[0]:
            return np.array([], dtype=float), np.array([], dtype=float)

        sc = np.tensordot(weights, rank_t, axes=(0, 0))
        k = min(int(top_k), sc.shape[1])
        if k <= 0:
            return np.array([], dtype=float), np.array([], dtype=float)

        daily_rets = np.zeros(len(sc), dtype=float)
        daily_ic = np.zeros(len(sc), dtype=float)
        for t in range(len(sc)):
            row = sc[t]
            idx = np.argpartition(row, -k)[-k:]
            short_idx = np.argpartition(row, k - 1)[:k]
            long_ret = float(np.mean(ret_t[t, idx]))
            short_ret = float(np.mean(ret_t[t, short_idx]))
            daily_rets[t] = long_ret - short_ret
            if row.std() > 1e-8 and ret_t[t].std() > 1e-8:
                daily_ic[t] = float(np.corrcoef(row, ret_t[t])[0, 1])
            else:
                daily_ic[t] = 0.0
        return daily_rets, daily_ic

    def _compute_cma_objective_components(
        self,
        daily_rets: np.ndarray,
        daily_ic: np.ndarray,
        turnover: float,
        annualization: int = 252,
        turnover_floor: float = 0.125,
    ) -> dict:
        """
        Compute objective raw terms, weighted terms, penalties, and total.
        Uses exactly the same math as _compute_cma_objective_score.
        """
        daily_rets = np.asarray(daily_rets, dtype=float)
        daily_ic = np.asarray(daily_ic, dtype=float)
        if daily_rets.size == 0:
            return {"total": float("-inf")}

        log_ret = float(np.log1p(daily_rets).sum())
        ic_mean = float(np.nanmean(daily_ic)) if daily_ic.size else 0.0
        ic_std = float(np.nanstd(daily_ic)) if daily_ic.size else 0.0
        icir = ic_mean / (ic_std + 1e-8) if ic_std > 1e-12 else 0.0
        icir_penalty = max(0.0, self.cma_icir_floor - icir)

        if not np.isfinite(turnover):
            turnover = 0.0
        denom_turnover = max(float(turnover_floor), float(turnover))

        mean_ret = float(np.nanmean(daily_rets))
        std_ret = float(np.nanstd(daily_rets))
        annual_return = float(mean_ret * annualization)
        if np.isfinite(std_ret) and std_ret > 1e-12:
            sharpe = float((mean_ret / std_ret) * np.sqrt(annualization))
        else:
            sharpe = 0.0
        fitness_score = float(sharpe * np.sqrt(abs(annual_return) / denom_turnover))
        if not np.isfinite(fitness_score):
            fitness_score = 0.0

        weighted_log_ret = float(self.cma_obj_logret_weight * log_ret)
        weighted_ic = float(self.cma_obj_ic_weight * ic_mean)
        weighted_fitness = float(self.cma_obj_fitness_weight * fitness_score)
        turnover_penalty_term = float(self.cma_turnover_penalty * turnover)
        icir_penalty_term = float(self.cma_icir_penalty * icir_penalty)

        total = (
            weighted_log_ret
            + weighted_ic
            + weighted_fitness
            - turnover_penalty_term
            - icir_penalty_term
        )
        raw_terms = {
            "log_ret": float(log_ret),
            "ic_mean": float(ic_mean),
            "ic_std": float(ic_std),
            "icir": float(icir),
            "fitness": float(fitness_score),
            "sharpe": float(sharpe),
            "annual_return": float(annual_return),
            "turnover": float(turnover),
        }
        penalties = {
            "icir_shortfall": float(icir_penalty),
        }
        weighted_terms = {
            "log_ret": weighted_log_ret,
            "ic": weighted_ic,
            "fitness": weighted_fitness,
        }
        penalty_terms = {
            "turnover_penalty": turnover_penalty_term,
            "icir_penalty": icir_penalty_term,
        }

        # Include flat keys for backward compatibility with existing report readers.
        return {
            "raw_terms": raw_terms,
            "penalties": penalties,
            "weighted_terms": weighted_terms,
            "penalty_terms": penalty_terms,
            "total": float(total),
            "log_ret_raw": raw_terms["log_ret"],
            "ic_mean_raw": raw_terms["ic_mean"],
            "ic_std_raw": raw_terms["ic_std"],
            "icir_raw": raw_terms["icir"],
            "fitness_raw": raw_terms["fitness"],
            "sharpe_raw": raw_terms["sharpe"],
            "annual_return_raw": raw_terms["annual_return"],
            "turnover_raw": raw_terms["turnover"],
            "icir_penalty_raw": penalties["icir_shortfall"],
            "weighted_log_ret": weighted_terms["log_ret"],
            "weighted_ic": weighted_terms["ic"],
            "weighted_fitness": weighted_terms["fitness"],
            "turnover_penalty_term": penalty_terms["turnover_penalty"],
            "icir_penalty_term": penalty_terms["icir_penalty"],
        }

    def _compute_per_factor_cma_metrics(
        self,
        rank_t: Optional[np.ndarray],
        ret_t: Optional[np.ndarray],
        factor_names: Optional[List[str]],
        weights_map: Optional[Dict[str, float]],
        score_mode: str,
    ) -> tuple:
        """Compute standalone per-factor CMA objective terms on the active CMA tensor."""
        names = [str(n) for n in (factor_names or []) if isinstance(n, str) and n]
        window_days = 0
        if isinstance(rank_t, np.ndarray) and rank_t.ndim == 3:
            window_days = int(rank_t.shape[1])
        elif isinstance(ret_t, np.ndarray) and ret_t.ndim == 2:
            window_days = int(ret_t.shape[0])
        if window_days <= 0:
            window_days = int(self.eval_window)

        base_meta = {
            "window_days_used": int(window_days),
            "score_mode": str(score_mode),
            "available": False,
            "unavailable_reason": "",
        }
        if not isinstance(rank_t, np.ndarray) or not isinstance(ret_t, np.ndarray):
            base_meta["unavailable_reason"] = "cma_tensor_missing"
            return [], base_meta
        if rank_t.ndim != 3 or ret_t.ndim != 2:
            base_meta["unavailable_reason"] = "cma_tensor_shape_invalid"
            return [], base_meta
        if rank_t.shape[1] != ret_t.shape[0]:
            base_meta["unavailable_reason"] = "cma_tensor_row_mismatch"
            return [], base_meta
        if rank_t.shape[2] != ret_t.shape[1]:
            base_meta["unavailable_reason"] = "cma_tensor_col_mismatch"
            return [], base_meta

        n_factors = int(rank_t.shape[0])
        if n_factors <= 0:
            base_meta["unavailable_reason"] = "no_cma_factors"
            return [], base_meta

        if not names:
            names = [f"factor_{i}" for i in range(n_factors)]
        if len(names) != n_factors:
            base_meta["unavailable_reason"] = "factor_name_count_mismatch"
            return [], base_meta

        weights = weights_map if isinstance(weights_map, dict) else {}

        def _finite_or_none(value):
            try:
                fv = float(value)
            except Exception:
                return None
            return fv if np.isfinite(fv) else None

        rows: List[dict] = []
        for i, name in enumerate(names):
            one_hot = np.zeros(n_factors, dtype=float)
            one_hot[i] = 1.0
            daily_rets, daily_ic = self._compute_cma_portfolio_series(
                rank_t,
                ret_t,
                one_hot,
                self.cma_top_bottom,
            )
            if daily_rets.size == 0:
                rows.append(
                    {
                        "factor": str(name),
                        "weight": _finite_or_none(weights.get(name, 0.0)) or 0.0,
                        "log_ret": None,
                        "ic_mean": None,
                        "icir": None,
                        "fitness": None,
                        "sharpe": None,
                        "annual_return": None,
                        "cma_total": None,
                    }
                )
                continue
            components = self._compute_cma_objective_components(
                daily_rets=daily_rets,
                daily_ic=daily_ic,
                turnover=0.0,
            )
            rows.append(
                {
                    "factor": str(name),
                    "weight": _finite_or_none(weights.get(name, 0.0)) or 0.0,
                    "log_ret": _finite_or_none(components.get("log_ret_raw")),
                    "ic_mean": _finite_or_none(components.get("ic_mean_raw")),
                    "icir": _finite_or_none(components.get("icir_raw")),
                    "fitness": _finite_or_none(components.get("fitness_raw")),
                    "sharpe": _finite_or_none(components.get("sharpe_raw")),
                    "annual_return": _finite_or_none(components.get("annual_return_raw")),
                    "cma_total": _finite_or_none(components.get("total")),
                }
            )

        base_meta["available"] = True
        base_meta["unavailable_reason"] = ""
        return rows, base_meta

    def _compute_cma_objective_score(
        self,
        daily_rets: np.ndarray,
        daily_ic: np.ndarray,
        turnover: float,
        annualization: int = 252,
        turnover_floor: float = 0.125,
    ) -> float:
        """
        Three-term CMA objective:
        score = w_logret*log_ret + w_ic*ic_mean + w_fit*fitness - penalties
        """
        components = self._compute_cma_objective_components(
            daily_rets=daily_rets,
            daily_ic=daily_ic,
            turnover=turnover,
            annualization=annualization,
            turnover_floor=turnover_floor,
        )
        return float(components.get("total", float("-inf")))

    @staticmethod
    def _compute_weighted_factor_portfolio_metrics(
        factor_window: pd.DataFrame,
        return_window: pd.DataFrame,
        annualization: int = 252,
        turnover_floor: float = 0.125,
    ) -> Dict[str, object]:
        """
        Brain-style weighted long/short factor portfolio metrics.

        Portfolio construction:
        - long top half, short bottom half
        - position size proportional to distance from median rank
        """
        empty = pd.Series(dtype=float)
        if (
            factor_window is None
            or return_window is None
            or not isinstance(factor_window, pd.DataFrame)
            or not isinstance(return_window, pd.DataFrame)
            or factor_window.empty
            or return_window.empty
        ):
            return {
                "daily_returns": empty,
                "turnover": 0.0,
                "sharpe": 0.0,
                "fitness": 0.0,
                "annual_return": 0.0,
            }

        factor_aligned, ret_aligned = factor_window.align(return_window, join="inner", axis=0)
        factor_aligned, ret_aligned = factor_aligned.align(ret_aligned, join="inner", axis=1)
        if factor_aligned.empty or ret_aligned.empty:
            return {
                "daily_returns": empty,
                "turnover": 0.0,
                "sharpe": 0.0,
                "fitness": 0.0,
                "annual_return": 0.0,
            }

        rank_pct = factor_aligned.rank(axis=1, pct=True)
        weights = (rank_pct - 0.5).where(np.isfinite(rank_pct), 0.0)

        gross = weights.abs().sum(axis=1).replace(0.0, np.nan)
        weights = weights.div(gross, axis=0).fillna(0.0)

        valid_rets = ret_aligned.where(np.isfinite(ret_aligned))
        weights = weights.where(valid_rets.notna(), 0.0)
        gross_valid = weights.abs().sum(axis=1).replace(0.0, np.nan)
        weights = weights.div(gross_valid, axis=0).fillna(0.0)

        daily_returns = (weights * valid_rets.fillna(0.0)).sum(axis=1)
        if daily_returns.empty:
            return {
                "daily_returns": daily_returns,
                "turnover": 0.0,
                "sharpe": 0.0,
                "fitness": 0.0,
                "annual_return": 0.0,
            }

        prev_w = weights.shift(1).fillna(0.0)
        daily_turnover = 0.5 * (weights - prev_w).abs().sum(axis=1)
        if len(daily_turnover) > 1:
            turnover = float(daily_turnover.iloc[1:].mean())
        else:
            turnover = float(daily_turnover.mean())
        if not np.isfinite(turnover):
            turnover = 0.0

        mean_ret = float(daily_returns.mean())
        std_ret = float(daily_returns.std(ddof=0))
        annual_return = float(mean_ret * annualization)
        if np.isfinite(std_ret) and std_ret > 1e-12:
            sharpe = float((mean_ret / std_ret) * np.sqrt(annualization))
        else:
            sharpe = 0.0

        denom_turnover = max(float(turnover_floor), float(turnover))
        if denom_turnover <= 0 or not np.isfinite(denom_turnover):
            denom_turnover = float(turnover_floor)
        fitness = float(sharpe * np.sqrt(abs(annual_return) / denom_turnover))
        if not np.isfinite(fitness):
            fitness = 0.0
        if not np.isfinite(sharpe):
            sharpe = 0.0
        if not np.isfinite(annual_return):
            annual_return = 0.0

        return {
            "daily_returns": daily_returns,
            "turnover": turnover,
            "sharpe": sharpe,
            "fitness": fitness,
            "annual_return": annual_return,
        }

    @staticmethod
    def _clean_factor_values(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return df
        cleaned = df.ffill()
        if cleaned.shape[0] > 0:
            first_row = cleaned.iloc[0]
            if first_row.isna().any():
                cleaned = cleaned.copy()
                for col in first_row.index[first_row.isna()]:
                    series = cleaned[col]
                    first_valid = series.first_valid_index()
                    if first_valid is not None:
                        cleaned.iat[0, cleaned.columns.get_loc(col)] = series.loc[first_valid]
        if cleaned.isna().all(axis=0).any():
            cleaned = cleaned.fillna(0.0)
        return cleaned

    @staticmethod
    def _report_date_text(value) -> str:
        if value is None:
            return ""
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        try:
            parsed = pd.Timestamp(value)
        except Exception:
            return str(value).strip()[:10]
        if pd.isna(parsed):
            return ""
        return parsed.strftime("%Y-%m-%d")

    @classmethod
    def _resolve_factor_panel_data_as_of_date(
        cls,
        factor_values_map: Optional[Dict[str, pd.DataFrame]],
        factor_names: Optional[List[str]] = None,
        fallback=None,
    ) -> str:
        fallback_text = cls._report_date_text(fallback)
        if not isinstance(factor_values_map, dict) or not factor_values_map:
            return fallback_text

        selected_names = [str(name) for name in list(factor_names or []) if str(name)]
        frames = (
            [factor_values_map.get(name) for name in selected_names]
            if selected_names
            else list(factor_values_map.values())
        )

        latest_dates = []
        for frame in frames:
            if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            sorted_frame = frame.sort_index()
            latest_date = cls._report_date_text(sorted_frame.index[-1])
            if latest_date:
                latest_dates.append(latest_date)

        if latest_dates:
            # Use the earliest factor snapshot date so partially stale scoring inputs stay visible.
            return min(latest_dates)
        return fallback_text

    @staticmethod
    def _build_actionable_aggregate_summary(
        latest_by_factor: Dict[str, pd.Series],
        selected_factors: List[str],
        top_n: int = 10,
    ) -> dict:
        top_counts: Dict[str, int] = {}
        bottom_counts: Dict[str, int] = {}
        combined_scores: Dict[str, float] = {}
        per_factor: Dict[str, dict] = {}
        factors_used: List[str] = []

        for factor_name in selected_factors:
            latest = latest_by_factor.get(factor_name)
            if latest is None:
                continue
            latest = pd.to_numeric(latest, errors="coerce")
            finite = latest[np.isfinite(latest.to_numpy(dtype=float))]
            if finite.empty:
                continue

            n = min(int(top_n), len(finite))
            if n <= 0:
                continue

            top_series = finite.nlargest(n)
            bottom_source = finite.drop(index=top_series.index, errors="ignore")
            if bottom_source.empty:
                bottom_source = finite
            bottom_series = bottom_source.nsmallest(min(n, len(bottom_source)))

            top_weights = np.arange(len(top_series), 0, -1, dtype=float)
            top_weights /= max(top_weights.sum(), 1.0)
            bottom_weights = np.arange(len(bottom_series), 0, -1, dtype=float)
            bottom_weights /= max(bottom_weights.sum(), 1.0)

            top_items = []
            for rank, ((symbol, value), weight) in enumerate(zip(top_series.items(), top_weights), start=1):
                sym = AlphaSAGEAlphaModel._symbol_label(symbol)
                weight = float(weight)
                top_counts[sym] = top_counts.get(sym, 0) + 1
                combined_scores[sym] = combined_scores.get(sym, 0.0) + weight
                top_items.append(
                    {
                        "symbol": sym,
                        "value": float(value),
                        "rank": int(rank),
                        "weight": weight,
                    }
                )

            bottom_items = []
            for rank, ((symbol, value), weight) in enumerate(zip(bottom_series.items(), bottom_weights), start=1):
                sym = AlphaSAGEAlphaModel._symbol_label(symbol)
                weight = float(weight)
                bottom_counts[sym] = bottom_counts.get(sym, 0) + 1
                combined_scores[sym] = combined_scores.get(sym, 0.0) - weight
                bottom_items.append(
                    {
                        "symbol": sym,
                        "value": float(value),
                        "rank": int(rank),
                        "weight": weight,
                    }
                )

            per_factor[factor_name] = {
                "top": top_items,
                "bottom": bottom_items,
            }
            factors_used.append(factor_name)

        top_count_rows = [
            {"symbol": sym, "count": int(count)}
            for sym, count in sorted(top_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        bottom_count_rows = [
            {"symbol": sym, "count": int(count)}
            for sym, count in sorted(bottom_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        combined_rank_rows = [
            {
                "symbol": sym,
                "score": float(score),
                "top_count": int(top_counts.get(sym, 0)),
                "bottom_count": int(bottom_counts.get(sym, 0)),
            }
            for sym, score in sorted(combined_scores.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

        return {
            "selected_factors": list(selected_factors),
            "factors_used": factors_used,
            "top_n": int(top_n),
            "top_counts": top_count_rows,
            "bottom_counts": bottom_count_rows,
            "combined_rank": combined_rank_rows,
            "per_factor": per_factor,
        }

    def _log_data_format(self, algorithm, label: str, df, is_multiindex: bool = False):
        """Log DataFrame format details: shape, index type, NaN count, date range."""
        if df is None:
            algorithm.Debug(f"[DataFormat] {label}: None")
            return
        if isinstance(df, pd.DataFrame):
            if df.empty:
                algorithm.Debug(f"[DataFormat] {label}: EMPTY DataFrame")
                return
            nan_count = int(df.isna().sum().sum())
            nan_pct = 100 * nan_count / (df.shape[0] * df.shape[1]) if df.size > 0 else 0
            if is_multiindex and isinstance(df.index, pd.MultiIndex):
                symbols = df.index.get_level_values(0).unique()
                times = df.index.get_level_values(1)
                algorithm.Debug(
                    f"[DataFormat] {label}: MultiIndex[symbols={len(symbols)}, time] "
                    f"shape={df.shape} cols={list(df.columns)} "
                    f"dates=[{times.min()}...{times.max()}]"
                )
            else:
                idx = df.index
                algorithm.Debug(
                    f"[DataFormat] {label}: shape={df.shape} NaN={nan_count} ({nan_pct:.1f}%) "
                    f"dates=[{idx[0]}...{idx[-1]}] cols={len(df.columns)}"
                )
        elif isinstance(df, pd.Series):
            nan_count = int(df.isna().sum())
            algorithm.Debug(f"[DataFormat] {label}: Series len={len(df)} NaN={nan_count}")
        else:
            algorithm.Debug(f"[DataFormat] {label}: type={type(df)}")

    @staticmethod
    def _max_true_run(mask: np.ndarray) -> int:
        max_run = 0
        run = 0
        for val in mask:
            if val:
                run += 1
                if run > max_run:
                    max_run = run
            else:
                run = 0
        return max_run

    def _log_missing_gaps(
        self,
        algorithm,
        label: str,
        df: pd.DataFrame,
        max_symbols: int = 30,
        min_missing_pct: float = 0.02,
        min_gap: int = 3,
        treat_zero_as_missing: bool = False,
    ) -> None:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return
        total = int(len(df.index))
        if total == 0:
            return
        missing_mask = df.isna()
        if treat_zero_as_missing:
            missing_mask = missing_mask | (df <= 0)
        missing_counts = missing_mask.sum(axis=0)
        offenders = []
        for col in df.columns:
            miss = int(missing_counts.get(col, 0))
            if miss == 0:
                continue
            col_mask = missing_mask[col].to_numpy()
            max_gap = self._max_true_run(col_mask)
            miss_pct = miss / total
            offenders.append((col, miss, miss_pct, max_gap))
        if not offenders:
            return
        offenders.sort(key=lambda x: (x[1], x[3]), reverse=True)
        total_offenders = len(offenders)
        if max_symbols <= 0 or total_offenders <= max_symbols:
            sample = offenders
        else:
            sample = offenders[:max_symbols]
        parts = []
        for col, miss, miss_pct, max_gap in sample:
            col_label = getattr(col, "Value", str(col))
            parts.append(f"{col_label} miss={miss}/{total} ({miss_pct * 100:.1f}%) gap={max_gap}")
        algorithm.Debug(
            f"[DataFormat] {label} gaps: {', '.join(parts)} shown={len(sample)}/{total_offenders}"
        )

    def _calc_nan_stats(self, df: pd.DataFrame, last_row: bool = False):
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        if last_row:
            row = df.iloc[-1]
            total = int(row.size)
            if total == 0:
                return None
            nan_count = int(row.isna().sum())
        else:
            total = int(df.size)
            if total == 0:
                return None
            nan_count = int(df.isna().sum().sum())
        nan_pct = 100 * nan_count / total if total > 0 else 0
        return nan_count, total, nan_pct

    def _log_ohlcv_history_quality(
        self,
        algorithm,
        frames: Dict[str, pd.DataFrame],
        max_missing_pct: float = 1.0,
    ) -> None:
        max_pct = 0.0
        has_issue = False
        for df in frames.values():
            stats = self._calc_nan_stats(df)
            if stats is None:
                has_issue = True
                continue
            _, _, nan_pct = stats
            max_pct = max(max_pct, nan_pct)
            if nan_pct > max_missing_pct:
                has_issue = True
        if not has_issue:
            algorithm.Debug(
                f"[DataQuality] OHLCV history OK (no missing > {max_missing_pct:.1f}%, max NaN {max_pct:.1f}%)"
            )
            return
        for label, df in frames.items():
            self._log_data_format(algorithm, label, df)
            treat_zero = "volume" in label
            self._log_missing_gaps(algorithm, label, df, treat_zero_as_missing=treat_zero)

    def _log_ohlcv_snapshot_quality(
        self,
        algorithm,
        frames: Dict[str, pd.DataFrame],
        max_missing_pct: float = 1.0,
    ) -> None:
        max_pct = 0.0
        has_issue = False
        missing_labels = []
        for label, df in frames.items():
            stats = self._calc_nan_stats(df, last_row=True)
            if stats is None:
                missing_labels.append(label)
                has_issue = True
                continue
            _, _, nan_pct = stats
            max_pct = max(max_pct, nan_pct)
            if nan_pct > max_missing_pct:
                has_issue = True
        if not has_issue:
            algorithm.Debug(
                f"[DataQuality] OHLCV snapshot OK (no missing > {max_missing_pct:.1f}%, max NaN {max_pct:.1f}%)"
            )
            return
        if missing_labels:
            algorithm.Debug(f"[DataQuality] OHLCV snapshot missing: {', '.join(missing_labels)}")
        for label, df in frames.items():
            self._log_cross_section_counts(algorithm, f"OHLCV {label}", df)

    def _log_cross_section_counts(self, algorithm, label: str, df: pd.DataFrame) -> None:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return
        last = df.iloc[-1]
        total = int(last.size)
        if total == 0:
            return
        nan_count = int(last.isna().sum())
        zero_count = int((last == 0).sum())
        algorithm.Debug(
            f"[XS] {label} last: nan={nan_count}/{total} zero={zero_count}/{total}"
        )

    def _log_intraday_features(self, algorithm, data_dict: dict):
        """Log NaN diagnostics for all intraday features."""
        intraday_feats = ['drift_factor', 'amihud_mean', 'amihud_range', 'vol_fft', 'ohl', 'chl', 'ohlc', 'chlo']
        stats_map = {}
        max_pct = 0.0
        has_issue = False
        for feat in intraday_feats:
            key = f'${feat}'
            df = data_dict.get(key)
            stats = self._calc_nan_stats(df)
            if stats is None:
                has_issue = True
                continue
            nan_count, total, nan_pct = stats
            stats_map[feat] = (df.shape, nan_count, total, nan_pct)
            max_pct = max(max_pct, nan_pct)
            if nan_pct > 1.0:
                has_issue = True
        if not stats_map:
            return
        if not has_issue:
            algorithm.Debug(
                f"[DataQuality] Intraday features OK (no missing > 1.0%, max NaN {max_pct:.1f}%)"
            )
            return
        for feat, (shape, nan_count, _, nan_pct) in stats_map.items():
            algorithm.Debug(
                f"[NaN Diagnostic] {feat}: shape={shape}, NaN={nan_count} ({nan_pct:.1f}%)"
            )
        missing = [feat for feat in intraday_feats if feat not in stats_map]
        if missing:
            algorithm.Debug(f"[IntradayQuality] Missing features: {', '.join(missing)}")

    def _compute_actionable_shadow_factors(
        self,
        algorithm,
        data_dict: dict,
        available_dates,
        columns,
    ) -> None:
        if not self.actionable_factor_defs:
            return

        def _index_pos(index, value):
            pos = index.get_loc(value)
            if isinstance(pos, slice):
                return pos.start
            if isinstance(pos, np.ndarray):
                return int(pos[0])
            return int(pos)

        def _slice_data_dict(idx):
            sliced = {}
            for key, val in data_dict.items():
                if isinstance(val, pd.DataFrame):
                    sliced[key] = val.reindex(index=idx)
                elif isinstance(val, pd.Series):
                    sliced[key] = val.reindex(index=idx)
                else:
                    sliced[key] = val
            return sliced

        latest_summary = []
        latest_snapshot = {}
        latest_by_factor = {}
        for f_def in self.actionable_factor_defs:
            name, expr = f_def["name"], f_def["expr"]
            try:
                cached = self.actionable_factor_cache.get_factor_values(name)
                if cached is None or cached.empty:
                    new_dates = list(available_dates)
                else:
                    new_dates = [d for d in available_dates if d > cached.index[-1]]
                    missing_dates = [d for d in available_dates if d not in cached.index]
                    if missing_dates:
                        new_dates = sorted(set(new_dates + missing_dates))
                        new_dates = [d for d in available_dates if d in new_dates]

                if new_dates:
                    warmup = self.actionable_factor_expected_warmup.get(name, 0)
                    first_new = new_dates[0]
                    last_new = new_dates[-1]
                    first_pos = _index_pos(available_dates, first_new)
                    last_pos = _index_pos(available_dates, last_new)
                    # Rolling/shift operators need one extra prior row in single-day incremental mode.
                    # Without this, newest-row values can become all-NaN for nested expressions.
                    start_pos = max(0, first_pos - warmup - 1)
                    subset_index = available_dates[start_pos:last_pos + 1]
                    engine = FactorEngine(_slice_data_dict(subset_index))
                    fv_slice = engine.parse_expression(expr)
                    if isinstance(fv_slice, pd.DataFrame) and not fv_slice.empty:
                        fv_new = fv_slice.reindex(index=new_dates)
                    else:
                        fv_new = pd.DataFrame(np.nan, index=new_dates, columns=columns)
                    self.actionable_factor_cache.update_factor_values(name, fv_new)

                fv_cached = self.actionable_factor_cache.get_factor_values(name)
                if isinstance(fv_cached, pd.DataFrame) and not fv_cached.empty:
                    fv = fv_cached.reindex(index=available_dates, columns=columns)
                else:
                    fv = pd.DataFrame(np.nan, index=available_dates, columns=columns)
                fv = self._clean_factor_values(fv)

                latest = pd.to_numeric(fv.iloc[-1], errors="coerce")
                latest_by_factor[name] = latest
                total = int(latest.size)
                nan_count = int(latest.isna().sum())
                latest_summary.append(f"{name}=nan {nan_count}/{total}")
                finite = latest[np.isfinite(latest.to_numpy(dtype=float))]
                if not finite.empty:
                    top_n = min(10, len(finite))
                    top_series = finite.nlargest(top_n)
                    bottom_series = finite.nsmallest(top_n)

                    top_items = [
                        {
                            "symbol": self._symbol_label(symbol),
                            "value": float(value),
                        }
                        for symbol, value in top_series.items()
                    ]
                    bottom_items = [
                        {
                            "symbol": self._symbol_label(symbol),
                            "value": float(value),
                        }
                        for symbol, value in bottom_series.items()
                    ]
                    top_preview = ", ".join(
                        f"{item['symbol']}={item['value']:.6g}" for item in top_items
                    )
                    bottom_preview = ", ".join(
                        f"{item['symbol']}={item['value']:.6g}" for item in bottom_items
                    )
                    algorithm.Debug(
                        f"[ActionableFactor] {name} long(top)=[{top_preview}] "
                        f"short(bottom)=[{bottom_preview}]"
                    )
                    latest_snapshot[name] = {
                        "expr": expr,
                        "top": top_items,
                        "bottom": bottom_items,
                    }
                else:
                    latest_snapshot[name] = {
                        "expr": expr,
                        "top": [],
                        "bottom": [],
                    }
            except Exception as e:
                algorithm.Debug(f"[ActionableFactor] {name} error: {e}")

        aggregate_summary = self._build_actionable_aggregate_summary(
            latest_by_factor,
            selected_factors=self.actionable_aggregate_factor_names,
            top_n=self.actionable_aggregate_top_n,
        )
        if aggregate_summary.get("factors_used"):
            top_counts_preview = ", ".join(
                f"{row['symbol']}={row['count']}"
                for row in aggregate_summary["top_counts"][:10]
            )
            bottom_counts_preview = ", ".join(
                f"{row['symbol']}={row['count']}"
                for row in aggregate_summary["bottom_counts"][:10]
            )
            combined_rank = aggregate_summary["combined_rank"]
            combined_top_preview = ", ".join(
                f"{row['symbol']}={row['score']:.6g}"
                for row in combined_rank[:10]
            )
            combined_bottom_preview = ", ".join(
                f"{row['symbol']}={row['score']:.6g}"
                for row in combined_rank[-10:]
            )
            factors_label = ",".join(aggregate_summary["factors_used"])
            top_n = aggregate_summary.get("top_n", self.actionable_aggregate_top_n)
            algorithm.Debug(
                f"[ActionableAggregate] factors=[{factors_label}] "
                f"appear_top{top_n}=[{top_counts_preview}] "
                f"appear_bottom{top_n}=[{bottom_counts_preview}]"
            )
            algorithm.Debug(
                f"[ActionableAggregate] combined_rank_top=[{combined_top_preview}] "
                f"combined_rank_bottom=[{combined_bottom_preview}]"
            )

        self.actionable_factor_cache.trim_all()
        if latest_summary:
            algorithm.Debug(f"[ActionableFactor] latest XS stats: {', '.join(latest_summary)}")
            self.last_actionable_factor_snapshot = {
                "date": str(available_dates[-1]),
                "factors": latest_snapshot,
                "aggregate": aggregate_summary,
            }

    def _log_cmaes_input_quality(self, algorithm, diag: dict) -> None:
        if not diag:
            return
        num_days = diag.get("num_days", 0)
        num_symbols = diag.get("num_symbols", 0)
        total_symbols = diag.get("total_symbols", num_symbols)
        masked_symbols = diag.get("masked_symbols", max(total_symbols - num_symbols, 0))
        num_factors = diag.get("num_factors", 0)
        long_days = diag.get("long_days", 0)
        min_required = diag.get("min_required_days", 0)
        configured_min = diag.get("configured_min_days", min_required)
        if num_days and num_symbols and num_factors:
            total_rank = num_days * num_symbols * num_factors
            total_ret = num_days * num_symbols
            k = min(self.cma_top_bottom, num_symbols)
            used_returns = num_days * k * 2
            algorithm.Debug(
                f"[CMA-ES] training window: days={num_days} symbols={num_symbols}/{total_symbols} "
                f"masked={masked_symbols} factors={num_factors} ranks={total_rank} returns={total_ret} "
                f"used_returns={used_returns} (top/bot {k})"
            )
            if min_required and configured_min and min_required < configured_min:
                algorithm.Debug(
                    f"[CMA-ES] min valid days relaxed: required={min_required}/{configured_min} "
                    f"(available={num_days} days)"
                )
            if long_days:
                algorithm.Debug(
                    f"[CMA-ES] long horizon usable days={long_days} "
                    f"(horizon={self.cma_forward_horizon}d)"
                )
        rank_nan = diag.get("rank_nan", 0)
        rank_total = diag.get("rank_total", 0)
        ret_nan = diag.get("ret_nan", 0)
        ret_total = diag.get("ret_total", 0)
        rank_pct = 100 * rank_nan / rank_total if rank_total > 0 else 0
        ret_pct = 100 * ret_nan / ret_total if ret_total > 0 else 0
        if rank_nan == 0 and ret_nan == 0:
            algorithm.Debug(
                f"[DataQuality] CMA-ES inputs OK (no NaNs, ranks={rank_total}, returns={ret_total})"
            )
        else:
            algorithm.Debug(
                f"[CMA-ES] input NaNs: ranks={rank_nan}/{rank_total} ({rank_pct:.1f}%) "
                f"returns={ret_nan}/{ret_total} ({ret_pct:.1f}%)"
            )
        missing_factors = diag.get("missing_factors", [])
        if missing_factors:
            algorithm.Debug(f"[CMA-ES] missing factor data: {', '.join(missing_factors)}")
        dropped_factors = diag.get("dropped_factors", {})
        if dropped_factors:
            threshold = diag.get("factor_min_coverage", None)
            dropped_items = sorted(dropped_factors.items(), key=lambda x: x[1])
            preview = ", ".join(f"{name}={cov:.2f}" for name, cov in dropped_items[:6])
            if threshold is not None:
                algorithm.Debug(
                    f"[CMA-ES] dropped factors (coverage<{threshold:.2f}): {preview}"
                )
            else:
                algorithm.Debug(f"[CMA-ES] dropped factors: {preview}")

    def _fill_all_nan_rows(self, algorithm, feat: str, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return df
        all_nan_mask = df.isna().all(axis=1)
        if not all_nan_mask.any():
            return df
        nan_dates = df.index[all_nan_mask]
        sample_dates = list(nan_dates[-3:]) if len(nan_dates) > 0 else []
        algorithm.Debug(
            f"[IntradayDiag] {feat} all-NaN rows={len(nan_dates)} sample={sample_dates}"
        )
        filled = df.ffill()
        df = df.copy()
        df.loc[all_nan_mask] = filled.loc[all_nan_mask]
        return df

    def _log_returns_nan_stats(self, algorithm, label: str, df: pd.DataFrame) -> None:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            algorithm.Debug(f"[ReturnDiag] {label}: EMPTY")
            return
        total_cells = int(df.size)
        nan_cells = int(df.isna().sum().sum())
        nan_pct = (nan_cells / total_cells) * 100 if total_cells > 0 else 0
        latest_nans = int(df.iloc[-1].isna().sum()) if not df.empty else 0
        latest_pct = (latest_nans / df.shape[1]) * 100 if df.shape[1] > 0 else 0
        clean_mask = ~df.isna().any(axis=1)
        clean_days = int(clean_mask.sum())
        total_days = int(df.shape[0])
        if clean_days > 0:
            clean_index = df.index[clean_mask]
            clean_range = f"{clean_index[0]}..{clean_index[-1]}"
        else:
            clean_range = "none"
        algorithm.Debug(
            f"[ReturnDiag] {label}: Total NaN={nan_cells}/{total_cells} ({nan_pct:.1f}%) | "
            f"Latest NaN={latest_nans}/{df.shape[1]} ({latest_pct:.1f}%) | "
            f"CleanDays={clean_days}/{total_days} range={clean_range}"
        )

    def _log_intraday_feature_health(self, algorithm, feat: str, df: pd.DataFrame, recent_days: int = 5) -> None:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return
        recent = df.tail(min(recent_days, len(df.index)))
        if recent.empty:
            return
        all_nan_recent = recent.isna().all(axis=1)
        if all_nan_recent.any():
            bad_dates = list(recent.index[all_nan_recent])
            algorithm.Debug(f"[IntradayDiag] {feat} all-NaN dates in last {recent_days}: {bad_dates}")

        last = df.iloc[-1]
        nan_mask = last.isna()
        if nan_mask.any():
            nan_count = int(nan_mask.sum())
            cols = df.columns[nan_mask]
            sample = []
            for col in cols[:8]:
                sample.append(getattr(col, "Value", str(col)))
            algorithm.Debug(
                f"[IntradayDiag] {feat} last row NaN {nan_count}/{len(last)} sample={sample}"
            )

    def _log_row_stats(self, algorithm, label: str, obj) -> None:
        if obj is None:
            algorithm.Debug(f"[AlphaDiag] {label}: MISSING")
            return
        if isinstance(obj, pd.DataFrame):
            if obj.empty:
                algorithm.Debug(f"[AlphaDiag] {label}: EMPTY")
                return
            last = obj.iloc[-1]
            total = int(last.size)
            nan_count = int(last.isna().sum())
            zero_count = int((last == 0).sum())
            inf_count = int(np.isinf(last).sum())
            finite = last.replace([np.inf, -np.inf], np.nan).dropna()
            if finite.empty:
                min_val = np.nan
                max_val = np.nan
            else:
                min_val = float(finite.min())
                max_val = float(finite.max())
            algorithm.Debug(
                f"[AlphaDiag] {label} last: nan={nan_count}/{total} "
                f"zero={zero_count}/{total} inf={inf_count}/{total} "
                f"min={min_val:.4g} max={max_val:.4g}"
            )
            return
        if isinstance(obj, pd.Series):
            if obj.empty:
                algorithm.Debug(f"[AlphaDiag] {label}: EMPTY")
                return
            total = int(obj.size)
            nan_count = int(obj.isna().sum())
            zero_count = int((obj == 0).sum())
            inf_count = int(np.isinf(obj).sum())
            finite = obj.replace([np.inf, -np.inf], np.nan).dropna()
            if finite.empty:
                min_val = np.nan
                max_val = np.nan
            else:
                min_val = float(finite.min())
                max_val = float(finite.max())
            algorithm.Debug(
                f"[AlphaDiag] {label} last: nan={nan_count}/{total} "
                f"zero={zero_count}/{total} inf={inf_count}/{total} "
                f"min={min_val:.4g} max={max_val:.4g}"
            )
            return
        algorithm.Debug(f"[AlphaDiag] {label}: type={type(obj)}")

    def _log_ohlcv_last_row_stats(self, algorithm, frames: Dict[str, pd.DataFrame], zero_threshold: float = 0.5) -> None:
        for name, df in frames.items():
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                continue
            last = df.iloc[-1]
            total = int(last.size)
            if total == 0:
                continue
            nan_count = int(last.isna().sum())
            zero_count = int((last == 0).sum())
            if (nan_count + zero_count) / total >= zero_threshold:
                self._log_row_stats(algorithm, f"OHLCV {name}", df)

    def _has_recent_nonzero_volume(self, algorithm, volumes: pd.DataFrame, lookback_days: int = 5) -> bool:
        if volumes is None or volumes.empty:
            algorithm.Debug("[Generate] Skipping factor compute: volume history empty")
            return False
        lookback = min(int(lookback_days), len(volumes.index))
        if lookback <= 0:
            algorithm.Debug("[Generate] Skipping factor compute: no volume rows")
            return False
        recent = volumes.tail(lookback)
        nonzero_count = int((recent > 0).sum().sum())
        if nonzero_count > 0:
            return True
        total_cells = int(recent.size)
        nan_count = int(recent.isna().sum().sum())
        zero_count = int((recent == 0).sum().sum())
        last_ts = recent.index[-1]
        algorithm.Debug(
            f"[Generate] Skipping factor compute: recent volume nonzero=0 "
            f"(zero={zero_count}/{total_cells} nan={nan_count}/{total_cells}) "
            f"last={last_ts}"
        )
        return False

    def _filter_symbols_by_history(
        self,
        algorithm,
        symbols,
        closes: pd.DataFrame,
        volumes: pd.DataFrame,
        min_days: int,
    ):
        if closes is None or closes.empty or not symbols:
            return symbols
        total_days = int(len(closes.index))
        if total_days <= 0:
            return symbols
        min_required = min(int(min_days), total_days)
        if min_required <= 0:
            return symbols
        window_days = min(total_days, max(self.eval_window, min_required))
        close_recent = closes.tail(window_days)
        vol_recent = volumes.tail(window_days)
        close_valid = close_recent.notna().sum(axis=0) >= min_required
        vol_valid = vol_recent.notna().sum(axis=0) >= min_required
        vol_nonzero = (vol_recent > 0).sum(axis=0) >= min_required
        active_mask = close_valid & vol_valid & vol_nonzero
        dropped = [s for s in symbols if s in active_mask.index and not bool(active_mask.loc[s])]
        if dropped:
            sample = [getattr(s, "Value", str(s)) for s in dropped[:8]]
            algorithm.Debug(
                f"[Universe] Dropping {len(dropped)}/{len(symbols)} symbols "
                f"with insufficient history in last {window_days} days (<{min_required}) "
                f"sample={sample}"
            )
        return [s for s in symbols if s in active_mask.index and bool(active_mask.loc[s])]

    def get_market_regime_eligibility(self, min_days: int) -> dict:
        summary = {
            "required_history_days": max(0, int(min_days or 0)),
            "eligible_symbols": [],
            "eligible_symbol_count": 0,
            "eligible_symbol_sample": [],
            "excluded_young_symbols": [],
            "excluded_young_symbol_count": 0,
            "excluded_young_symbol_sample": [],
        }
        close_df = self.market_cache.get("close")
        volume_df = self.market_cache.get("volume")
        if close_df is None or close_df.empty or volume_df is None or volume_df.empty:
            return summary

        close_df = close_df.sort_index()
        volume_df = volume_df.sort_index().reindex(index=close_df.index, columns=close_df.columns)
        min_required = max(0, int(min_days or 0))
        if min_required <= 0:
            eligible_symbols = list(close_df.columns)
            summary["eligible_symbols"] = eligible_symbols
            summary["eligible_symbol_count"] = len(eligible_symbols)
            summary["eligible_symbol_sample"] = [
                self._symbol_label(symbol) for symbol in eligible_symbols[:10]
            ]
            return summary

        close_valid = close_df.notna().sum(axis=0) >= min_required
        vol_valid = volume_df.notna().sum(axis=0) >= min_required
        vol_nonzero = (volume_df > 0).sum(axis=0) >= min_required
        eligible_mask = close_valid & vol_valid & vol_nonzero

        eligible_symbols = [
            symbol for symbol in close_df.columns
            if symbol in eligible_mask.index and bool(eligible_mask.loc[symbol])
        ]
        excluded_symbols = [
            symbol for symbol in close_df.columns
            if symbol not in eligible_mask.index or not bool(eligible_mask.loc[symbol])
        ]
        summary["eligible_symbols"] = eligible_symbols
        summary["eligible_symbol_count"] = len(eligible_symbols)
        summary["eligible_symbol_sample"] = [
            self._symbol_label(symbol) for symbol in eligible_symbols[:10]
        ]
        summary["excluded_young_symbols"] = excluded_symbols
        summary["excluded_young_symbol_count"] = len(excluded_symbols)
        summary["excluded_young_symbol_sample"] = [
            self._symbol_label(symbol) for symbol in excluded_symbols[:10]
        ]
        return summary

    def _log_alpha_input_stats(
        self,
        algorithm,
        name: str,
        expr: str,
        data_dict: dict,
        factor_df: pd.DataFrame,
        nan_threshold: float = 0.5,
    ) -> None:
        if not isinstance(factor_df, pd.DataFrame) or factor_df.empty:
            return
        last = factor_df.iloc[-1]
        total = int(last.size)
        if total == 0:
            return
        nan_count = int(last.isna().sum())
        if nan_count == 0:
            return
        if (nan_count / total) < nan_threshold:
            return
        algorithm.Debug(
            f"[AlphaDiag] {name} last row NaN {nan_count}/{total} "
            f"({100 * nan_count / total:.1f}%) expr={expr}"
        )
        var_names = sorted(set(re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*", expr)))
        for var in var_names:
            self._log_row_stats(algorithm, var, data_dict.get(var))

    def _log_tscorr_nan_sources(
        self,
        algorithm,
        name: str,
        expr: str,
        data_dict: dict,
        factor_df: pd.DataFrame,
    ) -> None:
        if not isinstance(factor_df, pd.DataFrame) or factor_df.empty:
            return
        last = factor_df.iloc[-1]
        if int(last.isna().sum()) == 0:
            return
        matches = re.findall(r"TsCorr\(([^,]+),([^,]+),\s*([0-9.]+)\)", expr)
        if not matches:
            return
        for lhs_var, rhs_var, window_str in matches:
            if "chip_" not in lhs_var and "chip_" not in rhs_var:
                continue
            lhs_key = lhs_var.strip()
            rhs_key = rhs_var.strip()
            lhs = data_dict.get(lhs_key)
            rhs = data_dict.get(rhs_key)
            if not isinstance(lhs, pd.DataFrame) or not isinstance(rhs, pd.DataFrame):
                algorithm.Debug(
                    f"[AlphaDiag] {name} TsCorr source {lhs_key},{rhs_key}: missing inputs"
                )
                continue
            if lhs.empty or rhs.empty:
                algorithm.Debug(
                    f"[AlphaDiag] {name} TsCorr source {lhs_key},{rhs_key}: empty inputs"
                )
                continue
            try:
                window = int(float(window_str))
            except Exception:
                continue
            min_p = min(10, window)
            rows = min(window, len(lhs.index))
            lhs_win = lhs.tail(rows)
            rhs_win = rhs.tail(rows)
            lhs_last = lhs.iloc[-1]
            rhs_last = rhs.iloc[-1]
            lhs_nan_last = int(lhs_last.isna().sum())
            rhs_nan_last = int(rhs_last.isna().sum())
            lhs_nan_win = int(lhs_win.isna().any(axis=0).sum())
            rhs_nan_win = int(rhs_win.isna().any(axis=0).sum())
            lhs_zero_std = int((lhs_win.std() <= 1e-8).sum())
            rhs_zero_std = int((rhs_win.std() <= 1e-8).sum())
            corr_df = TsCorr(lhs, rhs, window)
            if isinstance(corr_df, pd.DataFrame) and not corr_df.empty:
                corr_last = corr_df.iloc[-1]
                corr_nan_last = int(corr_last.isna().sum())
            else:
                corr_nan_last = len(lhs_last)
            algorithm.Debug(
                f"[AlphaDiag] {name} TsCorr source {lhs_key},{rhs_key} "
                f"rows={len(lhs.index)} win={window} min_p={min_p} "
                f"last_nan: lhs={lhs_nan_last}/{len(lhs_last)} "
                f"rhs={rhs_nan_last}/{len(rhs_last)} corr={corr_nan_last}/{len(lhs_last)} "
                f"win_nan: lhs={lhs_nan_win}/{len(lhs_last)} "
                f"rhs={rhs_nan_win}/{len(rhs_last)} "
                f"zero_std: lhs={lhs_zero_std}/{len(lhs_last)} rhs={rhs_zero_std}/{len(rhs_last)}"
            )
            if isinstance(corr_df, pd.DataFrame) and not corr_df.empty:
                corr_last = corr_df.iloc[-1]
                nan_mask = corr_last.isna()
                if nan_mask.any():
                    valid_pairs = lhs_win.notna() & rhs_win.notna()
                    pair_count = valid_pairs.sum(axis=0)
                    lhs_std = lhs_win.std()
                    rhs_std = rhs_win.std()
                    reasons = {"insufficient_pairs": 0, "zero_std": 0, "other": 0}
                    samples = {"insufficient_pairs": [], "zero_std": [], "other": []}
                    for col in corr_last.index[nan_mask]:
                        count = int(pair_count.get(col, 0))
                        lstd = float(lhs_std.get(col, np.nan))
                        rstd = float(rhs_std.get(col, np.nan))
                        if count < min_p:
                            reason = "insufficient_pairs"
                        elif (np.isfinite(lstd) and lstd <= 1e-8) or (np.isfinite(rstd) and rstd <= 1e-8):
                            reason = "zero_std"
                        else:
                            reason = "other"
                        reasons[reason] += 1
                        if len(samples[reason]) < 6:
                            samples[reason].append(getattr(col, "Value", str(col)))
                    reason_parts = []
                    for reason_key in ("insufficient_pairs", "zero_std", "other"):
                        count = reasons[reason_key]
                        if count == 0:
                            continue
                        sample = samples[reason_key]
                        sample_str = ", ".join(sample)
                        reason_parts.append(f"{reason_key}={count} sample=[{sample_str}]")
                    if reason_parts:
                        algorithm.Debug(
                            f"[AlphaDiag] {name} TsCorr NaN reasons {lhs_key},{rhs_key}: "
                            + " | ".join(reason_parts)
                        )

    def _validate_market_cache(self, algorithm: QCAlgorithm, symbols, current_date) -> bool:
        """
        Validate that market cache is up-to-date.
        
        This method NO LONGER calls history() - all data production is handled by
        minute_consolidator.py. This only checks cache freshness and logs status.
        
        Returns:
            True if cache is valid and up-to-date, False if gap detected.
        """
        self.market_cache.set_logger(algorithm.Debug)
        self.market_cache.ensure_columns(symbols)
        
        if self.market_cache.is_empty():
            algorithm.Debug("[Cache] WARNING: Market cache is empty - requires recovery mode")
            return False
        
        last_ts = self.market_cache.last_timestamp()
        if last_ts is None:
            algorithm.Debug("[Cache] WARNING: No timestamp in cache")
            return False
        
        last_date = last_ts.date() if hasattr(last_ts, 'date') else last_ts
        # Allow same-day or previous trading day
        days_gap = (current_date - last_date).days
        
        if days_gap > 5:  # More than 5 calendar days = likely missing trading days
            algorithm.Debug(f"[Cache] WARNING: Gap detected - last={last_date}, current={current_date}, gap={days_gap} days")
            return False
        
        cache_rows = len(self.market_cache.get("close"))
        algorithm.Debug(f"[Cache] Valid: {cache_rows} rows, last={last_date}")
        return True

    # NOTE: _get_1555_history and _aggregate_minute_to_1555 have been REMOVED.
    # All data production now happens in minute_consolidator.py via feed_history_bars().
    # This alpha_model is now a CONSUMER ONLY - it reads from cache, never produces data.

    def _build_intraday_snapshot(self, algorithm: QCAlgorithm, symbols) -> dict:
        if self.intraday_consolidator is not None:
            if hasattr(self.intraday_consolidator, "get_intraday_snapshot"):
                return self.intraday_consolidator.get_intraday_snapshot(symbols)
        return {"close": {}, "open": {}, "high": {}, "low": {}, "volume": {}, "vwap": {}}

    def _prepare_cmaes_data(
        self,
        factor_values_map: Dict[str, pd.DataFrame],
        returns_window: pd.DataFrame,
        returns_window_long: Optional[pd.DataFrame] = None,
    ) -> tuple:
        ranks = []
        rank_nan = 0
        rank_total = 0
        missing_factors = []
        num_days = int(len(returns_window.index))
        num_symbols = int(len(returns_window.columns))
        min_required = min(int(self.cma_min_valid_days), num_days) if num_days > 0 else 0
        aligned_frames = []
        for factor_def in self.factor_defs:
            name = factor_def["name"]
            fv = factor_values_map.get(name)
            if isinstance(fv, pd.DataFrame) and not fv.empty:
                aligned = fv.reindex(index=returns_window.index, columns=returns_window.columns)
            else:
                missing_factors.append(name)
                aligned = pd.DataFrame(np.nan, index=returns_window.index, columns=returns_window.columns)
            aligned_frames.append((name, aligned))

        total_symbols = num_symbols
        returns_valid = returns_window.notna().sum(axis=0) >= min_required
        base_cols = returns_window.columns[returns_valid]
        symbol_mask = returns_valid.copy()

        used_factors = []
        dropped_factors = {}
        for i, (name, aligned) in enumerate(aligned_frames):
            if aligned.notna().any().any():
                aligned = aligned.reindex(columns=base_cols)
                aligned_frames[i] = (name, aligned)
                if base_cols.size == 0:
                    coverage = 0.0
                    per_symbol_ok = pd.Series(dtype=bool)
                else:
                    per_symbol_ok = aligned.notna().sum(axis=0) >= min_required
                    coverage = float(per_symbol_ok.mean())
                if coverage < self.cma_factor_min_coverage:
                    dropped_factors[name] = coverage
                    continue
                used_factors.append(name)
                if not per_symbol_ok.empty:
                    symbol_mask.loc[per_symbol_ok.index] &= per_symbol_ok
            else:
                missing_factors.append(name)

        filtered_cols = returns_window.columns[symbol_mask]
        filtered_returns = returns_window.loc[:, filtered_cols]
        if returns_window_long is not None and not returns_window_long.empty:
            filtered_returns_long = returns_window_long.reindex(index=returns_window.index, columns=filtered_cols)
        else:
            filtered_returns_long = None
        symbols_list = [s.Value if hasattr(s, "Value") else str(s) for s in filtered_cols]

        for name, aligned in aligned_frames:
            if name not in used_factors:
                continue
            filtered = aligned.reindex(columns=filtered_cols)
            rank_nan += int(filtered.isna().sum().sum())
            rank_total += int(filtered.size)
            rank = filtered.rank(pct=True, axis=1)
            ranks.append(rank.values)

        ret_nan = int(filtered_returns.isna().sum().sum())
        ret_total = int(filtered_returns.size)
        diag = {
            "rank_nan": rank_nan,
            "rank_total": rank_total,
            "ret_nan": ret_nan,
            "ret_total": ret_total,
            "missing_factors": sorted(set(missing_factors)),
            "num_days": num_days,
            "num_symbols": int(len(filtered_cols)),
            "total_symbols": total_symbols,
            "masked_symbols": int(total_symbols - len(filtered_cols)),
            "num_factors": int(len(used_factors)),
            "min_required_days": min_required,
            "configured_min_days": int(self.cma_min_valid_days),
            "used_factors": used_factors,
            "dropped_factors": dropped_factors,
            "factor_min_coverage": float(self.cma_factor_min_coverage),
            "symbols": symbols_list,
            "long_days": int(filtered_returns_long.notna().any(axis=1).sum())
            if filtered_returns_long is not None
            else 0,
        }
        rank_tensor = np.stack(ranks, axis=0) if ranks else np.empty((0, len(filtered_returns.index), len(filtered_cols)))
        return (
            rank_tensor,
            filtered_returns.values,
            diag,
            filtered_returns_long.values if filtered_returns_long is not None else None,
        )

    @staticmethod
    def _symbol_label(symbol) -> str:
        return symbol.Value if hasattr(symbol, "Value") else str(symbol)

    @staticmethod
    def _has_security_price(security) -> bool:
        if security is None:
            return False
        # Lean can seed last price without live bars; orders still fail in this state.
        try:
            if not bool(security.HasData):
                return False
        except Exception:
            return False
        try:
            price = float(security.Price)
            if np.isfinite(price) and price > 0:
                return True
        except Exception:
            pass
        try:
            bid = float(security.Cache.BidPrice)
            ask = float(security.Cache.AskPrice)
            if (np.isfinite(bid) and bid > 0) or (np.isfinite(ask) and ask > 0):
                return True
        except Exception:
            pass
        return False

    def _collect_universe_symbols(self, algorithm: QCAlgorithm) -> List:
        symbols = []
        if self.intraday_consolidator is not None:
            raw = getattr(self.intraday_consolidator, "symbols", None)
            if raw:
                symbols = [s for s in raw if getattr(s, "SecurityType", None) == SecurityType.Equity]
        if not symbols:
            symbols = [
                kvp.Key for kvp in algorithm.Securities
                if kvp.Key.SecurityType == SecurityType.Equity
            ]
        if self.regime_symbols:
            regime_set = set(self.regime_symbols)
            symbols = [s for s in symbols if s not in regime_set]

        deduped = []
        seen = set()
        for symbol in symbols:
            key = self._symbol_label(symbol)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(symbol)
        return deduped

    def _select_symbols_for_run(self, algorithm: QCAlgorithm, is_preview: bool) -> List:
        universe_symbols = self._collect_universe_symbols(algorithm)
        if is_preview:
            return universe_symbols
        if not universe_symbols:
            return []

        live_symbols = []
        missing_security = 0
        for symbol in universe_symbols:
            security = algorithm.Securities[symbol] if symbol in algorithm.Securities else None
            if security is None:
                missing_security += 1
                continue
            if self._has_security_price(security):
                live_symbols.append(symbol)

        if live_symbols:
            if len(live_symbols) != len(universe_symbols):
                algorithm.Debug(
                    f"[Generate] symbol filter live={len(live_symbols)}/{len(universe_symbols)} "
                    f"missing_security={missing_security}"
                )
            return live_symbols

        cache_symbols = []
        try:
            close_df = self.market_cache.get("close")
            if close_df is not None and not close_df.empty:
                cache_cols = set(close_df.columns)
                for symbol in universe_symbols:
                    if symbol in cache_cols or self._symbol_label(symbol) in cache_cols:
                        cache_symbols.append(symbol)
        except Exception:
            cache_symbols = []

        if cache_symbols:
            algorithm.Debug(
                f"[Generate] live prices unavailable; fallback to cache symbols "
                f"{len(cache_symbols)}/{len(universe_symbols)} "
                f"(missing_security={missing_security})"
            )
            return cache_symbols

        algorithm.Debug(
            f"[Generate] symbol selection failed: universe={len(universe_symbols)} "
            f"missing_security={missing_security} no_live_or_cache_symbols"
        )
        return []

    def _required_market_regime_history_days(self, algorithm: QCAlgorithm) -> int:
        timing_model = getattr(algorithm, "actionable_timing_model", None)
        if timing_model is None or not bool(getattr(timing_model, "enable_market_regime_gate", False)):
            return 0

        train_days = max(0, int(getattr(timing_model, "market_regime_train_days", 0) or 0))
        if train_days <= 0:
            return 0

        actionable_warmup = (
            max(self.actionable_factor_expected_warmup.values())
            if self.actionable_factor_expected_warmup
            else 0
        )
        regime_feature_padding = 40
        return int(train_days + 1 + regime_feature_padding + actionable_warmup)

    def _ensure_market_regime_history_capacity(self, algorithm: QCAlgorithm) -> int:
        required_days = self._required_market_regime_history_days(algorithm)
        if required_days <= 0:
            return 0

        self.lookback_days = max(int(self.lookback_days), required_days)
        self.market_cache.max_days = max(int(getattr(self.market_cache, "max_days", 0) or 0), required_days)

        if self.chip_cache is not None and hasattr(self.chip_cache, "max_days"):
            self.chip_cache.max_days = max(
                int(getattr(self.chip_cache, "max_days", 0) or 0),
                required_days,
            )

        factor_cache_days = max(required_days + 100, 500)
        if self.factor_cache is not None and hasattr(self.factor_cache, "max_days"):
            self.factor_cache.max_days = max(
                int(getattr(self.factor_cache, "max_days", 0) or 0),
                factor_cache_days,
            )
        if self.actionable_factor_cache is not None and hasattr(self.actionable_factor_cache, "max_days"):
            self.actionable_factor_cache.max_days = max(
                int(getattr(self.actionable_factor_cache, "max_days", 0) or 0),
                factor_cache_days,
            )

        return required_days

    def _load_cma_state_once(self, algorithm: QCAlgorithm) -> None:
        if self._cma_state_loaded:
            return
        self._cma_state_loaded = True
        cache_manager = getattr(algorithm, "cache_manager", None)
        if cache_manager is None or not hasattr(cache_manager, "load_cma_state"):
            return
        try:
            payload = cache_manager.load_cma_state()
        except Exception as e:
            algorithm.Debug(f"[CMAState] load failed: {e}")
            return
        if not isinstance(payload, dict):
            return
        weights = payload.get("last_cma_weights")
        if isinstance(weights, dict):
            clean_weights = {}
            for name, value in weights.items():
                try:
                    fv = float(value)
                except Exception:
                    continue
                if np.isfinite(fv):
                    clean_weights[str(name)] = fv
            if clean_weights:
                self.last_cma_weights = clean_weights
        snapshot = payload.get("last_score_snapshot")
        if isinstance(snapshot, dict):
            snap_scores = snapshot.get("scores")
            if isinstance(snap_scores, dict):
                clean_scores = {}
                for name, value in snap_scores.items():
                    try:
                        fv = float(value)
                    except Exception:
                        continue
                    if np.isfinite(fv):
                        clean_scores[str(name)] = fv
                if clean_scores:
                    self.last_score_snapshot = {
                        "date": str(snapshot.get("date") or payload.get("date") or ""),
                        "scores": clean_scores,
                    }
        score_mode = payload.get("last_score_mode")
        if score_mode is not None:
            self.last_score_mode = str(score_mode)

    def _persist_cma_state(self, algorithm: QCAlgorithm, current_date) -> None:
        cache_manager = getattr(algorithm, "cache_manager", None)
        if cache_manager is None or not hasattr(cache_manager, "save_cma_state"):
            return
        payload = {
            "date": str(current_date),
            "last_score_mode": self.last_score_mode,
            "last_cma_weights": self.last_cma_weights or {},
            "last_score_snapshot": self.last_score_snapshot or {},
        }
        try:
            cache_manager.save_cma_state(payload)
        except Exception as e:
            algorithm.Debug(f"[CMAState] save failed: {e}")

    def _score_with_last_cma_weights(
        self,
        val_factors: List[dict],
        symbols,
    ) -> tuple:
        if not isinstance(self.last_cma_weights, dict) or not self.last_cma_weights:
            raise ValueError("last_cma_weights unavailable")
        final_scores = pd.Series(0.0, index=symbols, dtype=float)
        used = []
        abs_weight_sum = 0.0
        for factor in val_factors:
            name = factor.get("name")
            if not isinstance(name, str):
                continue
            weight = self.last_cma_weights.get(name)
            if weight is None:
                continue
            try:
                weight = float(weight)
            except Exception:
                continue
            if not np.isfinite(weight):
                continue
            values = factor.get("values")
            if not isinstance(values, pd.Series):
                continue
            ranks = values.rank(pct=True).reindex(symbols)
            final_scores = final_scores.add(weight * ranks, fill_value=0.0)
            used.append((name, weight))
            abs_weight_sum += abs(weight)
        if not used:
            raise ValueError("no overlapping factors with last_cma_weights")
        if abs_weight_sum > 1e-8:
            final_scores = final_scores / abs_weight_sum
        return final_scores, used

    def _series_quality_stats(self, series: pd.Series, sample_limit: int = 5) -> dict:
        if series is None or not isinstance(series, pd.Series) or series.empty:
            return {"total": 0, "nan": 0, "zero": 0, "non_zero": 0, "samples": {}}
        total = int(series.shape[0])
        nan_mask = series.isna()
        zero_mask = (series == 0) & (~nan_mask)
        non_zero_mask = (~nan_mask) & (~zero_mask)
        stats = {
            "total": total,
            "nan": int(nan_mask.sum()),
            "zero": int(zero_mask.sum()),
            "non_zero": int(non_zero_mask.sum()),
            "samples": {
                "nan": [self._symbol_label(sym) for sym in series.index[nan_mask][:sample_limit]],
                "zero": [self._symbol_label(sym) for sym in series.index[zero_mask][:sample_limit]],
                "non_zero": [self._symbol_label(sym) for sym in series.index[non_zero_mask][:sample_limit]],
            },
        }
        return stats

    def _build_daily_cma_report(
        self,
        algorithm: QCAlgorithm,
        current_date,
        final_scores: pd.Series,
        score_mode: str,
        rebalance_today: bool,
        data_dict: dict,
        objective_breakdown: Optional[dict] = None,
        factor_context: Optional[list] = None,
        per_factor_cma_metrics: Optional[list] = None,
        per_factor_cma_metrics_meta: Optional[dict] = None,
        data_as_of_date=None,
    ) -> Optional[dict]:
        if not self.cma_daily_report_enabled:
            return None
        if final_scores is None or not isinstance(final_scores, pd.Series) or final_scores.empty:
            return None

        top_n = int(self.cma_daily_report_top_n)
        bucket_size = int(self.cma_daily_report_bucket_size)

        current_scores = {}
        for symbol, value in pd.to_numeric(final_scores, errors="coerce").items():
            label = self._symbol_label(symbol)
            if pd.notna(value) and np.isfinite(value):
                current_scores[label] = float(value)

        prev_snapshot = self.last_score_snapshot if isinstance(self.last_score_snapshot, dict) else {}
        prev_scores = prev_snapshot.get("scores", {}) if isinstance(prev_snapshot, dict) else {}
        if not isinstance(prev_scores, dict):
            prev_scores = {}
        clean_prev_scores = {}
        for k, v in prev_scores.items():
            try:
                fv = float(v)
            except Exception:
                continue
            if np.isfinite(fv):
                clean_prev_scores[str(k)] = fv
        prev_scores = clean_prev_scores

        delta_rows = []
        overlap = set(current_scores.keys()) & set(prev_scores.keys())
        for symbol in overlap:
            today_score = current_scores[symbol]
            prev_score = prev_scores[symbol]
            delta = today_score - prev_score
            delta_rows.append(
                {
                    "symbol": symbol,
                    "today_score": float(today_score),
                    "prev_score": float(prev_score),
                    "delta": float(delta),
                    "abs_delta": float(abs(delta)),
                }
            )
        delta_rows.sort(key=lambda x: x["abs_delta"], reverse=True)
        delta_rows = delta_rows[:top_n]
        for row in delta_rows:
            row.pop("abs_delta", None)

        today_series = pd.Series(current_scores, dtype=float).sort_values(ascending=False)
        prev_series = pd.Series(prev_scores, dtype=float).sort_values(ascending=False)
        top_today = set(today_series.head(bucket_size).index)
        bottom_today = set(today_series.tail(bucket_size).index)
        top_prev = set(prev_series.head(bucket_size).index)
        bottom_prev = set(prev_series.tail(bucket_size).index)

        top_to_bottom = sorted(list(top_prev & bottom_today))
        bottom_to_top = sorted(list(bottom_prev & top_today))

        score_stats = self._series_quality_stats(pd.to_numeric(final_scores, errors="coerce"))
        input_stats = {}
        input_keys = [
            "$drift_factor",
            "$amihud_mean",
            "$amihud_range",
            "$vol_fft",
            "$ohl",
            "$chl",
            "$ohlc",
            "$chlo",
        ]
        for key in input_keys:
            df = data_dict.get(key)
            if isinstance(df, pd.DataFrame) and not df.empty:
                input_stats[key[1:]] = self._series_quality_stats(pd.to_numeric(df.iloc[-1], errors="coerce"))

        resolved_data_as_of_date = self._report_date_text(data_as_of_date)
        if not resolved_data_as_of_date:
            factor_values_map = getattr(getattr(self, "factor_cache", None), "factor_values", None)
            resolved_data_as_of_date = self._resolve_factor_panel_data_as_of_date(
                factor_values_map,
                fallback=current_date,
            )

        report = {
            "as_of_date": str(current_date),
            "data_as_of_date": resolved_data_as_of_date,
            "baseline_date": prev_snapshot.get("date") if isinstance(prev_snapshot, dict) else None,
            "score_mode": score_mode,
            "rebalance_today": bool(rebalance_today),
            "top_n": top_n,
            "bucket_size": bucket_size,
            "scores": current_scores,
            "scored_universe_size": len(current_scores),
            "score_changes_top_n": delta_rows,
            "bucket_transitions": {
                "top_to_bottom": top_to_bottom,
                "bottom_to_top": bottom_to_top,
            },
            "objective_breakdown": objective_breakdown if isinstance(objective_breakdown, dict) else None,
            "objective_config": {
                "cma_obj_logret_weight": float(self.cma_obj_logret_weight),
                "cma_obj_ic_weight": float(self.cma_obj_ic_weight),
                "cma_obj_fitness_weight": float(self.cma_obj_fitness_weight),
                "cma_turnover_penalty": float(self.cma_turnover_penalty),
                "cma_icir_penalty": float(self.cma_icir_penalty),
                "cma_icir_floor": float(self.cma_icir_floor),
                "annualization": 252,
                "turnover_floor": 0.125,
                "cma_top_bottom": int(self.cma_top_bottom),
                "cma_forward_horizon": int(self.cma_forward_horizon),
                "cma_min_valid_days": int(self.cma_min_valid_days),
                "eval_window": int(self.eval_window),
            },
            "factor_context": factor_context if isinstance(factor_context, list) else [],
            "per_factor_cma_metrics": (
                per_factor_cma_metrics if isinstance(per_factor_cma_metrics, list) else []
            ),
            "per_factor_cma_metrics_meta": (
                per_factor_cma_metrics_meta
                if isinstance(per_factor_cma_metrics_meta, dict)
                else {
                    "window_days_used": int(self.eval_window),
                    "score_mode": str(score_mode),
                    "available": False,
                    "unavailable_reason": "not_computed",
                }
            ),
            "stats": {
                "score": score_stats,
                "inputs": input_stats,
            },
        }

        algorithm.Debug(
            f"[CMAReport] date={current_date} data_as_of={resolved_data_as_of_date} score_mode={score_mode} "
            f"changes={len(delta_rows)} top_to_bottom={len(top_to_bottom)} bottom_to_top={len(bottom_to_top)}"
        )
        if delta_rows:
            preview = ", ".join(
                f"{row['symbol']}:{row['delta']:+.4f}" for row in delta_rows[: min(5, len(delta_rows))]
            )
            algorithm.Debug(f"[CMAReport] score delta sample: {preview}")
        return report

    def _persist_daily_cma_report(self, algorithm: QCAlgorithm, report: Optional[dict]) -> None:
        if not report:
            return
        sink = (self.cma_daily_report_sink or "").lower()
        if "log" in sink:
            algorithm.Debug(
                f"[CMAReport] persist sink=log date={report.get('as_of_date')} "
                f"mode={report.get('score_mode')}"
            )

        if "objectstore" not in sink:
            return

        cache_manager = getattr(algorithm, "cache_manager", None)
        if cache_manager is not None and hasattr(cache_manager, "save_cma_daily_report"):
            try:
                cache_manager.save_cma_daily_report(report.get("as_of_date"), report)
                return
            except Exception as e:
                algorithm.Debug(f"[CMAReport] ObjectStore persist failed via cache manager: {e}")

        try:
            if cache_manager is not None and hasattr(cache_manager, "_deploy_key"):
                key = cache_manager._deploy_key(
                    f"reports/cma_daily/{report.get('as_of_date')}.json"
                )
            else:
                key = f"alphasage/reports/cma_daily/{report.get('as_of_date')}.json"
            algorithm.ObjectStore.SaveBytes(
                key,
                json.dumps(report, default=str).encode("utf-8"),
            )
        except Exception as e:
            algorithm.Debug(f"[CMAReport] fallback ObjectStore persist failed: {e}")

    def generate_insights_on_schedule(self, algorithm: QCAlgorithm) -> List[Insight]:
        """Called by scheduled event. Main entry point for insight generation."""
        return self._generate_insights(algorithm)

    def update(self, algorithm: QCAlgorithm, data: Slice) -> List[Insight]:
        """Called by framework on data events. We only generate via scheduled events."""
        return []

    def _generate_insights(self, algorithm: QCAlgorithm) -> List[Insight]:
        """
        Generate insights based on factor analysis.
        If defer_first_rebalance is True, runs the full pipeline but skips emitting insights.
        """
        insights = []
        current_date = algorithm.Time.date()
        
        # Check if this is a preview run (deployment)
        is_preview = self.defer_first_rebalance
        skip_emit = False
        rebalance_today = True
        duplicate_run = False
        if is_preview:
            algorithm.Debug("[PREVIEW] Running full pipeline but will NOT emit insights")
            self.defer_first_rebalance = False  # Reset flag after preview
            skip_emit = True
        else:
            # Prevent duplicate signals on the same date
            if self.last_signal_date == current_date:
                skip_emit = True
                duplicate_run = True
                algorithm.Debug(f"[Schedule] Duplicate run on {current_date} - skipping insight emission")
            else:
                self.last_signal_date = current_date

            if self.next_rebalance_date is None:
                self.next_rebalance_date = current_date
                algorithm.Debug(
                    f"*** REBALANCE SCHEDULE INITIALIZED: First rebalance TODAY {current_date}, "
                    f"will rebalance every {self.rebalance_trading_days} trading days ***"
                )

            rebalance_today = current_date >= self.next_rebalance_date

            if rebalance_today:
                try:
                    trading_days = []
                    check_date = current_date
                    for _ in range(self.rebalance_trading_days * 3):
                        check_date += timedelta(days=1)
                        if algorithm.Securities[algorithm.Securities.Keys[0]].Exchange.DateIsOpen(check_date):
                            trading_days.append(check_date)
                            if len(trading_days) >= self.rebalance_trading_days:
                                break
                    self.next_rebalance_date = trading_days[-1] if trading_days else current_date + timedelta(days=self.rebalance_trading_days)
                except:
                    self.next_rebalance_date = current_date + timedelta(days=self.rebalance_trading_days)

                algorithm.Debug(
                    f"*** REBALANCING at {algorithm.Time} (date: {current_date}). "
                    f"Next scheduled: {self.next_rebalance_date} ***"
                )
            else:
                skip_emit = True
                algorithm.Debug(f"[Schedule] {current_date} not rebalance day - updating caches only")

        symbols = self._select_symbols_for_run(algorithm, is_preview=is_preview)
        if not symbols:
            algorithm.Debug("[Generate] No symbols available")
            return insights
        
        algorithm.Debug(f"[Generate] Processing {len(symbols)} symbols")
        self._load_cma_state_once(algorithm)

        # Validate cache (no longer calls history() - consumer only)
        cache_valid = self._validate_market_cache(algorithm, symbols, current_date)
        if not cache_valid:
            algorithm.Debug("[Generate] Cache invalid - recovery mode should handle this")
            # Continue anyway to allow recovery mode to work

        regime_history_days = self._ensure_market_regime_history_capacity(algorithm)

        # Determine if we should create today's snapshot
        # Use flags set by main.py, with time detection as fallback
        from datetime import time as dt_time
        current_time = algorithm.Time.time()
        market_open = dt_time(9, 30)
        market_finalize = dt_time(15, 55)
        
        # Check is_pre_market flag (set by main.py on deployment) OR actual time
        is_pre_market_now = self.is_pre_market or current_time < market_open
        is_post_market_now = self.is_post_market or current_time > dt_time(16, 0)
        is_finalize_time = not is_pre_market_now and current_time >= market_finalize

        if is_finalize_time and not is_preview and self.intraday_consolidator is not None:
            try:
                live_bar_count = int(sum(self.intraday_consolidator._bar_counts.values()))
            except Exception:
                live_bar_count = -1
            if live_bar_count == 0:
                algorithm.Debug(
                    "[Generate] No intraday bars collected today (bar_count=0). "
                    "Skipping signal to avoid stale rebalance."
                )
                return insights
        
        snapshot_time = None
        intraday_df = None
        
        if is_pre_market_now or is_post_market_now:
            # Pre/Post-market: DO NOT create today's snapshot (would be future/stale data)
            algorithm.Debug(f"[Generate] PRE/POST-MARKET (flag={self.is_pre_market}/{self.is_post_market}, time={current_time}) - using cache only")
            snapshot = {"close": {}, "open": {}, "high": {}, "low": {}, "volume": {}, "vwap": {}}
        elif is_finalize_time:
            # At or after 15:55: normal finalize
            if self.intraday_consolidator is not None:
                timestamp, snapshot, intraday_df = self.intraday_consolidator.get_daily_row(symbols, for_finalize=True)
                if snapshot.get("close"):
                    self.market_cache.append_snapshot(timestamp, snapshot)
                    algorithm.Debug(f"[Snapshot] Appended {len(snapshot.get('close', {}))} symbols at {timestamp}")
                    snapshot_time = timestamp
            else:
                algorithm.Debug("[Generate] WARNING: No consolidator - using fallback snapshot")
                snapshot = {"close": {}, "open": {}, "high": {}, "low": {}, "volume": {}, "vwap": {}}
        else:
            # During market hours but before 15:55: partial day data (preview only)
            if self.intraday_consolidator is not None:
                bar_count = sum(self.intraday_consolidator._bar_counts.values())
                if bar_count > 0:
                    timestamp, snapshot, intraday_df = self.intraday_consolidator.get_daily_row(symbols, for_finalize=False)
                    algorithm.Debug(f"[Generate] INTRADAY ({bar_count} bars) - partial snapshot at {timestamp}")
                    # For intraday, we can append to cache for preview but it will be overwritten at 15:55
                    if snapshot.get("close"):
                        self.market_cache.append_snapshot(timestamp, snapshot)
                        snapshot_time = timestamp
                else:
                    algorithm.Debug(f"[Generate] INTRADAY but no bars yet")
                    snapshot = {"close": {}, "open": {}, "high": {}, "low": {}, "volume": {}, "vwap": {}}
            else:
                snapshot = {"close": {}, "open": {}, "high": {}, "low": {}, "volume": {}, "vwap": {}}

        # Get OHLCV from cache
        closes = self.market_cache.get("close").reindex(columns=symbols)
        if closes.empty:
            algorithm.Debug("[Generate] closes is empty after cache update")
            return insights
        
        opens = self.market_cache.get("open").reindex(columns=symbols)
        highs = self.market_cache.get("high").reindex(columns=symbols)
        lows = self.market_cache.get("low").reindex(columns=symbols)
        volumes = self.market_cache.get("volume").reindex(columns=symbols)
        
        self._log_ohlcv_history_quality(
            algorithm,
            {
                "closes": closes,
                "opens": opens,
                "highs": highs,
                "lows": lows,
                "volumes": volumes,
            },
        )

        vwap = (closes + highs + lows) / 3
        v_cache = self.market_cache.get("vwap").reindex(index=vwap.index, columns=vwap.columns)
        vwap.update(v_cache)
        vwap = vwap.where(volumes > 0)
        self._log_ohlcv_last_row_stats(
            algorithm,
            {
                "close": closes,
                "open": opens,
                "high": highs,
                "low": lows,
                "volume": volumes,
                "vwap": vwap,
            },
        )
        self._log_ohlcv_snapshot_quality(
            algorithm,
            {
                "close": closes,
                "open": opens,
                "high": highs,
                "low": lows,
                "volume": volumes,
                "vwap": vwap,
            },
        )
        if not self._has_recent_nonzero_volume(algorithm, volumes, lookback_days=5):
            return insights

        symbols = self._filter_symbols_by_history(
            algorithm,
            symbols,
            closes,
            volumes,
            self.cma_min_valid_days,
        )
        if not symbols:
            algorithm.Debug("[Generate] No symbols with sufficient history after filter")
            return insights
        closes = closes.reindex(columns=symbols)
        opens = opens.reindex(columns=symbols)
        highs = highs.reindex(columns=symbols)
        lows = lows.reindex(columns=symbols)
        volumes = volumes.reindex(columns=symbols)
        vwap = vwap.reindex(columns=symbols)

        max_warmup = max(self.factor_expected_warmup.values()) if self.factor_expected_warmup else 0
        max_horizon = max(self.prediction_horizon, self.cma_forward_horizon)
        calc_window = self.eval_window + max_horizon + max_warmup + 1
        calc_window = max(calc_window, int(regime_history_days or 0))
        if len(closes.index) > calc_window:
            closes = closes.tail(calc_window)
            opens = opens.tail(calc_window)
            highs = highs.tail(calc_window)
            lows = lows.tail(calc_window)
            volumes = volumes.tail(calc_window)
            vwap = vwap.tail(calc_window)

        try:
            # Build data dictionary
            data_dict = {
                '$close': closes, '$open': opens, '$high': highs, '$low': lows, '$volume': volumes, '$vwap': vwap,
                '$log_close': np.log(closes.clip(lower=1e-8)),
                '$log_volume': np.log(volumes.clip(lower=1)),
                '$log_money': np.log((volumes * vwap).clip(lower=1)),
                '$high_break_revert_45_5': calculate_high_break_revert_45_5(highs),
                '$sortino_ratio': calculate_sortino_ratio(closes.pct_change()),
                '$ts_mom_rank': calculate_ts_mom_rank(closes),
                '$max_dd_ratio': calculate_max_dd_ratio(closes),
                '$rel_strength_ma': calculate_rel_strength_ma(closes),
            }
            
            # DEBUG: Check log_volume for alpha_3 diagnosis
            lv = data_dict['$log_volume']
            if not lv.empty:
                last_lv = lv.iloc[-1]
                zero_log_vols = (last_lv == 0).sum()
                nan_log_vols = last_lv.isna().sum()
                std_last_20 = lv.iloc[-20:].std()
                zero_std_count = (std_last_20 == 0).sum()
                algorithm.Debug(f"[Alpha3 Debug] LogVolume LastRow: Zeros={zero_log_vols}/{len(last_lv)} NaNs={nan_log_vols} | ZeroStd(20d)={zero_std_count}")


            # Add intraday features with date-based alignment
            if self.intraday_consolidator:
                feats = self.intraday_consolidator.daily_features or {}
                hist_panel = getattr(self.intraday_consolidator, "feature_history", None)
                hist_panel = hist_panel.get_all_features_panel() if hist_panel else {}
                
                # Normalize indices to dates for alignment
                closes_dates = pd.to_datetime(closes.index).normalize()
                
                for feat in ['drift_factor', 'amihud_mean', 'amihud_range', 'vol_fft', 'ohl', 'chl', 'ohlc', 'chlo']:
                    h_df = hist_panel.get(feat)
                    if h_df is not None and not h_df.empty:
                        # Normalize feature dates and reindex to match closes
                        h_df_dates = pd.to_datetime(h_df.index).normalize()
                        h_df_aligned = h_df.copy()
                        h_df_aligned.index = h_df_dates
                        if h_df_aligned.index.has_duplicates:
                            h_df_aligned = h_df_aligned[~h_df_aligned.index.duplicated(keep="first")]
                        aligned = h_df_aligned.reindex(closes_dates)
                        aligned.index = closes.index
                        aligned = aligned.reindex(columns=closes.columns)
                        
                        # Forward-fill NaN for all price gap features (illiquid symbols)
                        # These features can have NaN when a symbol has no liquid bars that day
                        if feat in ['ohl', 'ohlc', 'chl', 'chlo']:
                            aligned = aligned.ffill()

                        aligned = self._fill_all_nan_rows(algorithm, feat, aligned)
                        self._log_intraday_feature_health(algorithm, feat, aligned)
                        data_dict[f'${feat}'] = aligned
                    else:
                        data_dict[f'${feat}'] = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
                
                # Overlay today's live features if available
                if feats and snapshot_time is not None:
                    for feat in ['drift_factor', 'amihud_mean', 'amihud_range', 'vol_fft', 'ohl', 'chl', 'ohlc', 'chlo']:
                        if snapshot_time in data_dict[f'${feat}'].index:
                            live_series = pd.Series({s: f.get(feat, np.nan) for s, f in feats.items()})
                            data_dict[f'${feat}'].loc[snapshot_time] = live_series.reindex(closes.columns)
            else:
                for feat in ['drift_factor', 'amihud_mean', 'amihud_range', 'vol_fft', 'ohl', 'chl', 'ohlc', 'chlo']:
                    data_dict[f'${feat}'] = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)

            # Daily chip features from OHLCV history (incremental)
            compute_chip = True
            update_dates = None
            reset_chip_state = False
            if not algorithm.LiveMode and self.backtest_chip_rebalance_only and not rebalance_today:
                compute_chip = False
            if compute_chip:
                if not algorithm.LiveMode and self.backtest_chip_rebalance_only:
                    backfill = min(self.chip_backfill_days, len(closes.index))
                    if backfill > 0:
                        update_dates = list(closes.index[-backfill:])
                        reset_chip_state = True
                self.chip_cache.update_panel(
                    lows,
                    highs,
                    closes,
                    volumes,
                    vwap=vwap,
                    symbols=list(closes.columns),
                    dates=update_dates,
                    reset_state=reset_chip_state,
                )

            chip_panel = self.chip_cache.get_panel(
                closes.index,
                closes.columns,
                ffill=(not algorithm.LiveMode and self.backtest_chip_rebalance_only),
            )
            for feat_name, feat_df in chip_panel.items():
                data_dict[f'${feat_name}'] = feat_df

            # Log intraday feature diagnostics
            self._log_intraday_features(algorithm, data_dict)

            # Compute factors
            val_factors = []
            f_vals_map = {}
            ret_win = closes.pct_change().shift(-self.prediction_horizon).iloc[-(self.eval_window + self.prediction_horizon):-self.prediction_horizon]
            ret_long_full = closes.pct_change(self.cma_forward_horizon).shift(-self.cma_forward_horizon)
            ret_long_win = ret_long_full.reindex(index=ret_win.index)
            self._log_returns_nan_stats(algorithm, "ret_win", ret_win)
            self._log_returns_nan_stats(algorithm, "ret_long_win", ret_long_win)
            available_dates = closes.index

            def _index_pos(index, value):
                pos = index.get_loc(value)
                if isinstance(pos, slice):
                    return pos.start
                if isinstance(pos, np.ndarray):
                    return int(pos[0])
                return int(pos)

            def _slice_data_dict(idx):
                sliced = {}
                for key, val in data_dict.items():
                    if isinstance(val, pd.DataFrame):
                        sliced[key] = val.reindex(index=idx)
                    elif isinstance(val, pd.Series):
                        sliced[key] = val.reindex(index=idx)
                    else:
                        sliced[key] = val
                return sliced

            for f_def in self.factor_defs:
                name, expr = f_def['name'], f_def['expr']
                try:
                    cached = self.factor_cache.get_factor_values(name)
                    if cached is None or cached.empty:
                        new_dates = list(available_dates)
                    else:
                        new_dates = [d for d in available_dates if d > cached.index[-1]]
                        missing_dates = [d for d in available_dates if d not in cached.index]
                        if missing_dates:
                            new_dates = sorted(set(new_dates + missing_dates))
                            new_dates = [d for d in available_dates if d in new_dates]

                    if new_dates:
                        warmup = self.factor_expected_warmup.get(name, 0)
                        first_new = new_dates[0]
                        last_new = new_dates[-1]
                        first_pos = _index_pos(available_dates, first_new)
                        last_pos = _index_pos(available_dates, last_new)
                        # Rolling/shift operators need one extra prior row in single-day incremental mode.
                        # Without this, newest-row values can become all-NaN for nested expressions.
                        start_pos = max(0, first_pos - warmup - 1)
                        subset_index = available_dates[start_pos:last_pos + 1]
                        engine = FactorEngine(_slice_data_dict(subset_index))
                        fv_slice = engine.parse_expression(expr)
                        if isinstance(fv_slice, pd.DataFrame) and not fv_slice.empty:
                            fv_new = fv_slice.reindex(index=new_dates)
                        else:
                            fv_new = pd.DataFrame(np.nan, index=new_dates, columns=closes.columns)
                        self.factor_cache.update_factor_values(name, fv_new)

                    fv_cached = self.factor_cache.get_factor_values(name)
                    if isinstance(fv_cached, pd.DataFrame) and not fv_cached.empty:
                        fv = fv_cached.reindex(index=available_dates, columns=closes.columns)
                    else:
                        fv = pd.DataFrame(np.nan, index=available_dates, columns=closes.columns)
                    fv = self._clean_factor_values(fv)
                    f_vals_map[name] = fv
                    self._log_alpha_input_stats(algorithm, name, expr, data_dict, fv)
                    self._log_tscorr_nan_sources(algorithm, name, expr, data_dict, fv)
                    
                    af = fv.iloc[-(self.eval_window + self.prediction_horizon):-self.prediction_horizon]
                    if af.shape[0] < self.eval_window:
                        continue
                    
                    # Compute or retrieve IC
                    ic_series = self.factor_cache.get_daily_ic(name)
                    if ic_series is None or not all(d in ic_series.index for d in af.index):
                        ic_series = af.corrwith(ret_win, axis=1)
                        self.factor_cache.update_daily_ic(name, ic_series)
                    
                    # Compute or retrieve L/S returns
                    ls_series = self.factor_cache.get_daily_ls(name)
                    if ls_series is None or not all(d in ls_series.index for d in af.index):
                        top, bot = af.quantile(0.8, axis=1), af.quantile(0.2, axis=1)
                        ls_series = ret_win.where(af.ge(top, axis=0)).mean(axis=1) - ret_win.where(af.le(bot, axis=0)).mean(axis=1)
                        self.factor_cache.update_daily_ls(name, ls_series)

                    weighted_metrics = self._compute_weighted_factor_portfolio_metrics(af, ret_win)
                    
                    val_factors.append({
                        'name': name, 'values': fv.iloc[-1],
                        'expr': expr,
                        'effective_direction': 1,
                        'icir': ic_series.mean() / (ic_series.std() + 1e-8),
                        'return': ls_series.sum(),
                        'sortino': ls_series.mean() / (ls_series[ls_series < 0].std() + 1e-8),
                        'fitness': float(weighted_metrics.get('fitness', 0.0)),
                        'sharpe': float(weighted_metrics.get('sharpe', 0.0)),
                        'turnover': float(weighted_metrics.get('turnover', 0.0)),
                    })
                except Exception as e:
                    algorithm.Debug(f"Factor {name} error: {e}")

            # Detailed NaN Statistics
            self._log_full_nan_stats(algorithm, f_vals_map)

            # Compute additional actionable factors as shadow diagnostics only.
            # These are intentionally excluded from scoring/weighting.
            self._compute_actionable_shadow_factors(
                algorithm,
                data_dict,
                available_dates,
                closes.columns,
            )

            self.factor_cache.trim_all()
            
            # Log factor NaN summary
            xs_parts = []
            for f_def in self.factor_defs:
                name = f_def["name"]
                fv = f_vals_map.get(name)
                if not isinstance(fv, pd.DataFrame) or fv.empty:
                    xs_parts.append(f"{name}=empty")
                else:
                    latest_row = fv.iloc[-1]
                    total = int(latest_row.size)
                    nan_count = int(latest_row.isna().sum())
                    zero_count = int((latest_row == 0).sum())
                    xs_parts.append(f"{name}=nan {nan_count}/{total} zero {zero_count}/{total}")
            algorithm.Debug(f"[FactorData] latest XS stats: {', '.join(xs_parts)}")
            
            if not val_factors:
                algorithm.Debug("[Generate] No valid factors computed")
                return insights

            score_factor_names = [str(f.get("name")) for f in val_factors if f.get("name")]

            cma_rank_t = None
            cma_ret_t = None
            cma_ret_long_t = None
            cma_diag = None
            cma_val_factors = val_factors
            cma_factor_names = None
            cma_symbols = None
            objective_breakdown = None
            per_factor_cma_metrics = []
            per_factor_cma_metrics_meta = {
                "window_days_used": int(self.eval_window),
                "score_mode": "unknown",
                "available": False,
                "unavailable_reason": "not_cma_mode",
            }
            cma_mode_full = self.weighting_method == "cmaes" and (rebalance_today or is_preview)
            cma_prepare_for_report = self.weighting_method == "cmaes" and self.cma_daily_report_enabled
            if cma_mode_full or cma_prepare_for_report:
                try:
                    cma_rank_t, cma_ret_t, cma_diag, cma_ret_long_t = self._prepare_cmaes_data(
                        f_vals_map,
                        ret_win,
                        ret_long_win,
                    )
                    self._log_cmaes_input_quality(algorithm, cma_diag)
                    if cma_diag and cma_diag.get("used_factors"):
                        name_map = {f["name"]: f for f in val_factors}
                        used = cma_diag.get("used_factors", [])
                        keep_idx = [i for i, n in enumerate(used) if n in name_map]
                        cma_val_factors = [name_map[n] for n in used if n in name_map]
                        if cma_rank_t is not None and keep_idx:
                            cma_rank_t = cma_rank_t[keep_idx, :, :]
                        cma_factor_names = [used[i] for i in keep_idx] if keep_idx else used
                    cma_symbols = cma_diag.get("symbols") if cma_diag else None
                except Exception as e:
                    algorithm.Debug(f"[CMA-ES] input prep error: {e}")

            if duplicate_run and not is_preview:
                return insights

            # Compute final scores
            final_scores = pd.Series(0.0, index=closes.columns, dtype=float)
            score_mode = "full"
            effective_method = self.weighting_method
            if effective_method == "cmaes":
                if cma_mode_full:
                    try:
                        if not cma_val_factors:
                            raise ValueError("CMA-ES requires at least one usable factor")
                        if cma_rank_t is None or cma_ret_t is None:
                            cma_rank_t, cma_ret_t, cma_diag, cma_ret_long_t = self._prepare_cmaes_data(
                                f_vals_map,
                                ret_win,
                                ret_long_win,
                            )
                            self._log_cmaes_input_quality(algorithm, cma_diag)
                            if cma_diag and cma_diag.get("used_factors"):
                                name_map = {f["name"]: f for f in val_factors}
                                used = cma_diag.get("used_factors", [])
                                keep_idx = [i for i, n in enumerate(used) if n in name_map]
                                cma_val_factors = [name_map[n] for n in used if n in name_map]
                                if cma_rank_t is not None and keep_idx:
                                    cma_rank_t = cma_rank_t[keep_idx, :, :]
                                cma_factor_names = [used[i] for i in keep_idx] if keep_idx else used
                            cma_symbols = cma_diag.get("symbols") if cma_diag else None
                        rank_t = cma_rank_t
                        ret_t = cma_ret_t

                        cache_manager = getattr(algorithm, "cache_manager", None)
                        if cache_manager and hasattr(cache_manager, "save_cmaes_inputs"):
                            factor_names = cma_factor_names or (cma_diag.get("used_factors") if cma_diag else [])
                            symbols = cma_symbols or [s.Value if hasattr(s, "Value") else str(s) for s in ret_win.columns]
                            meta = {
                                "eval_window": int(self.eval_window),
                                "prediction_horizon": int(self.prediction_horizon),
                                "top_k": int(min(self.cma_top_bottom, len(symbols))),
                            }
                            factor_fitness = {}
                            for factor in cma_val_factors:
                                name = factor.get("name")
                                if not name:
                                    continue
                                fit = float(factor.get("fitness", 0.0))
                                factor_fitness[str(name)] = fit if np.isfinite(fit) else 0.0
                            if factor_fitness:
                                meta["factor_fitness"] = factor_fitness
                            if cma_diag:
                                if "rank_nan" in cma_diag:
                                    meta["rank_nan"] = int(cma_diag["rank_nan"])
                                if "ret_nan" in cma_diag:
                                    meta["return_nan"] = int(cma_diag["ret_nan"])
                            cache_manager.save_cmaes_inputs(
                                current_date,
                                factor_names,
                                symbols,
                                list(ret_win.index),
                                cma_rank_t,
                                cma_ret_t,
                                metadata=meta,
                            )

                        prev_w = getattr(self, "cmaes_weights", None)

                        def obj(w):
                            wn = self._normalize_weights(w)
                            daily_rets, daily_ic = self._compute_cma_portfolio_series(
                                rank_t,
                                ret_t,
                                wn,
                                self.cma_top_bottom,
                            )
                            if daily_rets.size == 0:
                                return 0.0
                            turnover = 0.0
                            if isinstance(prev_w, np.ndarray) and prev_w.shape == wn.shape:
                                turnover = float(np.sum(np.abs(wn - prev_w)))
                            score = self._compute_cma_objective_score(
                                daily_rets=daily_rets,
                                daily_ic=daily_ic,
                                turnover=turnover,
                            )
                            return -score

                        rw = cmaes_optimize(
                            obj,
                            len(cma_val_factors),
                            seed=current_date.toordinal(),
                            max_iters=40,
                            sigma=0.5,
                        )
                        cw = self._normalize_weights(rw)
                        self.cmaes_weights = cw
                        self.last_cma_weights = {
                            f["name"]: float(cw[i]) for i, f in enumerate(cma_val_factors)
                        }

                        daily_rets, daily_ic = self._compute_cma_portfolio_series(
                            rank_t,
                            ret_t,
                            cw,
                            self.cma_top_bottom,
                        )
                        turnover = 0.0
                        if isinstance(prev_w, np.ndarray) and prev_w.shape == cw.shape:
                            turnover = float(np.sum(np.abs(cw - prev_w)))
                        objective_breakdown = self._compute_cma_objective_components(
                            daily_rets=daily_rets,
                            daily_ic=daily_ic,
                            turnover=turnover,
                        )
                        factor_names = [str(f.get("name")) for f in cma_val_factors if f.get("name")]
                        weights_map = {str(f["name"]): float(cw[i]) for i, f in enumerate(cma_val_factors) if f.get("name")}
                        per_factor_cma_metrics, per_factor_cma_metrics_meta = self._compute_per_factor_cma_metrics(
                            rank_t=rank_t,
                            ret_t=ret_t,
                            factor_names=factor_names,
                            weights_map=weights_map,
                            score_mode="full",
                        )

                        weight_pairs = ", ".join(
                            f"{f['name']}={cw[i]:.4f}" for i, f in enumerate(cma_val_factors)
                        )
                        algorithm.Debug(f"[CMA-ES] weights: {weight_pairs}")
                        score_factor_names = [str(f.get("name")) for f in cma_val_factors if f.get("name")]

                        for i, f in enumerate(cma_val_factors):
                            final_scores += cw[i] * f["values"].rank(pct=True)
                        score_mode = "full"
                    except Exception as e:
                        algorithm.Debug(f"[CMA-ES] Error: {e}, falling back to zscore")
                        per_factor_cma_metrics = []
                        per_factor_cma_metrics_meta = {
                            "window_days_used": int(self.eval_window),
                            "score_mode": "full",
                            "available": False,
                            "unavailable_reason": "cma_full_failed",
                        }
                        effective_method = "zscore"
                else:
                    score_mode = "light"
                    per_factor_cma_metrics_meta = {
                        "window_days_used": int(self.eval_window),
                        "score_mode": "light",
                        "available": False,
                        "unavailable_reason": "cma_light_inputs_unavailable",
                    }
                    try:
                        if not self.cma_light_report_nonrebalance:
                            raise ValueError("non-rebalance light scoring disabled")
                        if self.cma_light_score_source != "last_cma_weights":
                            raise ValueError(f"unsupported light score source: {self.cma_light_score_source}")
                        final_scores, used_weights = self._score_with_last_cma_weights(
                            val_factors,
                            closes.columns,
                        )
                        score_factor_names = [str(name) for name, _ in used_weights if name]
                        used_preview = ", ".join(
                            f"{name}={weight:.4f}" for name, weight in used_weights[: min(8, len(used_weights))]
                        )
                        algorithm.Debug(f"[CMA-Light] using last_cma_weights: {used_preview}")
                        if cma_rank_t is not None and cma_ret_t is not None and used_weights:
                            weights_map = {str(n): float(w) for n, w in used_weights}
                            factor_names = cma_factor_names or [f.get("name") for f in cma_val_factors]
                            wn = np.asarray([weights_map.get(str(name), 0.0) for name in factor_names], dtype=float)
                            abs_sum = float(np.sum(np.abs(wn)))
                            if abs_sum > 1e-8:
                                wn = wn / abs_sum
                                daily_rets, daily_ic = self._compute_cma_portfolio_series(
                                    cma_rank_t,
                                    cma_ret_t,
                                    wn,
                                    self.cma_top_bottom,
                                )
                                turnover = 0.0
                                prev_w = getattr(self, "cmaes_weights", None)
                                if isinstance(prev_w, np.ndarray) and prev_w.shape == wn.shape:
                                    turnover = float(np.sum(np.abs(wn - prev_w)))
                                objective_breakdown = self._compute_cma_objective_components(
                                    daily_rets=daily_rets,
                                    daily_ic=daily_ic,
                                    turnover=turnover,
                                )
                                per_factor_cma_metrics, per_factor_cma_metrics_meta = self._compute_per_factor_cma_metrics(
                                    rank_t=cma_rank_t,
                                    ret_t=cma_ret_t,
                                    factor_names=[str(n) for n in factor_names if n],
                                    weights_map=weights_map,
                                    score_mode="light",
                                )
                    except Exception as e:
                        algorithm.Debug(f"[CMA-Light] Error: {e}, falling back to zscore")
                        score_mode = "full"
                        per_factor_cma_metrics = []
                        per_factor_cma_metrics_meta = {
                            "window_days_used": int(self.eval_window),
                            "score_mode": "light",
                            "available": False,
                            "unavailable_reason": "cma_light_failed",
                        }
                        effective_method = "zscore"

            if effective_method != "cmaes":
                per_factor_cma_metrics = []
                per_factor_cma_metrics_meta = {
                    "window_days_used": int(self.eval_window),
                    "score_mode": str(score_mode),
                    "available": False,
                    "unavailable_reason": "effective_method_not_cmaes",
                }
                if effective_method == "equal":
                    n = float(len(val_factors))
                    if n > 0:
                        for factor in val_factors:
                            final_scores += (1.0 / n) * factor["values"].rank(pct=True)
                elif effective_method == "fitness_sharpe":
                    dfm = pd.DataFrame(val_factors)
                    dfm["fitness_rank"] = dfm["fitness"].rank(pct=True)
                    dfm["sharpe_rank"] = dfm["sharpe"].rank(pct=True)
                    dfm["w"] = (dfm["fitness_rank"] + dfm["sharpe_rank"]).fillna(0.0)
                    w_sum = float(dfm["w"].sum())
                    if w_sum <= 1e-8:
                        dfm["w"] = 1.0 / max(len(dfm.index), 1)
                    else:
                        dfm["w"] = dfm["w"] / w_sum
                    preview = ", ".join(
                        f"{r['name']}={r['w']:.4f} (fit={r['fitness']:.3f}, sh={r['sharpe']:.3f})"
                        for _, r in dfm.head(10).iterrows()
                    )
                    algorithm.Debug(f"[FitnessSharpe] factor weights: {preview}")
                    for _, r in dfm.iterrows():
                        final_scores += r["w"] * r["values"].rank(pct=True)
                else:
                    dfm = pd.DataFrame(val_factors)
                    dfm["w"] = (zscore(dfm["icir"]) + zscore(dfm["return"]) + zscore(dfm["sortino"])) / 3
                    for _, r in dfm.iterrows():
                        final_scores += r["w"] * r["values"].rank(pct=True)

            # Select top stocks
            algorithm.Debug(f"[ScoreMode] score_mode={score_mode}")
            clean_scores = final_scores.dropna()
            top_stocks = clean_scores.nlargest(8)
            bottom_stocks = clean_scores.nsmallest(min(8, len(clean_scores)))
            algorithm.Debug(
                f"[Result] Top 8: {[self._symbol_label(s) for s in top_stocks.index]} "
                f"Scores: {[f'{v:.4f}' for v in top_stocks.values]}"
            )
            algorithm.Debug(
                f"[Result] Bottom 8: {[self._symbol_label(s) for s in bottom_stocks.index]} "
                f"Scores: {[f'{v:.4f}' for v in bottom_stocks.values]}"
            )

            factor_context = []
            for factor in val_factors:
                name = factor.get("name")
                expr = factor.get("expr")
                if not isinstance(name, str):
                    continue
                expr_str = str(expr) if expr is not None else ""
                expr_hash = hashlib.sha1(expr_str.encode("utf-8")).hexdigest()[:12] if expr_str else ""
                factor_context.append(
                    {
                        "factor_name": name,
                        "name": name,
                        "expr": expr_str,
                        "expr_hash": expr_hash,
                        "effective_direction": int(factor.get("effective_direction", 1) or 1),
                    }
                )

            report_date = (
                self._resolve_preview_report_date(current_date)
                if is_preview
                else current_date
            )
            persist_preview_outputs = self._should_persist_preview_outputs(
                algorithm=algorithm,
                is_preview=bool(is_preview),
                current_time=current_time,
                market_finalize=market_finalize,
                is_pre_market_now=is_pre_market_now,
                current_date=current_date,
                report_date=report_date,
            )
            report_data_as_of_date = self._resolve_factor_panel_data_as_of_date(
                f_vals_map,
                factor_names=score_factor_names,
                fallback=report_date,
            )

            if persist_preview_outputs:
                report = self._build_daily_cma_report(
                    algorithm,
                    report_date,
                    final_scores,
                    score_mode=score_mode,
                    rebalance_today=rebalance_today,
                    data_dict=data_dict,
                    objective_breakdown=objective_breakdown,
                    factor_context=factor_context,
                    per_factor_cma_metrics=per_factor_cma_metrics,
                    per_factor_cma_metrics_meta=per_factor_cma_metrics_meta,
                    data_as_of_date=report_data_as_of_date,
                )
                self._persist_daily_cma_report(algorithm, report)
                if is_preview:
                    algorithm.Debug(
                        f"[PREVIEW] Persisted report artifacts date={report_date} "
                        f"(runtime={current_date} time={current_time})"
                    )
            elif is_preview:
                algorithm.Debug(
                    f"[PREVIEW] Report persistence deferred until finalize "
                    f"(runtime={current_date} time={current_time})"
                )

            clean_scores = {}
            for symbol, value in pd.to_numeric(final_scores, errors="coerce").items():
                if pd.notna(value) and np.isfinite(value):
                    clean_scores[self._symbol_label(symbol)] = float(value)
            self.last_score_snapshot = {
                "date": str(report_date),
                "scores": clean_scores,
            }
            self.last_score_mode = score_mode
            if persist_preview_outputs:
                self._persist_cma_state(algorithm, report_date)

            # Emit insights (unless this is a deferred run)
            if skip_emit:
                if is_preview:
                    algorithm.Debug("[PREVIEW] Skipping insight emission. Will emit on next scheduled event.")
                else:
                    algorithm.Debug(f"[Insights] Skipped emission (score_mode={score_mode})")
            else:
                w = 1.0 / len(top_stocks) if not top_stocks.empty else 0
                for s in top_stocks.index:
                    insight = Insight.Price(
                        s,
                        timedelta(days=9999),
                        InsightDirection.UP,
                        None,
                        None,
                        None,
                        w,
                    )
                    if insight is None or getattr(insight, "Symbol", None) is None:
                        algorithm.Debug(
                            f"[Insights] Skip invalid insight for symbol={self._symbol_label(s)}"
                        )
                        continue
                    insights.append(insight)
                algorithm.Debug(f"[Insights] Emitted {len(insights)} insights")

        except Exception as e:
            algorithm.Debug(f"Alpha model error: {e}")
            import traceback
            algorithm.Debug(traceback.format_exc())

        return insights

    def on_securities_changed(self, algorithm: QCAlgorithm, changes: SecurityChanges) -> None:
        pass

    def _log_full_nan_stats(self, algorithm, factor_map: Dict[str, pd.DataFrame]):
        """Log comprehensive NaN statistics for all factors."""
        algorithm.Debug("\n[Full NaN Statistics] (Overall DataFrame NaNs)")
        for name, df in factor_map.items():
            if df.empty:
                algorithm.Debug(f"  {name}: EMPTY")
                continue
                
            total_cells = df.size
            nan_cells = df.isna().sum().sum()
            nan_pct = (nan_cells / total_cells) * 100
            
            # Also check latest row specifically
            latest_nans = df.iloc[-1].isna().sum() if not df.empty else 0
            latest_pct = (latest_nans / df.shape[1]) * 100
            clean_mask = ~df.isna().any(axis=1)
            clean_days = int(clean_mask.sum())
            total_days = int(df.shape[0])
            if clean_days > 0:
                clean_index = df.index[clean_mask]
                first_clean = clean_index[0]
                last_clean = clean_index[-1]
                clean_range = f"{first_clean}..{last_clean}"
            else:
                clean_range = "none"
            
            algorithm.Debug(
                f"  {name}: Total NaN={nan_cells}/{total_cells} ({nan_pct:.1f}%) | "
                f"Latest NaN={latest_nans}/{df.shape[1]} ({latest_pct:.1f}%) | "
                f"CleanDays={clean_days}/{total_days} range={clean_range}"
            )
            
            # If high NaNs, drill down into first few rows vs last few rows
            if nan_pct > 50:
                head_nans = df.iloc[:10].isna().sum().sum()
                head_total = df.iloc[:10].size
                tail_nans = df.iloc[-10:].isna().sum().sum()
                tail_total = df.iloc[-10:].size
                algorithm.Debug(f"    -> Head(10) NaNs: {head_nans}/{head_total} | Tail(10) NaNs: {tail_nans}/{tail_total}")
