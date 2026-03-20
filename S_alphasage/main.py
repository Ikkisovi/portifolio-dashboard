# region imports
from AlgorithmImports import *
import pandas as pd
import numpy as np
import json
from pathlib import Path
from datetime import time as dt_time, timedelta
from universe_selection import AlphaSAGEUniverseSelectionModel
from alpha_model import AlphaSAGEAlphaModel
from risk_management import TimingExitRiskModel, ActionableFactorTimingRisk
from portfolio_construction import AlphaSAGEPortfolioConstructionModel
from minute_consolidator import IntradayFeatureConsolidator
from early_close import EarlyCloseDetector
from cache_persistence import CachePersistenceManager
from recovery_history import request_history_with_symbol_fallback
from factor_selection_report import build_factor_selection_daily_report
from typing import Optional
import hashlib
# endregion

"""
AlphaSAGE Multi-Factor Strategy

Architecture:
- minute_consolidator.py: SINGLE SOURCE OF TRUTH for daily data (OHLCV + intraday features)
- alpha_model.py: Consumer only - reads from cache, never produces data
- main.py: Orchestration, scheduling, recovery mode

Data Flow:
- Live: OnData → consolidator._update_bar() → 15:55 finalize → cache append → signal
- Recovery: History → feed_history_bars() (same accumulator) → cache populate

Weighting Methods:
- "zscore": Combine ICIR + return + Sortino z-scores
- "cmaes": Use CMA-ES to optimize factor weights
- "fitness_sharpe": Brain-style weighted long/short fitness + sharpe
- "equal": Equal weight all factors
"""


class AlphaSAGE_MultiFactors(QCAlgorithm):
    """
    AlphaSAGE Multi-Factor Strategy using QuantConnect Algorithm Framework.
    """

    def Initialize(self):
        def parse_date_param(value):
            try:
                parts = [int(x) for x in value.split("-")]
                if len(parts) == 3:
                    return parts
            except Exception:
                return None
            return None

        def parse_bool_param(name: str, default: bool) -> bool:
            raw = self.GetParameter(name)
            if raw is None or str(raw).strip() == "":
                return bool(default)
            return str(raw).strip().lower() not in {"0", "false", "no", "off"}

        def parse_int_param(name: str, default: int, minimum: int = 1) -> int:
            raw = self.GetParameter(name)
            if raw is None or str(raw).strip() == "":
                return int(default)
            try:
                return max(minimum, int(raw))
            except Exception:
                return int(default)

        def parse_float_param(name: str, default: float, minimum: Optional[float] = None) -> float:
            raw = self.GetParameter(name)
            if raw is None or str(raw).strip() == "":
                return float(default)
            try:
                out = float(raw)
            except Exception:
                out = float(default)
            if minimum is not None:
                out = max(minimum, out)
            return out

        start_param = self.GetParameter("backtest_start")
        start_parts = parse_date_param(start_param) if start_param else None
        if start_parts:
            self.SetStartDate(*start_parts)
        else:
            self.SetStartDate(2023, 1, 2)

        end_param = self.GetParameter("backtest_end")
        end_parts = parse_date_param(end_param) if end_param else None
        if end_parts:
            self.SetEndDate(*end_parts)
        self.SetCash(100000)

        # We need intraday data for same-day close execution
        self.UniverseSettings.Resolution = Resolution.Minute
        self.UniverseSettings.FillForward = True
        self.UniverseSettings.ExtendedMarketHours = True

        # No ETF benchmarks; use SPY only for scheduling and regime exclusion
        self.benchmark_symbols = []

        # Set Algorithm Framework components
        self.SetUniverseSelection(AlphaSAGEUniverseSelectionModel())

        # Weighting method options:
        # - "zscore": Combine ICIR + return + Sortino z-scores
        # - "cmaes": Use CMA-ES to optimize factor weights (manual implementation)
        # - "fitness_sharpe": Brain-style weighted long/short fitness + sharpe
        # - "equal": Equal weight all factors
        self.weighting_method = "cmaes"
        method_param = self.GetParameter("weighting_method")
        if method_param:
            self.weighting_method = method_param.strip().lower()
        if self.weighting_method not in {"zscore", "cmaes", "equal", "fitness_sharpe"}:
            self.weighting_method = "cmaes"

        rebalance_param = 20
        self.rebalance_days = int(rebalance_param) if rebalance_param else 20

        eval_param = 80
        self.eval_window = int(eval_param) if eval_param else 60

        horizon_param = self.GetParameter("prediction_horizon_days")
        if horizon_param:
            self.prediction_horizon = max(1, int(horizon_param))
        else:
            self.prediction_horizon = max(1, self.rebalance_days)

        lookback_param = self.GetParameter("prediction_lookback_days")
        self.prediction_lookback_days = int(lookback_param) if lookback_param else None

        feature_mode_param = self.GetParameter("feature_update_mode")
        self.feature_update_mode = (feature_mode_param or "incremental").strip().lower()
        if self.feature_update_mode not in {"incremental", "batch"}:
            self.feature_update_mode = "incremental"
        self.feature_fft_intraday_interval = parse_int_param(
            "feature_fft_intraday_interval",
            default=30,
            minimum=1,
        )
        self.feature_shadow_mode = parse_bool_param("feature_shadow_mode", default=False)
        self.feature_shadow_diff_tol = parse_float_param(
            "feature_shadow_diff_tol",
            default=1e-4,
            minimum=0.0,
        )

        self.cma_daily_report_enabled = parse_bool_param("cma_daily_report_enabled", default=True)
        self.cma_daily_report_top_n = parse_int_param("cma_daily_report_top_n", default=20, minimum=1)
        self.cma_daily_report_bucket_size = parse_int_param(
            "cma_daily_report_bucket_size",
            default=8,
            minimum=1,
        )
        self.cma_daily_report_sink = (self.GetParameter("cma_daily_report_sink") or "log+objectstore").strip().lower()
        self.cma_light_report_nonrebalance = parse_bool_param(
            "cma_light_report_nonrebalance",
            default=True,
        )
        self.cma_light_score_source = (
            self.GetParameter("cma_light_score_source") or "last_cma_weights"
        ).strip().lower()
        self.backtest_fast_recovery = parse_bool_param("backtest_fast_recovery", default=True)
        self.backtest_max_runtime_minutes = parse_int_param(
            "backtest_max_runtime_minutes",
            default=15,
            minimum=1,
        )
        self.cma_obj_logret_weight = parse_float_param(
            "cma_obj_logret_weight",
            default=1.0,
            minimum=0.0,
        )
        self.cma_obj_ic_weight = parse_float_param(
            "cma_obj_ic_weight",
            default=1.0,
            minimum=0.0,
        )
        self.cma_obj_fitness_weight = parse_float_param(
            "cma_obj_fitness_weight",
            default=1.0,
            minimum=0.0,
        )
        actionable_eval_window = parse_int_param(
            "actionable_eval_window",
            default=40,
            minimum=1,
        )

        # No warmup - all historical data fetched via history() calls

        alpha_model = AlphaSAGEAlphaModel(
            weighting_method=self.weighting_method,
            rebalance_days=self.rebalance_days,
            eval_window=self.eval_window,
            prediction_horizon=self.prediction_horizon,
            prediction_lookback_days=self.prediction_lookback_days,
            cma_daily_report_enabled=self.cma_daily_report_enabled,
            cma_daily_report_top_n=self.cma_daily_report_top_n,
            cma_daily_report_bucket_size=self.cma_daily_report_bucket_size,
            cma_daily_report_sink=self.cma_daily_report_sink,
            cma_light_report_nonrebalance=self.cma_light_report_nonrebalance,
            cma_light_score_source=self.cma_light_score_source,
            cma_obj_logret_weight=self.cma_obj_logret_weight,
            cma_obj_ic_weight=self.cma_obj_ic_weight,
            cma_obj_fitness_weight=self.cma_obj_fitness_weight,
        )
        self.alpha_model = alpha_model  # Store reference before AddAlpha
        self.AddAlpha(alpha_model)
        self.alpha_model.regime_symbols = self.benchmark_symbols
        self.Debug(
            f"[Config] weighting_method={self.weighting_method} "
            f"rebalance_days={self.rebalance_days} eval_window={self.eval_window} "
            f"prediction_horizon_days={self.prediction_horizon} "
            f"prediction_lookback_days={self.prediction_lookback_days} "
            f"feature_update_mode={self.feature_update_mode} "
            f"feature_fft_intraday_interval={self.feature_fft_intraday_interval} "
            f"feature_shadow_mode={self.feature_shadow_mode} "
            f"cma_daily_report_enabled={self.cma_daily_report_enabled} "
            f"cma_daily_report_sink={self.cma_daily_report_sink} "
            f"backtest_fast_recovery={self.backtest_fast_recovery} "
            f"backtest_max_runtime_minutes={self.backtest_max_runtime_minutes} "
            f"cma_obj_logret_weight={self.cma_obj_logret_weight} "
            f"cma_obj_ic_weight={self.cma_obj_ic_weight} "
            f"cma_obj_fitness_weight={self.cma_obj_fitness_weight} "
            f"actionable_eval_window={actionable_eval_window}"
        )

        # Portfolio construction: Only rebalance when new insights arrive
        def never_rebalance(dt):
            """Return a date very far in the future to effectively disable auto-rebalancing"""
            return dt + timedelta(days=9999)

        self.SetPortfolioConstruction(
            AlphaSAGEPortfolioConstructionModel(
                num_positions=8,
                rebalance=never_rebalance
            )
        )

        # Use ImmediateExecutionModel for actual order placement
        self.SetExecution(ImmediateExecutionModel())

        # Actionable factor timing risk (daily exit/restore for bottom-bucket stocks)
        enable_actionable_timing = parse_bool_param("enable_actionable_timing", default=True)
        enable_market_regime_gate = parse_bool_param("enable_market_regime_gate", default=True)
        market_regime_train_days = parse_int_param("market_regime_train_days", default=252, minimum=40)
        market_regime_cluster_count = parse_int_param("market_regime_cluster_count", default=4, minimum=2)
        market_regime_blocked_labels = (self.GetParameter("market_regime_blocked_labels") or "3").strip()
        if enable_actionable_timing:
            actionable_timing_model = ActionableFactorTimingRisk(
                bottom_n=parse_int_param("timing_bottom_n", default=8, minimum=1),
                num_positions=8,
                retrain_interval_days=self.rebalance_days,
                eval_window=actionable_eval_window,
                prediction_horizon=1,
                enable_market_regime_gate=enable_market_regime_gate,
                market_regime_train_days=market_regime_train_days,
                market_regime_cluster_count=market_regime_cluster_count,
                market_regime_blocked_labels=market_regime_blocked_labels,
            )
            actionable_timing_model.alpha_model = alpha_model
            self.SetRiskManagement(actionable_timing_model)
            self.actionable_timing_model = actionable_timing_model
            self.Debug(
                f"[Config] Actionable timing risk enabled "
                f"(bottom_n={actionable_timing_model.bottom_n}, "
                f"retrain_days={actionable_timing_model.retrain_interval_days}, "
                f"eval_window={actionable_timing_model.eval_window}, "
                f"market_regime_gate={actionable_timing_model.enable_market_regime_gate}, "
                f"market_regime_train_days={actionable_timing_model.market_regime_train_days})"
            )
        else:
            self.Debug("[Config] Actionable timing risk disabled.")

        # Schedule signal generation before market close
        self.spy = self.AddEquity("SPY", Resolution.Minute, Market.USA, True, 1, True).Symbol
        self.benchmark_symbols = [self.spy]
        self.alpha_model.regime_symbols = self.benchmark_symbols

        # Early close detector
        self.early_close_detector = EarlyCloseDetector(self)

        # Cache persistence manager
        self.cache_manager = CachePersistenceManager(self, use_object_store=True)
        self._sector_map = self._load_sector_map()

        # ========================================
        # Deployment Detection (Pre/Post Market)
        # Run preview immediately on deployment
        # ========================================
        self._run_deployment_preview = False
        self._is_pre_market = False  # Flag for alpha_model to check
        self._is_post_market = False
        
        if self.LiveMode:
            from datetime import time as dt_time
            now_time = self.Time.time()
            market_open = dt_time(9, 30)
            market_close = dt_time(16, 0)
            
            if now_time < market_open:
                # Pre-market deploy: use last trading day data, NO today's snapshot
                self.Debug(f"[PRE-MARKET DEPLOY] Deployed at {self.Time}. Will run preview with ONLY last trading day data.")
                self.alpha_model.defer_first_rebalance = True
                self.alpha_model.is_pre_market = True  # Tell alpha_model to skip today's snapshot
                self._is_pre_market = True
                self._run_deployment_preview = True
            elif now_time > market_close:
                # Post-market deploy: market already closed today
                self.Debug(f"[POST-MARKET DEPLOY] Deployed at {self.Time}. First rebalance deferred to next trading day.")
                self.alpha_model.defer_first_rebalance = True
                self.alpha_model.is_post_market = True
                self._is_post_market = True
                self._run_deployment_preview = True
            else:
                # Normal deploy during market hours
                self.Debug(f"[MARKET HOURS DEPLOY] Deployed at {self.Time}. Will run preview with latest intraday bar.")
                self.alpha_model.defer_first_rebalance = True
                self._run_deployment_preview = True
        else:
            # Backtest: always run recovery once to warm caches via History()
            self.Debug(f"[BACKTEST] Running recovery mode warmup at {self.Time}.")
            self.alpha_model.defer_first_rebalance = True
            self._run_deployment_preview = True

        def scheduled_signal_check():
            """
            UNIFIED finalize event at BeforeMarketClose(5min).
            Combines previous 15:54 intraday compute + 15:55 signal generation.
            """
            self.Debug(f">>> SCHEDULED EVENT at {self.Time}: Finalize + Signal <<<")
            self._drop_pending_insights_for_new_day()
            
            # Update return thresholds for intraday features (was in separate 15:54 event)
            if self.intraday_consolidator is not None:
                try:
                    closes = self.alpha_model.market_cache.get("close")
                    if closes is None or closes.empty:
                        self.Debug("[Finalize] No 15:55 closes in cache for return thresholds")
                    else:
                        self.intraday_consolidator.update_return_thresholds(closes.sort_index())
                except Exception as e:
                    self.Debug(f"[Finalize] Failed to update return thresholds: {e}")
            
            # Generate insights (this now calls consolidator.get_daily_row() internally)
            insights = alpha_model.generate_insights_on_schedule(self)
            if insights:
                self._emit_or_defer_insights(insights, source="Schedule")
            if self.cache_manager is not None:
                self._persist_structured_reports(current_date=self.Time.date())
                self._save_runtime_caches(
                    overwrite_dates=[self.Time.date()],
                    live_only=True,
                )
            if self.LiveMode:
                self._log_runtime_validation("15:55-finalize", current_date=self.Time.date())
                self._log_cache_sample(source="Finalize")

        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.BeforeMarketClose(self.spy, 5),
            scheduled_signal_check
        )

        # ========================================
        # Intraday Feature Consolidator
        # ========================================
        self.log_consolidator = False
        self.intraday_consolidator = None
        self._last_livefeed_log_time = None
        self._livefeed_log_interval = timedelta(minutes=10)
        self._pending_insights_enabled = parse_bool_param("pending_insights_enabled", True)
        self._pending_insights_retry_minutes = parse_int_param("pending_insights_retry_minutes", 20, minimum=1)
        self._pending_insights_log_interval = timedelta(minutes=2)
        self._pending_insights = None
        self._pending_insights_created_time = None
        self._pending_insights_symbols = []
        self._pending_insights_source = ""
        self._pending_insights_last_log_time = None
        self._pending_startup_recovery = False
        self._startup_recovery_due_time = None
        self._startup_recovery_attempted = False
        self._startup_recovery_stability_minutes = parse_int_param(
            "startup_recovery_stability_minutes",
            default=1,
            minimum=1,
        )

        # Periodic status logging independent of OnData (helps after market close)
        def scheduled_status_tick():
            if not self.LiveMode:
                return
            self._run_pending_startup_recovery_if_ready()
            self._try_emit_pending_insights(source="ScheduleTick")
            if self._last_livefeed_log_time is None or (self.Time - self._last_livefeed_log_time) >= self._livefeed_log_interval:
                self._last_livefeed_log_time = self.Time
                self._log_livefeed_sample(None, source="Schedule")
                self._log_cache_sample(source="Schedule")

        from System import TimeSpan
        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.Every(TimeSpan.FromMinutes(1)),
            scheduled_status_tick
        )

        # Schedule daily reset of minute bars at market open
        def reset_consolidator():
            if self.intraday_consolidator is not None:
                self.intraday_consolidator.reset_daily()
                if self.log_consolidator:
                    self.Debug(f"[Consolidator] Reset minute bars at {self.Time}")

        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.AfterMarketOpen(self.spy, 1),
            reset_consolidator
        )

        # NOTE: Separate 15:54 compute_intraday_features event has been REMOVED.
        # Intraday features are now computed as part of the unified 15:55 finalize event
        # via consolidator.get_daily_row() called from alpha_model._generate_insights().

        # Schedule cache save at market close (only in live mode)
        def save_caches():
            self._save_runtime_caches(
                overwrite_dates=[self.Time.date()],
                live_only=True,
            )

        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.AfterMarketClose(self.spy, 5),  # 5 minutes after market close
            save_caches
        )

        def validate_report_inputs():
            if self.LiveMode:
                self._log_runtime_validation("16:06-report-ready", current_date=self.Time.date())

        self.Schedule.On(
            self.DateRules.EveryDay(self.spy),
            self.TimeRules.AfterMarketClose(self.spy, 6),
            validate_report_inputs
        )

    def _load_sector_map(self) -> dict:
        try:
            path = Path(__file__).resolve().parents[1] / "scripts" / "sector_map.json"
            if not path.exists():
                self.Debug(f"[ReportData] sector map missing: {path}")
                return {}
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return {}
            out = {}
            for k, v in raw.items():
                if k is None:
                    continue
                key = str(k).strip().upper()
                if not key:
                    continue
                out[key] = str(v).strip() if v is not None else "Unknown"
            return out
        except Exception as e:
            self.Debug(f"[ReportData] Failed to load sector map: {e}")
            return {}

    def _compute_market_summary(self, window_days: int, as_of_date) -> Optional[dict]:
        close_df = None
        try:
            close_df = self.alpha_model.market_cache.get("close")
        except Exception:
            close_df = None
        if close_df is None or close_df.empty:
            return None

        closes = close_df.sort_index()
        cutoff = pd.Timestamp(as_of_date)
        closes = closes.loc[pd.to_datetime(closes.index).normalize() <= cutoff]
        if closes.shape[0] < window_days + 1:
            return None

        latest = pd.to_numeric(closes.iloc[-1], errors="coerce")
        base = pd.to_numeric(closes.iloc[-(window_days + 1)], errors="coerce")
        returns = (latest / base) - 1.0
        returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
        if returns.empty:
            return None

        sorted_rets = returns.sort_values(ascending=False)
        advancers = int((returns > 0).sum())
        decliners = int((returns < 0).sum())
        unchanged = int((returns == 0).sum())
        total = int(len(returns))

        def _sym_label(sym) -> str:
            return str(sym.Value) if hasattr(sym, "Value") else str(sym)

        best_5 = {str(_sym_label(k)): float(v) for k, v in sorted_rets.head(5).items()}
        worst_5 = {str(_sym_label(k)): float(v) for k, v in sorted_rets.tail(5).items()}

        sector_rows = {}
        for sym, ret in returns.items():
            label = _sym_label(sym).upper()
            sector = self._sector_map.get(label, "Unknown")
            sector_rows.setdefault(sector, []).append(float(ret))
        sector_mean = {
            sector: float(np.mean(vals)) for sector, vals in sector_rows.items() if vals
        }
        sector_sorted = sorted(sector_mean.items(), key=lambda kv: kv[1], reverse=True)
        sector_winners = [{"sector": s, "return": float(r)} for s, r in sector_sorted[:3]]
        sector_losers = [{"sector": s, "return": float(r)} for s, r in sector_sorted[-3:]]

        return {
            "as_of_date": str(as_of_date),
            "data_as_of_date": str(as_of_date),
            "window_days": int(window_days),
            "universe_mean_ret": float(returns.mean()),
            "universe_median_ret": float(returns.median()),
            "win_rate": float(advancers / total) if total > 0 else 0.0,
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "total_scored": total,
            "best_5": best_5,
            "worst_5": worst_5,
            "sector_winners": sector_winners,
            "sector_losers": sector_losers,
        }

    def _persist_structured_reports(self, current_date, source_mode: str = "finalize") -> None:
        cache_manager = getattr(self, "cache_manager", None)
        if cache_manager is None:
            return

        self._persist_timing_daily_report(current_date=current_date)
        self._persist_factor_selection_daily_report(current_date=current_date, source_mode=source_mode)

        try:
            daily = self._compute_market_summary(window_days=1, as_of_date=current_date)
            if isinstance(daily, dict) and hasattr(cache_manager, "save_market_summary_daily_report"):
                cache_manager.save_market_summary_daily_report(current_date, daily)
        except Exception as e:
            self.Debug(f"[ReportData] Failed to persist daily market summary: {e}")

        try:
            weekly = self._compute_market_summary(window_days=5, as_of_date=current_date)
            if isinstance(weekly, dict) and hasattr(cache_manager, "save_market_summary_weekly_report"):
                cache_manager.save_market_summary_weekly_report(current_date, weekly)
        except Exception as e:
            self.Debug(f"[ReportData] Failed to persist weekly market summary: {e}")

    def _persist_timing_daily_report(self, current_date) -> None:
        cache_manager = getattr(self, "cache_manager", None)
        if cache_manager is None or not hasattr(cache_manager, "save_timing_daily_report"):
            return

        model = getattr(self, "actionable_timing_model", None)
        if model is None:
            return

        current_date_str = current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10]

        def _summary_date(summary: Optional[dict]) -> str:
            if not isinstance(summary, dict):
                return ""
            return str(summary.get("as_of_date") or summary.get("data_as_of_date") or "")[:10]

        summary = getattr(model, "last_timing_summary", None)
        if _summary_date(summary) != current_date_str:
            self._trigger_timing_preview_persistence(current_date=current_date)
            summary = getattr(model, "last_timing_summary", None)

        if _summary_date(summary) != current_date_str:
            self.Debug(
                f"[ReportData] Timing summary unavailable or stale for {current_date_str}; "
                f"skip timing_daily persistence."
            )
            return

        payload = dict(summary)
        payload.setdefault("as_of_date", current_date_str)
        payload.setdefault("data_as_of_date", current_date_str)
        try:
            cache_manager.save_timing_daily_report(current_date, payload)
        except Exception as e:
            self.Debug(f"[ReportData] Failed to persist timing_daily: {e}")

    @staticmethod
    def _factor_selection_context_rows(factor_defs, include_effective_direction: bool) -> list:
        rows = []
        for item in list(factor_defs or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            expr = str(item.get("expr") or "")
            row = {
                "name": name,
                "expr": expr,
                "expr_hash": hashlib.sha1(expr.encode("utf-8")).hexdigest()[:12] if expr else "",
            }
            if include_effective_direction:
                row["effective_direction"] = int(item.get("effective_direction", 1) or 1)
            rows.append(row)
        return rows

    def _persist_factor_selection_daily_report(self, current_date, source_mode: str = "finalize") -> None:
        cache_manager = getattr(self, "cache_manager", None)
        if cache_manager is None or not hasattr(cache_manager, "save_factor_selection_daily_report"):
            return

        alpha_model = getattr(self, "alpha_model", None)
        if alpha_model is None:
            return

        cma_factor_cache = getattr(getattr(alpha_model, "factor_cache", None), "factor_values", None)
        actionable_factor_cache = getattr(
            getattr(alpha_model, "actionable_factor_cache", None),
            "factor_values",
            None,
        )
        cma_factor_panels = cma_factor_cache if isinstance(cma_factor_cache, dict) else {}
        actionable_factor_panels = actionable_factor_cache if isinstance(actionable_factor_cache, dict) else {}
        if not cma_factor_panels and not actionable_factor_panels:
            return

        payload = build_factor_selection_daily_report(
            as_of_date=current_date,
            data_as_of_date=current_date,
            source_mode=source_mode or "finalize",
            cma_factor_panels=cma_factor_panels,
            cma_factor_context=self._factor_selection_context_rows(
                getattr(alpha_model, "factor_defs", []),
                include_effective_direction=True,
            ),
            actionable_factor_panels=actionable_factor_panels,
            actionable_factor_context=self._factor_selection_context_rows(
                getattr(alpha_model, "actionable_factor_defs", []),
                include_effective_direction=False,
            ),
            selection_size=10,
            lookback_days=20,
        )
        try:
            cache_manager.save_factor_selection_daily_report(current_date, payload)
        except Exception as e:
            self.Debug(f"[ReportData] Failed to persist factor_selection_daily: {e}")

    @staticmethod
    def _runtime_date_str(value) -> str:
        if value is None:
            return ""
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        return str(value)[:10]

    def _save_runtime_caches(self, overwrite_dates: Optional[list] = None, live_only: bool = False) -> None:
        cache_manager = getattr(self, "cache_manager", None)
        if cache_manager is None:
            return
        if live_only:
            if not getattr(self, "LiveMode", False):
                return
        elif not (getattr(self, "LiveMode", False) or getattr(self, "backtest_fast_recovery", False)):
            return
        cache_manager.save_all(
            self.alpha_model,
            self.intraday_consolidator,
            overwrite_dates=list(overwrite_dates or []) or None,
        )

    def _build_runtime_validation_snapshot(self, current_date) -> dict:
        current_date_str = self._runtime_date_str(current_date)
        cache_manager = getattr(self, "cache_manager", None)

        def _has_persisted_report(method_name: str) -> bool:
            checker = getattr(cache_manager, method_name, None)
            if not callable(checker):
                return False
            try:
                return bool(checker(current_date))
            except Exception:
                return False

        last_score_snapshot = getattr(getattr(self, "alpha_model", None), "last_score_snapshot", None)
        cma_date = ""
        score_mode = str(getattr(getattr(self, "alpha_model", None), "last_score_mode", "") or "")
        scored_universe_size = 0
        if isinstance(last_score_snapshot, dict):
            cma_date = self._runtime_date_str(last_score_snapshot.get("date"))
            scores = last_score_snapshot.get("scores")
            if isinstance(scores, dict):
                scored_universe_size = len(scores)
        cma_ready = cma_date == current_date_str and bool(cma_date)
        cma_persisted_ready = _has_persisted_report("has_cma_daily_report")
        if cma_persisted_ready:
            cma_ready = True
            if not cma_date:
                cma_date = current_date_str

        timing_summary = getattr(getattr(self, "actionable_timing_model", None), "last_timing_summary", None)
        timing_date = ""
        timing_source = ""
        if isinstance(timing_summary, dict):
            timing_date = self._runtime_date_str(
                timing_summary.get("as_of_date") or timing_summary.get("data_as_of_date")
            )
            timing_source = str(timing_summary.get("score_source") or "")
        timing_ready = timing_date == current_date_str and bool(timing_date)
        timing_persisted_ready = _has_persisted_report("has_timing_daily_report")
        if timing_persisted_ready:
            timing_ready = True
            if not timing_date:
                timing_date = current_date_str

        market_cache_last = ""
        market_ready = False
        try:
            close_df = getattr(getattr(self, "alpha_model", None), "market_cache", None)
            close_df = close_df.get("close") if close_df is not None else None
            if close_df is not None and not close_df.empty:
                last_idx = close_df.index[-1]
                if hasattr(last_idx, "strftime"):
                    market_cache_last = last_idx.strftime("%Y-%m-%d %H:%M")
                    market_ready = last_idx.strftime("%Y-%m-%d") == current_date_str
                else:
                    market_cache_last = str(last_idx)
                    market_ready = str(last_idx).startswith(current_date_str)
        except Exception:
            market_cache_last = ""
            market_ready = False
        market_daily_report_ready = _has_persisted_report("has_market_summary_daily_report")
        market_weekly_report_ready = _has_persisted_report("has_market_summary_weekly_report")
        if market_daily_report_ready and market_weekly_report_ready:
            market_ready = True

        bar_counts = getattr(getattr(self, "intraday_consolidator", None), "_bar_counts", None)
        if isinstance(bar_counts, dict):
            bar_symbols_total = len(bar_counts)
            bar_symbols_ready = sum(1 for count in bar_counts.values() if count and count > 0)
        else:
            bar_symbols_total = 0
            bar_symbols_ready = 0

        pending_symbols = getattr(self, "_pending_insights_symbols", None) or []
        pending_insights = len(pending_symbols)

        return {
            "current_date": current_date_str,
            "cma_ready": cma_ready,
            "cma_date": cma_date,
            "cma_persisted_ready": cma_persisted_ready,
            "score_mode": score_mode,
            "scored_universe_size": scored_universe_size,
            "timing_ready": timing_ready,
            "timing_date": timing_date,
            "timing_persisted_ready": timing_persisted_ready,
            "timing_source": timing_source,
            "market_ready": market_ready,
            "market_cache_last": market_cache_last,
            "market_daily_report_ready": market_daily_report_ready,
            "market_weekly_report_ready": market_weekly_report_ready,
            "bar_symbols_ready": bar_symbols_ready,
            "bar_symbols_total": bar_symbols_total,
            "pending_insights": pending_insights,
            "report_inputs_ready": cma_ready and timing_ready and market_ready,
        }

    def _log_runtime_validation(self, checkpoint: str, current_date) -> None:
        snapshot = self._build_runtime_validation_snapshot(current_date)
        self.Debug(
            f"[RuntimeCheck:{checkpoint}] "
            f"date={snapshot['current_date']} "
            f"cma_ready={snapshot['cma_ready']} cma_date={snapshot['cma_date']} "
            f"score_mode={snapshot['score_mode']} scored={snapshot['scored_universe_size']} "
            f"timing_ready={snapshot['timing_ready']} timing_date={snapshot['timing_date']} "
            f"timing_source={snapshot['timing_source']} "
            f"market_ready={snapshot['market_ready']} "
            f"market_reports={snapshot['market_daily_report_ready']}/{snapshot['market_weekly_report_ready']} "
            f"market_cache_last={snapshot['market_cache_last']} "
            f"bars={snapshot['bar_symbols_ready']}/{snapshot['bar_symbols_total']} "
            f"pending_insights={snapshot['pending_insights']} "
            f"report_inputs_ready={snapshot['report_inputs_ready']}"
        )

    @staticmethod
    def _market_cache_latest_timestamp_from_algorithm(algorithm):
        try:
            close_df = getattr(getattr(algorithm, "alpha_model", None), "market_cache", None)
            close_df = close_df.get("close") if close_df is not None else None
            if close_df is None or close_df.empty:
                return None
            idx = pd.to_datetime(close_df.index, errors="coerce")
            idx = idx[~pd.isna(idx)]
            if len(idx) == 0:
                return None
            return idx.max()
        except Exception:
            return None

    def _preview_timing_override_timestamp(self, current_date):
        if current_date is None or getattr(self, "Time", None) is None:
            return None
        if bool(getattr(self, "_is_post_market", False)):
            return None
        try:
            report_date = pd.Timestamp(current_date).date()
            runtime_date = pd.Timestamp(self.Time).date()
        except Exception:
            return None
        if report_date >= runtime_date:
            return None
        latest_cache_ts = self._market_cache_latest_timestamp_from_algorithm(self)
        if latest_cache_ts is None:
            return None
        latest_cache_ts = pd.Timestamp(latest_cache_ts)
        cutoff = dt_time(15, 55)
        if latest_cache_ts.date() != report_date or latest_cache_ts.time() < cutoff:
            return None
        return pd.Timestamp(report_date) + pd.Timedelta(hours=cutoff.hour, minutes=cutoff.minute + 1)

    def _trigger_timing_preview_persistence(self, current_date=None) -> None:
        """
        Force one timing evaluation in deployment preview/recovery path so timing_daily
        is available even when no framework-emitted targets occurred yet.
        """
        model = getattr(self, "actionable_timing_model", None)
        if model is None:
            return
        forced_time = self._preview_timing_override_timestamp(current_date)
        try:
            if forced_time is not None:
                self.Debug(
                    f"[ReportData] Overnight previous-session timing replay: "
                    f"runtime={getattr(self, 'Time', None)} forced_as_of={forced_time}"
                )
                result = model.ManageRisk(self, [], as_of_time=forced_time)
            else:
                result = model.ManageRisk(self, [])
            target_count = len(result) if isinstance(result, list) else 0
            if current_date is not None:
                self.Debug(
                    f"[ReportData] Timing preview evaluation completed "
                    f"(targets={target_count}, as_of={current_date})."
                )
            else:
                self.Debug(f"[ReportData] Timing preview evaluation completed (targets={target_count}).")
        except Exception as e:
            self.Debug(f"[ReportData] Timing preview evaluation failed: {e}")

    def _try_live_regime_storage_fast_path(self) -> dict:
        result = {
            "used_storage_first": False,
            "storage_hit": False,
            "requested_external_history": False,
        }
        if not getattr(self, "LiveMode", False):
            return result
        cache_manager = getattr(self, "cache_manager", None)
        model = getattr(self, "actionable_timing_model", None)
        if cache_manager is None or model is None or not bool(getattr(model, "enable_market_regime_gate", False)):
            return result

        try:
            loaded = cache_manager.load_all(self.alpha_model, self.intraday_consolidator)
        except Exception as e:
            self.Debug(f"[RECOVERY MODE] Storage-first regime load failed: {e}")
            return result

        result["used_storage_first"] = bool(loaded)
        if not loaded:
            return result

        evaluator = getattr(model, "evaluate_market_regime_diagnostics", None)
        if not callable(evaluator):
            return result

        state, debug_payload = evaluator(
            self,
            self.Time.date(),
            used_storage_first=True,
            storage_hit=True,
            requested_external_history=False,
        )
        available_rows = int(debug_payload.get("available_row_count") or 0)
        required_rows = int(debug_payload.get("required_row_count") or 0)
        if bool(state.get("available")):
            result["storage_hit"] = True
            self.Debug(
                f"[RECOVERY MODE] Storage-first regime cache hit: "
                f"available_rows={available_rows} required_rows={required_rows}, "
                f"skip History() backfill."
            )
        else:
            self.Debug(
                f"[RECOVERY MODE] Storage-first regime cache miss: "
                f"available_rows={available_rows} required_rows={required_rows} "
                f"reason={state.get('reason') or debug_payload.get('failure_reason') or 'unknown'}"
            )
        return result

    def _persist_market_regime_debug_and_validate(
        self,
        current_date,
        *,
        used_storage_first: bool,
        storage_hit: bool,
        requested_external_history: bool,
    ) -> dict:
        model = getattr(self, "actionable_timing_model", None)
        if model is None or not bool(getattr(model, "enable_market_regime_gate", False)):
            return {}
        evaluator = getattr(model, "evaluate_market_regime_diagnostics", None)
        if not callable(evaluator):
            return {}

        state, debug_payload = evaluator(
            self,
            current_date,
            used_storage_first=used_storage_first,
            storage_hit=storage_hit,
            requested_external_history=requested_external_history,
        )
        cache_manager = getattr(self, "cache_manager", None)
        if cache_manager is not None and hasattr(cache_manager, "save_market_regime_debug_report"):
            cache_manager.save_market_regime_debug_report(current_date, debug_payload)

        if self.LiveMode and not bool(state.get("available")):
            reason = str(state.get("reason") or debug_payload.get("failure_reason") or "market_regime_unavailable")
            if reason == "no_regime_eligible_symbols":
                eligible_count = int(debug_payload.get("regime_eligible_symbol_count") or 0)
                excluded_count = int(debug_payload.get("regime_excluded_young_symbol_count") or 0)
                sample = list(debug_payload.get("regime_excluded_young_symbols_sample") or [])
                self.Debug(
                    f"[RECOVERY MODE] Market regime validation skipped: reason={reason} "
                    f"eligible={eligible_count} excluded_young={excluded_count} sample={sample[:8]}"
                )
                return state
            raise RuntimeError(
                f"Market regime warmup incomplete for {current_date}: {reason}"
            )
        return state

    def _schedule_startup_recovery(self) -> None:
        if not getattr(self, "LiveMode", False):
            return
        if not getattr(self, "_run_deployment_preview", False):
            return
        stable_minutes = max(1, int(getattr(self, "_startup_recovery_stability_minutes", 1) or 1))
        due_time = pd.Timestamp(self.Time) + pd.Timedelta(minutes=stable_minutes)
        self._pending_startup_recovery = True
        self._startup_recovery_due_time = due_time
        symbol_count = len(list(getattr(getattr(self, "intraday_consolidator", None), "symbols", []) or []))
        self.Debug(
            f"[RECOVERY MODE] Deferred startup recovery scheduled: "
            f"symbols={symbol_count} due={due_time} stability_minutes={stable_minutes}"
        )

    def _run_pending_startup_recovery_if_ready(self) -> None:
        if not getattr(self, "LiveMode", False):
            return
        if not bool(getattr(self, "_pending_startup_recovery", False)):
            return
        due_time = getattr(self, "_startup_recovery_due_time", None)
        if due_time is None or pd.Timestamp(self.Time) < pd.Timestamp(due_time):
            return
        if bool(getattr(self, "_startup_recovery_attempted", False)):
            self._pending_startup_recovery = False
            return

        consolidator = getattr(self, "intraday_consolidator", None)
        symbols = list(getattr(consolidator, "symbols", []) or [])
        if not symbols:
            self.Debug("[RECOVERY MODE] Deferred startup recovery skipped: no symbols registered yet.")
            self._pending_startup_recovery = False
            self._run_deployment_preview = False
            self._startup_recovery_attempted = True
            return

        self.Debug(
            f"[RECOVERY MODE] Deferred startup recovery starting with stable universe "
            f"symbols={len(symbols)} due={due_time}"
        )
        self._pending_startup_recovery = False
        self._run_deployment_preview = False
        self._startup_recovery_attempted = True
        self._run_recovery_mode(symbols)

    @staticmethod
    def _resolve_cache_symbol_key(columns, symbol):
        if columns is None:
            return None
        for candidate in (symbol, getattr(symbol, "Value", None), str(symbol)):
            if candidate is not None and candidate in columns:
                return candidate
        return None

    @staticmethod
    def _normalize_cache_dates(index_like) -> pd.DatetimeIndex:
        if index_like is None:
            return pd.DatetimeIndex([])
        idx = pd.to_datetime(pd.Index(index_like), errors="coerce")
        idx = idx[~pd.isna(idx)]
        if len(idx) == 0:
            return pd.DatetimeIndex([])
        return pd.DatetimeIndex(idx).normalize().unique().sort_values()

    def _build_storage_first_backfill_plan(self, symbols, lookback_days: int, history_span_days: int) -> dict:
        request_end = pd.Timestamp(self.Time)
        default_start = request_end - timedelta(days=max(1, int(history_span_days)))
        symbol_list = list(symbols or [])
        plan = {
            "history_symbols": list(symbol_list),
            "history_start": default_start,
            "history_end": request_end,
            "requested_external_history": bool(symbol_list),
        }
        if not symbol_list:
            plan["requested_external_history"] = False
            return plan

        am = getattr(self, "alpha_model", None)
        market_cache = getattr(am, "market_cache", None)
        close_df = market_cache.get("close") if market_cache is not None else pd.DataFrame()
        vol_df = market_cache.get("volume") if market_cache is not None else pd.DataFrame()
        if close_df is None or close_df.empty or vol_df is None or vol_df.empty:
            return plan

        close_df = close_df.sort_index()
        vol_df = vol_df.sort_index().reindex(index=close_df.index)
        recent_close = close_df.tail(max(1, int(lookback_days)))
        recent_vol = vol_df.tail(max(1, int(lookback_days))).reindex(index=recent_close.index)
        universe_dates = self._normalize_cache_dates(recent_close.index)
        if len(universe_dates) == 0:
            return plan

        min_required_rows = min(
            max(1, int(getattr(am, "cma_min_valid_days", len(universe_dates)) or len(universe_dates))),
            len(universe_dates),
        )

        missing_symbols = []
        candidate_starts = []
        candidate_ends = []

        for symbol in symbol_list:
            key = self._resolve_cache_symbol_key(recent_close.columns, symbol)
            if key is None:
                missing_symbols.append(symbol)
                candidate_starts.append(default_start)
                candidate_ends.append(request_end)
                continue

            close_series = pd.to_numeric(recent_close[key], errors="coerce")
            vol_series = pd.to_numeric(recent_vol[key], errors="coerce")
            valid_mask = close_series.notna() & vol_series.gt(0.0)
            valid_dates = self._normalize_cache_dates(recent_close.index[valid_mask.fillna(False)])
            available_rows = int(len(valid_dates))
            missing_dates = universe_dates.difference(valid_dates)

            front_gap_dates = pd.DatetimeIndex([])
            internal_gap_dates = pd.DatetimeIndex([])
            if len(valid_dates) > 0 and len(missing_dates) > 0:
                earliest_valid = pd.Timestamp(valid_dates.min())
                front_gap_dates = missing_dates[missing_dates < earliest_valid]
                internal_gap_dates = missing_dates[missing_dates >= earliest_valid]
            elif len(missing_dates) > 0:
                front_gap_dates = missing_dates

            if available_rows >= min_required_rows and len(front_gap_dates) == 0 and len(internal_gap_dates) == 0:
                continue

            missing_symbols.append(symbol)

            if len(internal_gap_dates) > 0:
                candidate_starts.append(pd.Timestamp(internal_gap_dates.min()))
                candidate_ends.append(pd.Timestamp(internal_gap_dates.max()) + timedelta(days=1))

            needs_front_fill = bool(len(front_gap_dates) > 0) or (
                available_rows < min_required_rows and len(internal_gap_dates) == 0
            )
            if needs_front_fill:
                gap_rows = max(0, min_required_rows - available_rows)
                if len(valid_dates) > 0:
                    earliest_valid = pd.Timestamp(valid_dates.min())
                    buffer_days = max(15, gap_rows * 3)
                    candidate_starts.append(earliest_valid - timedelta(days=buffer_days))
                    candidate_ends.append(earliest_valid + timedelta(days=1))
                else:
                    candidate_starts.append(default_start)
                    candidate_ends.append(request_end)

        model = getattr(self, "actionable_timing_model", None)
        build_feature_frame = getattr(model, "_build_market_regime_feature_frame", None)
        regime_requires_front_fill = False
        if (
            model is not None
            and bool(getattr(model, "enable_market_regime_gate", False))
            and callable(build_feature_frame)
        ):
            try:
                feature_frame = build_feature_frame(am)
            except Exception:
                feature_frame = pd.DataFrame()
            if isinstance(feature_frame, pd.DataFrame) and not feature_frame.empty:
                feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan).sort_index()
                feature_dates = self._normalize_cache_dates(feature_frame.index)
                clean_dates = self._normalize_cache_dates(feature_frame.dropna().index)
                required_feature_rows = int(max(0, int(getattr(model, "market_regime_train_days", 0) or 0)) + 1)
                if required_feature_rows > 0 and len(clean_dates) < required_feature_rows and len(feature_dates) > 0:
                    first_clean_date = pd.Timestamp(clean_dates.min()) if len(clean_dates) > 0 else None
                    if first_clean_date is not None:
                        first_clean_pos = feature_dates.get_indexer([first_clean_date])[0]
                        warmup_padding = max(0, int(first_clean_pos))
                    else:
                        warmup_padding = 40
                    required_raw_rows = required_feature_rows + warmup_padding
                    raw_gap_rows = max(0, required_raw_rows - len(feature_dates))
                    if raw_gap_rows > 0:
                        regime_requires_front_fill = True
                        earliest_feature_date = pd.Timestamp(feature_dates.min())
                        buffer_days = max(30, raw_gap_rows * 3)
                        candidate_starts.append(earliest_feature_date - timedelta(days=buffer_days))
                        candidate_ends.append(earliest_feature_date + timedelta(days=1))

        if regime_requires_front_fill:
            missing_symbols = list(dict.fromkeys(list(missing_symbols) + list(symbol_list)))

        if missing_symbols:
            plan["history_symbols"] = list(dict.fromkeys(missing_symbols))
            if candidate_starts:
                plan["history_start"] = min(candidate_starts)
            if candidate_ends:
                plan["history_end"] = min(request_end, max(candidate_ends))
            if plan["history_end"] <= plan["history_start"]:
                plan["history_start"] = default_start
                plan["history_end"] = request_end
            plan["requested_external_history"] = True
            return plan

        plan["history_symbols"] = []
        plan["requested_external_history"] = False
        return plan

    def OnData(self, data: Slice):
        """Called on every data event. Feed minute bars to consolidator."""
        if self.intraday_consolidator is not None:
            self.intraday_consolidator.on_data(data)
        self._try_emit_pending_insights(source="OnData")
        if self.LiveMode:
            if self._last_livefeed_log_time is None or (self.Time - self._last_livefeed_log_time) >= self._livefeed_log_interval:
                self._last_livefeed_log_time = self.Time
                self._log_livefeed_sample(data)

    def _insight_symbols(self, insights) -> list:
        symbols = []
        seen = set()
        for insight in insights or []:
            symbol = getattr(insight, "Symbol", None)
            if symbol is None:
                symbol = getattr(insight, "symbol", None)
            if symbol is None or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    def _sanitize_insights(self, insights, source: str = "Schedule") -> list:
        valid = []
        null_count = 0
        symbolless_count = 0
        for insight in insights or []:
            if insight is None:
                null_count += 1
                continue
            symbol = getattr(insight, "Symbol", None)
            if symbol is None:
                symbol = getattr(insight, "symbol", None)
            if symbol is None:
                symbolless_count += 1
                continue
            valid.append(insight)

        if null_count or symbolless_count:
            self.Debug(
                f"[Insights:{source}] Dropped invalid insights: "
                f"none={null_count} symbolless={symbolless_count}"
            )
        return valid

    def _symbol_has_executable_price(self, symbol) -> bool:
        security = None
        try:
            security = self.Securities[symbol] if symbol in self.Securities else None
        except Exception:
            security = None

        checker = getattr(self.alpha_model, "_has_security_price", None)
        if callable(checker):
            try:
                return bool(checker(security))
            except Exception:
                pass

        if security is None:
            return False

        try:
            return float(security.Price) > 0
        except Exception:
            return False

    def _price_ready_stats_for_insights(self, insights):
        symbols = self._insight_symbols(insights)
        ready = 0
        missing = []
        for symbol in symbols:
            if self._symbol_has_executable_price(symbol):
                ready += 1
            else:
                missing.append(symbol.Value if hasattr(symbol, "Value") else str(symbol))
        return ready, len(symbols), missing

    def _clear_pending_insights(self) -> None:
        self._pending_insights = None
        self._pending_insights_created_time = None
        self._pending_insights_symbols = []
        self._pending_insights_source = ""
        self._pending_insights_last_log_time = None

    def _drop_pending_insights_for_new_day(self) -> None:
        if not self._pending_insights:
            return
        created_time = self._pending_insights_created_time
        if created_time is None:
            return
        if created_time.date() < self.Time.date():
            self.Debug(
                f"[Insights:Pending] Dropping previous-day deferred insights created at {created_time}"
            )
            self._clear_pending_insights()

    def _emit_or_defer_insights(self, insights, source: str = "Schedule") -> bool:
        if not insights:
            return False
        insights = self._sanitize_insights(insights, source=source)
        if not insights:
            return False

        if not getattr(self, "_pending_insights_enabled", True):
            self.EmitInsights(insights)
            return True

        ready, total, missing = self._price_ready_stats_for_insights(insights)
        if total == 0 or ready == total:
            self.EmitInsights(insights)
            return True

        self._pending_insights = list(insights)
        self._pending_insights_created_time = self.Time
        self._pending_insights_symbols = self._insight_symbols(insights)
        self._pending_insights_source = source
        self._pending_insights_last_log_time = None
        self.Debug(
            f"[Insights:Pending] Deferred {len(insights)} insights at {self.Time} "
            f"(price-ready {ready}/{total}) missing={missing[:8]}"
        )
        return False

    def _try_emit_pending_insights(self, source: str = "OnData") -> None:
        pending = self._pending_insights
        if not pending:
            return
        pending = self._sanitize_insights(pending, source=source)
        if not pending:
            self._clear_pending_insights()
            return

        created_time = self._pending_insights_created_time or self.Time
        retry_minutes = max(1, int(getattr(self, "_pending_insights_retry_minutes", 20)))
        max_age = timedelta(minutes=retry_minutes)
        age = self.Time - created_time

        ready, total, missing = self._price_ready_stats_for_insights(pending)
        if total == 0 or ready == total:
            self.EmitInsights(pending)
            self.Debug(
                f"[Insights:Pending] Emitted deferred {len(pending)} insights via {source} "
                f"after {age.total_seconds() / 60.0:.1f}m"
            )
            self._clear_pending_insights()
            return

        if age >= max_age:
            self.Debug(
                f"[Insights:Pending] Dropped deferred insights after {age.total_seconds() / 60.0:.1f}m "
                f"(price-ready {ready}/{total}) missing={missing[:8]}"
            )
            self._clear_pending_insights()
            return

        last_log = self._pending_insights_last_log_time
        log_interval = getattr(self, "_pending_insights_log_interval", timedelta(minutes=2))
        if last_log is None or (self.Time - last_log) >= log_interval:
            self._pending_insights_last_log_time = self.Time
            self.Debug(
                f"[Insights:Pending] Waiting for prices via {source}: "
                f"price-ready {ready}/{total}, age={age.total_seconds() / 60.0:.1f}m, "
                f"missing={missing[:8]}"
            )

    def _log_cache_sample(self, source: str = "OnData") -> None:
        try:
            closes = self.alpha_model.market_cache.get("close")
        except Exception:
            closes = None
        if closes is None or closes.empty:
            self.Debug(f"[Cache:{source}] close cache empty")
            return

        cache_ts = closes.index[-1]
        if self.intraday_consolidator is not None and self.intraday_consolidator.symbols:
            sample = self.intraday_consolidator.symbols[:5]
        else:
            sample = list(closes.columns[:5])

        parts = []
        for symbol in sample:
            name = symbol.Value if hasattr(symbol, "Value") else str(symbol)
            if symbol in closes.columns:
                value = closes[symbol].iloc[-1]
            elif hasattr(symbol, "Value") and symbol.Value in closes.columns:
                value = closes[symbol.Value].iloc[-1]
            else:
                value = float("nan")
            if pd.notna(value):
                parts.append(f"{name}={float(value):.2f}")
            else:
                parts.append(f"{name}=NaN")
        self.Debug(f"[Cache:{source}] last={cache_ts} " + " | ".join(parts))

    def _log_livefeed_sample(self, data: Optional[Slice], source: str = "OnData") -> None:
        if self.intraday_consolidator is None or not self.intraday_consolidator.symbols:
            self.Debug("[LiveFeed] No symbols registered yet")
            return

        cache_close = None
        cache_ts = None
        try:
            closes = self.alpha_model.market_cache.get("close")
            if closes is not None and not closes.empty:
                cache_ts = closes.index[-1]
                cache_close = closes.iloc[-1]
        except Exception:
            pass

        sample = self.intraday_consolidator.symbols[:5]
        parts = []
        for symbol in sample:
            security = self.Securities[symbol] if symbol in self.Securities else None
            price = security.Price if security is not None else 0.0
            # If we only have quotes, compute a mid price from the cache.
            mid_price = None
            if security is not None:
                try:
                    bid = float(security.Cache.BidPrice)
                    ask = float(security.Cache.AskPrice)
                    if bid > 0 and ask > 0:
                        mid_price = 0.5 * (bid + ask)
                    elif bid > 0:
                        mid_price = bid
                    elif ask > 0:
                        mid_price = ask
                except Exception:
                    mid_price = None
            bar_count = self.intraday_consolidator._bar_counts.get(symbol, 0)
            cache_str = ""
            cache_val = float("nan")
            if cache_close is not None:
                if symbol in cache_close.index:
                    cache_val = cache_close.loc[symbol]
                elif symbol.Value in cache_close.index:
                    cache_val = cache_close.loc[symbol.Value]
                else:
                    cache_val = float("nan")
                if pd.notna(cache_val):
                    cache_str = f" cache={float(cache_val):.2f}"
            # After hours, Security.Price can be 0; prefer last cached close for logging
            display_price = price
            if display_price is None or display_price <= 0:
                if mid_price is not None and mid_price > 0:
                    display_price = float(mid_price)
                elif pd.notna(cache_val):
                    display_price = float(cache_val)

            if data is not None and symbol in data.Bars:
                bar = data.Bars[symbol]
                parts.append(
                    f"{symbol.Value}:bar={bar.EndTime:%H:%M} close={bar.Close:.2f} price={display_price:.2f}{cache_str} cnt={bar_count}"
                )
            else:
                parts.append(f"{symbol.Value}:bar=None price={display_price:.2f}{cache_str} cnt={bar_count}")

        prefix = f"[LiveFeed:{source}]"
        if cache_ts is not None:
            prefix += f" cacheLast={cache_ts}"
        self.Debug(prefix + " " + " | ".join(parts))

    def OnSecuritiesChanged(self, changes: SecurityChanges):
        """Handle securities changes - update consolidator symbols."""
        added_symbols = [sec.Symbol for sec in changes.AddedSecurities
                         if sec.Symbol.SecurityType == SecurityType.Equity]
        removed_symbols = [sec.Symbol for sec in changes.RemovedSecurities
                           if sec.Symbol.SecurityType == SecurityType.Equity]
        # Subscriptions are created in the universe selection model.

        if hasattr(self, 'benchmark_symbols') and self.benchmark_symbols:
            added_symbols = [s for s in added_symbols if s not in self.benchmark_symbols]
            removed_symbols = [s for s in removed_symbols if s not in self.benchmark_symbols]

        if self.intraday_consolidator is None and added_symbols:
            self.intraday_consolidator = IntradayFeatureConsolidator(
                self,
                added_symbols,
                history_window=600,
                feature_update_mode=self.feature_update_mode,
                feature_fft_intraday_interval=self.feature_fft_intraday_interval,
                feature_shadow_mode=self.feature_shadow_mode,
                feature_shadow_diff_tol=self.feature_shadow_diff_tol,
            )
            self.intraday_consolidator.set_early_close_detector(self.early_close_detector)
            self.alpha_model.intraday_consolidator = self.intraday_consolidator
            
            # Run deployment preview / recovery mode after consolidator is set up
            if getattr(self, '_run_deployment_preview', False):
                if self.LiveMode:
                    self._schedule_startup_recovery()
                else:
                    self._run_deployment_preview = False
                    self._run_recovery_mode(added_symbols)
        elif self.intraday_consolidator is not None:
            for sym in added_symbols:
                self.intraday_consolidator.add_symbol(sym)
            for sym in removed_symbols:
                self.intraday_consolidator.remove_symbol(sym)
            if self.LiveMode and getattr(self, '_run_deployment_preview', False) and (added_symbols or removed_symbols):
                self._schedule_startup_recovery()

    def OnFrameworkSecuritiesChanged(self, changes: SecurityChanges):
        """Forward framework securities change events to the base handler."""
        try:
            QCAlgorithm.OnFrameworkSecuritiesChanged(self, changes)
        except Exception as e:
            self.Debug(f"[OnFrameworkSecuritiesChanged] Base call failed: {e}")

    def OnEndOfAlgorithm(self):
        if self.cache_manager and (self.LiveMode or self.backtest_fast_recovery):
            try:
                self._save_runtime_caches(overwrite_dates=[self.Time.date()])
                self.Debug("[CachePersistence] Saved caches at OnEndOfAlgorithm")
            except Exception as e:
                self.Debug(f"[CachePersistence] OnEnd save failed: {e}")

    def _clip_backtest_caches_to_date(self, cutoff_date):
        """
        Remove any rows >= cutoff_date from loaded caches to avoid lookahead in backtests.
        """
        cutoff_ts = pd.Timestamp(cutoff_date)

        market_cache = getattr(self.alpha_model, "market_cache", None)
        if market_cache is not None:
            for key, frame in list(market_cache.frames.items()):
                if frame is None or frame.empty:
                    continue
                idx = pd.to_datetime(frame.index, errors="coerce")
                market_cache.frames[key] = frame.loc[idx < cutoff_ts]

        def _clip_factor_cache(cache_obj):
            if cache_obj is None:
                return
            for name, frame in list(getattr(cache_obj, "factor_values", {}).items()):
                if frame is None or frame.empty:
                    continue
                idx = pd.to_datetime(frame.index, errors="coerce")
                clipped = frame.loc[idx < cutoff_ts]
                if clipped.empty:
                    cache_obj.factor_values.pop(name, None)
                    cache_obj.last_date.pop(name, None)
                else:
                    cache_obj.factor_values[name] = clipped
                    cache_obj.last_date[name] = clipped.index[-1]

            for name, series in list(getattr(cache_obj, "daily_ic", {}).items()):
                if series is None or series.empty:
                    continue
                idx = pd.to_datetime(series.index, errors="coerce")
                clipped = series.loc[idx < cutoff_ts]
                if clipped.empty:
                    cache_obj.daily_ic.pop(name, None)
                else:
                    cache_obj.daily_ic[name] = clipped

            for name, series in list(getattr(cache_obj, "daily_ls_returns", {}).items()):
                if series is None or series.empty:
                    continue
                idx = pd.to_datetime(series.index, errors="coerce")
                clipped = series.loc[idx < cutoff_ts]
                if clipped.empty:
                    cache_obj.daily_ls_returns.pop(name, None)
                else:
                    cache_obj.daily_ls_returns[name] = clipped

        _clip_factor_cache(getattr(self.alpha_model, "factor_cache", None))
        _clip_factor_cache(getattr(self.alpha_model, "actionable_factor_cache", None))

        consolidator = getattr(self, "intraday_consolidator", None)
        if consolidator is not None and hasattr(consolidator, "feature_history"):
            history = getattr(consolidator.feature_history, "history", {})
            for feature_name, frame in list(history.items()):
                if frame is None or frame.empty:
                    continue
                idx = pd.to_datetime(frame.index, errors="coerce")
                history[feature_name] = frame.loc[idx < cutoff_ts]

    def _run_recovery_mode(self, symbols):
        """
        Recovery mode: Backfill caches from history when algorithm starts with empty/stale cache.
        
        This is the ONLY place where history() is called for data production.
        Uses feed_history_bars() which goes through the same per-bar accumulator as live data.
        
        NOTE: If deploying pre-market, we backfill history but do NOT create today's snapshot.
        The generate_insights will use cache only (no today's snapshot) for pre-market.
        """
        from datetime import time as dt_time
        current_time = self.Time.time()
        market_open = dt_time(9, 30)
        market_finalize = dt_time(15, 55)
        
        self.Debug(f"[RECOVERY MODE] Running at {self.Time} (pre-market: {current_time < market_open})")
        regime_history_days = 0
        ensure_history_capacity = getattr(self.alpha_model, "_ensure_market_regime_history_capacity", None)
        if callable(ensure_history_capacity):
            regime_history_days = int(ensure_history_capacity(self) or 0)
        factor_lb = getattr(self.alpha_model, "factor_lookback_days", 0)
        factor_padding = 30
        min_lookback = self.eval_window + self.prediction_horizon + max(60, factor_lb + factor_padding)
        lookback_days = max(self.alpha_model.lookback_days, min_lookback, regime_history_days)
        history_span_days = max(int(lookback_days * 2), lookback_days + 120)
        chunk_param = self.GetParameter("recovery_history_chunk_days")
        history_chunk_days = int(chunk_param) if chunk_param else 0
        backfill_chunk_param = self.GetParameter("recovery_backfill_chunks")
        backfill_chunks = int(backfill_chunk_param) if backfill_chunk_param else 1
        # Auto two-phase pipeline when not explicitly configured
        if not chunk_param and not backfill_chunk_param:
            if history_span_days >= 120:
                history_chunk_days = max(1, int((history_span_days + 1) / 2))
                backfill_chunks = max(2, backfill_chunks)
                self.Debug(
                    f"[RECOVERY MODE] Auto two-phase enabled: history_chunk_days={history_chunk_days} "
                    f"backfill_chunks={backfill_chunks}"
                )
        if backfill_chunks < 1:
            backfill_chunks = 1

        storage_context = {
            "used_storage_first": False,
            "storage_hit": False,
            "requested_external_history": False,
        }
        if self.LiveMode:
            storage_context = self._try_live_regime_storage_fast_path()

        if (not self.LiveMode) and self.backtest_fast_recovery and self.cache_manager:
            try:
                loaded = self.cache_manager.load_all(self.alpha_model, self.intraday_consolidator)
            except Exception as e:
                loaded = False
                self.Debug(f"[RECOVERY MODE] Fast recovery load failed: {e}")

            if loaded:
                close_df_before_clip = self.alpha_model.market_cache.get("close")
                loaded_rows = 0
                loaded_first = None
                loaded_last = None
                if close_df_before_clip is not None and not close_df_before_clip.empty:
                    loaded_rows = len(close_df_before_clip.index)
                    loaded_first = close_df_before_clip.index.min()
                    loaded_last = close_df_before_clip.index.max()

                self._clip_backtest_caches_to_date(self.Time.date())
                close_df = self.alpha_model.market_cache.get("close")
                clipped_rows = 0
                usable_rows = 0
                coverage = 0.0
                if close_df is not None and not close_df.empty:
                    clipped_rows = len(close_df.index)
                    recent = close_df.tail(lookback_days)
                    usable_rows = len(recent.index)
                    if len(recent.columns) > 0:
                        min_valid = max(1, min(self.alpha_model.cma_min_valid_days, usable_rows))
                        valid_symbols = int((recent.notna().sum(axis=0) >= min_valid).sum())
                        coverage = valid_symbols / max(1, len(recent.columns))

                min_rows = max(20, min(lookback_days, 120))
                min_coverage = 0.6
                range_suffix = ""
                if loaded_first is not None and loaded_last is not None:
                    range_suffix = f" loaded_range={loaded_first}..{loaded_last}"
                self.Debug(
                    f"[RECOVERY MODE] Fast recovery diagnostics: "
                    f"loaded_rows={loaded_rows} clipped_rows={clipped_rows} "
                    f"usable_rows={usable_rows} required_rows={min_rows} "
                    f"coverage={coverage:.2f} required_coverage={min_coverage:.2f}"
                    f"{range_suffix}"
                )

                if usable_rows >= min_rows and coverage >= min_coverage:
                    self.Debug(
                        f"[RECOVERY MODE] Fast recovery cache hit: rows={usable_rows} "
                        f"coverage={coverage:.2f}, skip History() backfill."
                    )
                    insights = self.alpha_model.generate_insights_on_schedule(self)
                    self.Debug(
                        f"[RECOVERY MODE] Fast path completed. "
                        f"{len(insights)} insights generated (not emitted)."
                    )
                    return

                self.Debug(
                    f"[RECOVERY MODE] Fast recovery cache miss: rows={usable_rows} "
                    f"coverage={coverage:.2f}, fallback to History() backfill."
                )

        backfill_plan = {
            "history_symbols": list(symbols or []),
            "history_start": pd.Timestamp(self.Time) - timedelta(days=history_span_days),
            "history_end": pd.Timestamp(self.Time),
            "requested_external_history": bool(symbols),
        }

        def _history_df(request_symbols, span=None, start=None, end=None):
            start_ts = datetime.utcnow()
            label = f"span={span}" if span is not None else f"start={start} end={end}"
            self.Debug(
                f"[RECOVERY MODE] History request start symbols={len(request_symbols)} "
                f"{label}"
            )
            self.Log(
                f"[RECOVERY MODE] History request start symbols={len(request_symbols)} "
                f"{label}"
            )
            if start is not None and end is not None:
                def _multi_fetch():
                    try:
                        return self.History(
                            TradeBar,
                            request_symbols,
                            start,
                            end,
                            Resolution.Minute,
                            fillForward=False,
                        )
                    except TypeError:
                        try:
                            return self.History(
                                TradeBar,
                                request_symbols,
                                start,
                                end,
                                Resolution.Minute,
                            )
                        except TypeError:
                            return self.History(
                                request_symbols,
                                start,
                                end,
                                Resolution.Minute,
                                fillForward=False,
                            )

                def _single_fetch(symbol):
                    try:
                        return self.History(
                            TradeBar,
                            [symbol],
                            start,
                            end,
                            Resolution.Minute,
                            fillForward=False,
                        )
                    except TypeError:
                        try:
                            return self.History(
                                TradeBar,
                                [symbol],
                                start,
                                end,
                                Resolution.Minute,
                            )
                        except TypeError:
                            return self.History(
                                [symbol],
                                start,
                                end,
                                Resolution.Minute,
                                fillForward=False,
                            )
            else:
                def _multi_fetch():
                    try:
                        return self.History(
                            TradeBar,
                            request_symbols,
                            span,
                            Resolution.Minute,
                            fillForward=False,
                        )
                    except TypeError:
                        try:
                            return self.History(
                                TradeBar,
                                request_symbols,
                                span,
                                Resolution.Minute,
                            )
                        except TypeError:
                            return self.History(
                                request_symbols,
                                span,
                                Resolution.Minute,
                                fillForward=False,
                            )

                def _single_fetch(symbol):
                    try:
                        return self.History(
                            TradeBar,
                            [symbol],
                            span,
                            Resolution.Minute,
                            fillForward=False,
                        )
                    except TypeError:
                        try:
                            return self.History(
                                TradeBar,
                                [symbol],
                                span,
                                Resolution.Minute,
                            )
                        except TypeError:
                            return self.History(
                                [symbol],
                                span,
                                Resolution.Minute,
                                fillForward=False,
                            )

            history, used_fallback = request_history_with_symbol_fallback(
                request_symbols,
                multi_fetcher=_multi_fetch,
                single_fetcher=_single_fetch,
                debug=self.Debug,
            )
            if history is None:
                self.Debug(
                    f"[RECOVERY MODE] History request returned None "
                    f"elapsed_ms={(datetime.utcnow() - start_ts).total_seconds() * 1000:.0f}"
                )
                self.Log(
                    f"[RECOVERY MODE] History request returned None "
                    f"elapsed_ms={(datetime.utcnow() - start_ts).total_seconds() * 1000:.0f}"
                )
                return None
            fallback_suffix = " via single-symbol fallback" if used_fallback else ""
            self.Debug(
                f"[RECOVERY MODE] History request returned DataFrame rows={len(history)}{fallback_suffix} "
                f"elapsed_ms={(datetime.utcnow() - start_ts).total_seconds() * 1000:.0f}"
            )
            self.Log(
                f"[RECOVERY MODE] History request returned DataFrame rows={len(history)}{fallback_suffix} "
                f"elapsed_ms={(datetime.utcnow() - start_ts).total_seconds() * 1000:.0f}"
            )
            return history

        if storage_context["storage_hit"]:
            try:
                insights = self.alpha_model.generate_insights_on_schedule(self)
                self.Debug(
                    f"[RECOVERY MODE] Storage-first fast path completed. "
                    f"{len(insights)} insights generated (not emitted)."
                )
                report_date = self.Time.date()
                try:
                    snapshot = getattr(self.alpha_model, "last_score_snapshot", None)
                    if isinstance(snapshot, dict):
                        snap_date = snapshot.get("date")
                        if snap_date:
                            report_date = pd.Timestamp(str(snap_date)).date()
                except Exception:
                    report_date = self.Time.date()

                self._persist_market_regime_debug_and_validate(
                    report_date,
                    used_storage_first=storage_context["used_storage_first"],
                    storage_hit=storage_context["storage_hit"],
                    requested_external_history=storage_context["requested_external_history"],
                )
                self._persist_structured_reports(current_date=report_date, source_mode="warmup_preview")
                self.Debug(f"[RECOVERY MODE] Structured market reports persisted for {report_date}.")
                if current_time >= market_finalize or bool(getattr(self, "_is_post_market", False)):
                    self._trigger_timing_preview_persistence(current_date=report_date)
                if self.cache_manager and (self.LiveMode or self.backtest_fast_recovery):
                    self._save_runtime_caches(overwrite_dates=[report_date])
                    self.Debug("[RECOVERY MODE] Saved caches to ObjectStore")
                return
            except Exception as e:
                self.Debug(f"[RECOVERY MODE] Error: {e}")
                import traceback
                self.Debug(traceback.format_exc())
                if self.LiveMode:
                    raise
                return

        if storage_context["used_storage_first"] and not storage_context["storage_hit"]:
            backfill_plan = self._build_storage_first_backfill_plan(
                symbols,
                lookback_days=lookback_days,
                history_span_days=history_span_days,
            )
            storage_context["requested_external_history"] = bool(backfill_plan["requested_external_history"])
            sample = [getattr(s, "Value", str(s)) for s in backfill_plan["history_symbols"][:8]]
            self.Debug(
                f"[RECOVERY MODE] Storage-first miss: backfill missing symbols/dates only "
                f"{len(backfill_plan['history_symbols'])}/{len(symbols)} "
                f"window={backfill_plan['history_start']} -> {backfill_plan['history_end']} "
                f"sample={sample}"
            )
            if not backfill_plan["requested_external_history"]:
                try:
                    insights = self.alpha_model.generate_insights_on_schedule(self)
                    self.Debug(
                        f"[RECOVERY MODE] Storage-first no-gap fast path completed. "
                        f"{len(insights)} insights generated (not emitted)."
                    )
                    report_date = self.Time.date()
                    try:
                        snapshot = getattr(self.alpha_model, "last_score_snapshot", None)
                        if isinstance(snapshot, dict):
                            snap_date = snapshot.get("date")
                            if snap_date:
                                report_date = pd.Timestamp(str(snap_date)).date()
                    except Exception:
                        report_date = self.Time.date()

                    self._persist_market_regime_debug_and_validate(
                        report_date,
                        used_storage_first=storage_context["used_storage_first"],
                        storage_hit=storage_context["storage_hit"],
                        requested_external_history=False,
                    )
                    self._persist_structured_reports(current_date=report_date, source_mode="warmup_preview")
                    self.Debug(f"[RECOVERY MODE] Structured market reports persisted for {report_date}.")
                    if current_time >= market_finalize or bool(getattr(self, "_is_post_market", False)):
                        self._trigger_timing_preview_persistence(current_date=report_date)
                    if self.cache_manager and (self.LiveMode or self.backtest_fast_recovery):
                        self._save_runtime_caches(overwrite_dates=[report_date])
                        self.Debug("[RECOVERY MODE] Saved caches to ObjectStore")
                    return
                except Exception as e:
                    self.Debug(f"[RECOVERY MODE] Error: {e}")
                    import traceback
                    self.Debug(traceback.format_exc())
                    if self.LiveMode:
                        raise
                    return

        history_symbols = list(backfill_plan["history_symbols"])
        history_start = pd.Timestamp(backfill_plan["history_start"])
        history_end = pd.Timestamp(backfill_plan["history_end"])
        effective_span_days = max(
            1,
            int(np.ceil(max(0.0, (history_end - history_start).total_seconds()) / 86400.0)),
        )
        if not storage_context["storage_hit"] and storage_context["requested_external_history"]:
            self.Debug(
                f"[RECOVERY MODE] Fetching minute history for backfill "
                f"(symbols={len(history_symbols)}, lookback_days={lookback_days}, "
                f"start={history_start}, end={history_end}, span_days={effective_span_days}, "
                f"factor_lb={factor_lb}, history_chunk_days={history_chunk_days}, "
                f"backfill_chunks={backfill_chunks})"
            )

        try:
            # Fetch minute history for backfill (160 calendar days ≈ 110 trading days)
            minute_history = None
            if history_chunk_days and history_chunk_days < effective_span_days:
                total_chunks = (effective_span_days + history_chunk_days - 1) // history_chunk_days
                chunk_dfs = []
                chunk_start = history_start
                chunk_index = 1
                while chunk_start < history_end:
                    chunk_end = min(chunk_start + timedelta(days=history_chunk_days), history_end)
                    self.Debug(
                        f"[RECOVERY MODE] History chunk {chunk_index}/{total_chunks}: "
                        f"{chunk_start} -> {chunk_end}"
                    )
                    chunk_df = _history_df(history_symbols, start=chunk_start, end=chunk_end)
                    if chunk_df is not None and not chunk_df.empty:
                        chunk_dfs.append(chunk_df)
                    chunk_start = chunk_end
                    chunk_index += 1
                if chunk_dfs:
                    minute_history = pd.concat(chunk_dfs)
                    if isinstance(minute_history.index, pd.MultiIndex):
                        minute_history = minute_history[~minute_history.index.duplicated(keep="first")]
                    elif "symbol" in minute_history.columns and "time" in minute_history.columns:
                        minute_history = minute_history.drop_duplicates(subset=["symbol", "time"])
                    self.Debug(
                        f"[RECOVERY MODE] History chunks combined rows={len(minute_history)}"
                    )
            else:
                minute_history = _history_df(history_symbols, start=history_start, end=history_end)

            if minute_history is None or minute_history.empty:
                self.Debug("[RECOVERY MODE] WARNING: No minute history available for backfill")
                if storage_context["used_storage_first"] and not storage_context["storage_hit"]:
                    # Storage may already contain enough raw OHLCV to rebuild derived factor state.
                    try:
                        insights = self.alpha_model.generate_insights_on_schedule(self)
                        self.Debug(
                            f"[RECOVERY MODE] Storage-first cache fallback completed after empty backfill. "
                            f"{len(insights)} insights generated (not emitted)."
                        )
                        report_date = self.Time.date()
                        try:
                            snapshot = getattr(self.alpha_model, "last_score_snapshot", None)
                            if isinstance(snapshot, dict):
                                snap_date = snapshot.get("date")
                                if snap_date:
                                    report_date = pd.Timestamp(str(snap_date)).date()
                        except Exception:
                            report_date = self.Time.date()

                        self._persist_market_regime_debug_and_validate(
                            report_date,
                            used_storage_first=storage_context["used_storage_first"],
                            storage_hit=storage_context["storage_hit"],
                            requested_external_history=storage_context["requested_external_history"],
                        )
                        self._persist_structured_reports(current_date=report_date, source_mode="warmup_preview")
                        self.Debug(f"[RECOVERY MODE] Structured market reports persisted for {report_date}.")
                        if current_time >= market_finalize or bool(getattr(self, "_is_post_market", False)):
                            self._trigger_timing_preview_persistence(current_date=report_date)
                        if self.cache_manager and (self.LiveMode or self.backtest_fast_recovery):
                            self._save_runtime_caches(overwrite_dates=[report_date])
                            self.Debug("[RECOVERY MODE] Saved caches to ObjectStore")
                        return
                    except Exception as e:
                        self.Debug(f"[RECOVERY MODE] Error: {e}")
                        import traceback
                        self.Debug(traceback.format_exc())
                        if self.LiveMode:
                            raise
                self._persist_market_regime_debug_and_validate(
                    self.Time.date(),
                    used_storage_first=storage_context["used_storage_first"],
                    storage_hit=storage_context["storage_hit"],
                    requested_external_history=storage_context["requested_external_history"],
                )
                return
            
            # Use feed_history_bars() which returns BOTH intraday features AND OHLCV
            # This uses the SAME per-bar accumulator as live data (single code path)
            self.Debug(
                f"[RECOVERY MODE] Backfill phase start rows={len(minute_history)} "
                f"lookback_days={lookback_days} chunks={backfill_chunks}"
            )
            backfill_start = datetime.utcnow()
            if backfill_chunks <= 1:
                backfilled, daily_results = self.intraday_consolidator.feed_history_bars(
                    minute_history,
                    lookback_days=lookback_days,
                    symbols=history_symbols,
                    log_daily_stats=not self.LiveMode,
                    log_summary=True,
                )
            else:
                df_all = self.intraday_consolidator._normalize_history_frame(
                    minute_history,
                    symbols_to_process=history_symbols,
                )
                if df_all.empty:
                    self.Debug("[RECOVERY MODE] Backfill split: failed to normalize history frame")
                    self._persist_market_regime_debug_and_validate(
                        self.Time.date(),
                        used_storage_first=storage_context["used_storage_first"],
                        storage_hit=storage_context["storage_hit"],
                        requested_external_history=storage_context["requested_external_history"],
                    )
                    return
                df_all["date"] = df_all["time"].dt.normalize()
                unique_dates = sorted(df_all["date"].unique())
                if not unique_dates:
                    self.Debug("[RECOVERY MODE] Backfill split: no dates found in history")
                    self._persist_market_regime_debug_and_validate(
                        self.Time.date(),
                        used_storage_first=storage_context["used_storage_first"],
                        storage_hit=storage_context["storage_hit"],
                        requested_external_history=storage_context["requested_external_history"],
                    )
                    return
                chunk_size = (len(unique_dates) + backfill_chunks - 1) // backfill_chunks
                backfilled = 0
                daily_results = {}
                for i in range(backfill_chunks):
                    chunk_dates = unique_dates[i * chunk_size:(i + 1) * chunk_size]
                    if not chunk_dates:
                        continue
                    chunk_df = df_all[df_all["date"].isin(chunk_dates)]
                    start_date = pd.Timestamp(chunk_dates[0]).date()
                    end_date = pd.Timestamp(chunk_dates[-1]).date()
                    self.Debug(
                        f"[RECOVERY MODE] Backfill chunk {i+1}/{backfill_chunks}: "
                        f"{start_date}..{end_date} rows={len(chunk_df)}"
                    )
                    chunk_start_ts = datetime.utcnow()
                    chunk_backfilled, chunk_results = self.intraday_consolidator.feed_history_bars(
                        chunk_df,
                        lookback_days=len(chunk_dates),
                        symbols=history_symbols,
                        log_daily_stats=False,
                        log_summary=True,
                    )
                    chunk_elapsed = (datetime.utcnow() - chunk_start_ts).total_seconds()
                    self.Debug(
                        f"[RECOVERY MODE] Backfill chunk {i+1} done: days={chunk_backfilled} "
                        f"elapsed={chunk_elapsed:.1f}s"
                    )
                    backfilled += chunk_backfilled
                    daily_results.update(chunk_results)
            total_elapsed = (datetime.utcnow() - backfill_start).total_seconds()
            self.Debug(
                f"[RECOVERY MODE] Backfill phase done days={backfilled} elapsed={total_elapsed:.1f}s"
            )
            self.Debug(f"[RECOVERY MODE] Backfilled {backfilled} days via feed_history_bars()")
            
            # Populate market_cache from the OHLCV results
            if daily_results:
                for timestamp, (ohlcv, intraday_df) in sorted(daily_results.items()):
                    # CRITICAL: Prevent appending future-dated snapshot created by backfill
                    # feed_history_bars stamps everything with 15:55. If that's today and we're pre-market, skip it.
                    is_today = timestamp.date() >= self.Time.date()
                    if is_today and (self.alpha_model.is_pre_market or current_time < market_finalize):
                        reason = "Pre-market" if self.alpha_model.is_pre_market else "Before 15:55"
                        self.Debug(f"[RECOVERY MODE] Skipping backfilled snapshot for today {timestamp} ({reason})")
                        continue
                        
                    if ohlcv.get("close"):
                        self.alpha_model.market_cache.append_snapshot(timestamp, ohlcv)
                
                cache_rows = len(self.alpha_model.market_cache.get("close"))
                last_date = self.alpha_model.market_cache.last_timestamp()
                self.Debug(f"[RECOVERY MODE] Market cache now has {cache_rows} rows, last={last_date}")

            retry_span_days = min(history_span_days, max(lookback_days, 60))
            retry_plan = self._build_storage_first_backfill_plan(
                symbols,
                lookback_days=lookback_days,
                history_span_days=retry_span_days,
            )
            missing_symbols = list(retry_plan["history_symbols"])
            if missing_symbols:
                sample = [getattr(s, "Value", str(s)) for s in missing_symbols[:8]]
                self.Debug(
                    f"[RECOVERY MODE] Missing history after backfill: "
                    f"{len(missing_symbols)}/{len(symbols)} "
                    f"window={retry_plan['history_start']} -> {retry_plan['history_end']} "
                    f"sample={sample}"
                )
                if len(missing_symbols) >= len(symbols):
                    self.Debug(
                        "[RECOVERY MODE] Retry plan still spans full symbol set; "
                        "skip duplicate broad request in the same recovery loop."
                    )
                else:
                    retry_history = _history_df(
                        missing_symbols,
                        start=pd.Timestamp(retry_plan["history_start"]),
                        end=pd.Timestamp(retry_plan["history_end"]),
                    )
                    if retry_history is None or retry_history.empty:
                        self.Debug("[RECOVERY MODE] Retry backfill: no history returned")
                    else:
                        retry_backfilled, retry_results = self.intraday_consolidator.feed_history_bars(
                            retry_history,
                            lookback_days=lookback_days,
                            symbols=missing_symbols,
                            log_daily_stats=False,
                            log_summary=True,
                        )
                        self.Debug(
                            f"[RECOVERY MODE] Retry backfilled {retry_backfilled} days "
                            f"for missing symbols"
                        )
                        for timestamp, (ohlcv, _) in sorted(retry_results.items()):
                            if ohlcv.get("close"):
                                self.alpha_model.market_cache.append_snapshot(timestamp, ohlcv)

            # If deploying during market hours, seed today's intraday bars for preview
            market_close = dt_time(16, 0)
            if market_open <= current_time <= market_close:
                try:
                    recent_history = _history_df(symbols, 1)
                    if recent_history is None or recent_history.empty:
                        self.Debug("[RECOVERY MODE] Seed intraday bars: no recent history")
                    elif self.intraday_consolidator is not None:
                        seeded = self.intraday_consolidator.seed_intraday_bars(
                            recent_history, target_date=self.Time.date()
                        )
                        if seeded > 0:
                            self.Debug(f"[RECOVERY MODE] Seeded {seeded} intraday bars for preview.")
                except Exception as e:
                    self.Debug(f"[RECOVERY MODE] Failed to seed intraday bars: {e}")
            
            # Run the full pipeline (defer flag is set so no insights emitted)
            # NOTE: For pre-market, _generate_insights will use cache-only, no today snapshot
            insights = self.alpha_model.generate_insights_on_schedule(self)
            self.Debug(f"[RECOVERY MODE] Completed. {len(insights)} insights generated (not emitted).")

            # Persist structured report artifacts immediately after preview run.
            # Prefer score snapshot date to avoid writing pre-market partial "today" rows.
            report_date = self.Time.date()
            try:
                snapshot = getattr(self.alpha_model, "last_score_snapshot", None)
                if isinstance(snapshot, dict):
                    snap_date = snapshot.get("date")
                    if snap_date:
                        report_date = pd.Timestamp(str(snap_date)).date()
            except Exception:
                report_date = self.Time.date()

            if report_date != self.Time.date():
                self.Debug(
                    f"[RECOVERY MODE] Overnight finalized-session replay: "
                    f"runtime_date={self.Time.date()} report_date={report_date}"
                )

            self._persist_market_regime_debug_and_validate(
                report_date,
                used_storage_first=storage_context["used_storage_first"],
                storage_hit=storage_context["storage_hit"],
                requested_external_history=storage_context["requested_external_history"],
            )
            self._persist_structured_reports(current_date=report_date, source_mode="warmup_preview")
            self.Debug(f"[RECOVERY MODE] Structured market reports persisted for {report_date}.")

            # timing_daily requires risk model evaluation (normally framework flow).
            # For post-close/after-finalize startup, trigger one preview evaluation here.
            if current_time >= market_finalize or bool(getattr(self, "_is_post_market", False)):
                self._trigger_timing_preview_persistence(current_date=report_date)
             
            # Save caches after recovery
            if self.cache_manager and (self.LiveMode or self.backtest_fast_recovery):
                self._save_runtime_caches(overwrite_dates=[report_date])
                self.Debug("[RECOVERY MODE] Saved caches to ObjectStore")
                
        except Exception as e:
            self.Debug(f"[RECOVERY MODE] Error: {e}")
            import traceback
            self.Debug(traceback.format_exc())
            if self.LiveMode:
                raise


def validate_snapshot(
    timestamp,
    ohlcv: dict,
    intraday_features: dict,
    universe_symbols: set,
    cache_last_date=None
):
    """
    Validate today's data before appending to cache.
    
    Args:
        timestamp: Snapshot timestamp
        ohlcv: Dict of {field: {symbol: value}}
        intraday_features: Dict of {symbol: {feature: value}}
        universe_symbols: Set of expected symbols
        cache_last_date: Last date in cache (for monotonicity check)
    
    Returns:
        Tuple of (is_valid: bool, error_message: str)
    """
    import pandas as pd
    nan_threshold = 0.05
    
    # 1. Column signature: snapshot symbols == universe symbols
    snapshot_symbols = set(ohlcv.get('close', {}).keys())
    if snapshot_symbols != universe_symbols:
        missing = universe_symbols - snapshot_symbols
        extra = snapshot_symbols - universe_symbols
        return False, f"Column mismatch: missing={len(missing)}, extra={len(extra)}"
    
    # 2. Time consistency: must be strictly after cache_last_date
    if hasattr(timestamp, 'date'):
        snapshot_date = timestamp.date()
    else:
        snapshot_date = timestamp
    
    if cache_last_date is not None:
        if snapshot_date <= cache_last_date:
            return False, f"Time not monotonic: snapshot={snapshot_date}, cache_last={cache_last_date}"
    
    # 3. NaN ratio check for OHLCV
    for field, data in ohlcv.items():
        if not data:
            continue
        nan_count = sum(1 for v in data.values() if pd.isna(v))
        nan_pct = nan_count / len(data) if data else 0
        if nan_pct > nan_threshold:
            return False, f"NaN threshold exceeded in {field}: {nan_pct:.1%}"
    
    # 4. Intraday feature sanity (allow more NaN here)
    total_features = len(intraday_features) * 8  # 8 features per symbol
    if total_features > 0:
        nan_count = sum(
            sum(1 for v in f.values() if pd.isna(v)) 
            for f in intraday_features.values() if isinstance(f, dict)
        )
        if nan_count > total_features * 0.5:
            return False, f"Too many NaN in intraday features: {nan_count}/{total_features}"
    
    return True, ""
