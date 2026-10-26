"""Counterfactual strategy and regret evaluator (Pillar 2 / ML-07).

Evaluates live and shadow trading decisions across the 20-strategy Iron Condor
incubator using:
  1. Counterfactual Action Regret: max_a R_t(a) - R_t(a_chosen)
  2. Cross-Grid Strategy Regret: max_s R_t(s) - R_t(s_chosen)
  3. MFE / MAE Capture Efficiency on closed trades
  4. Downside Sortino Ratio, Profit Factor, and 95% CVaR (Expected Shortfall)
  5. Market Regime Stratification (Low-Vol Pin, Mean-Reverting Chop,
     Directional Trend, High-Vol Expansion)
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any

from ml.features import finite_number

DEFAULT_TRAJECTORY_PATH = Path("logs/trajectories.jsonl")
VALID_POLICY_ACTIONS = ("HOLD", "ADD_POSITION", "TRIM_POSITION", "ADJUST_PROFIT_TARGET", "FLATTEN_NOW")


def classify_market_regime(market: Mapping[str, Any] | None) -> str:
    """Classify a 5-minute market feature snapshot into one of four canonical regimes."""
    if not isinstance(market, Mapping):
        return "Unknown"

    vix = finite_number(market.get("vix"))
    vix_term_ratio = finite_number(market.get("vix_term_ratio"))
    vwap_distance = finite_number(market.get("vwap_distance"))
    atr_pct = finite_number(market.get("atr_pct"))
    rsi_14 = finite_number(market.get("rsi_14"))
    velocity = finite_number(market.get("price_velocity_15m"))

    if all(val is None for val in (vix, vix_term_ratio, vwap_distance, atr_pct, rsi_14, velocity)):
        return "Unknown"

    # 1. High-Vol Expansion: elevated VIX, term-structure backwardation, or wide true range
    if (
        (vix is not None and vix >= 22.0)
        or (vix_term_ratio is not None and vix_term_ratio >= 1.05)
        or (atr_pct is not None and atr_pct >= 0.0025)
    ):
        return "High-Vol Expansion"

    # 2. Directional Trend: persistent move away from VWAP, rapid 15m velocity, or extreme RSI
    if (
        (vwap_distance is not None and abs(vwap_distance) >= 0.004)
        or (velocity is not None and abs(velocity) >= 0.003)
        or (rsi_14 is not None and (rsi_14 >= 70.0 or rsi_14 <= 30.0))
    ):
        return "Directional Trend"

    # 3. Low-Vol Pin: subdued VIX and price tightly anchored near session VWAP
    if (
        vix is not None
        and vix < 16.0
        and (vwap_distance is None or abs(vwap_distance) <= 0.0015)
    ):
        return "Low-Vol Pin"

    # 4. Default balanced intraday state
    return "Mean-Reverting Chop"


def compute_sortino_and_tail_metrics(
    returns: Sequence[float],
    target: float = 0.0,
) -> dict[str, float | None]:
    """Compute Downside Sortino Ratio, Profit Factor, Win Rate, and 95% CVaR."""
    clean = [val for item in returns if (val := finite_number(item)) is not None]
    if not clean:
        return {
            "count": 0,
            "mean_return": None,
            "sortino_ratio": None,
            "profit_factor": None,
            "cvar_95": None,
            "win_rate": None,
        }

    n = len(clean)
    mean_ret = sum(clean) / n
    downside_sq = [(min(0.0, r - target)) ** 2 for r in clean]
    downside_dev = math.sqrt(sum(downside_sq) / n)
    sortino = (mean_ret - target) / downside_dev if downside_dev > 1e-9 else None

    gains = sum(r for r in clean if r > 0)
    losses = abs(sum(r for r in clean if r < 0))
    profit_factor = (gains / losses) if losses > 1e-9 else (None if gains <= 0 else float("inf"))
    if profit_factor is not None and not math.isfinite(profit_factor):
        profit_factor = None

    sorted_rets = sorted(clean)
    tail_count = max(1, math.ceil(0.05 * n))
    cvar_95 = sum(sorted_rets[:tail_count]) / tail_count
    wins = sum(1 for r in clean if r > 0)

    return {
        "count": n,
        "mean_return": mean_ret,
        "sortino_ratio": sortino,
        "profit_factor": profit_factor,
        "cvar_95": cvar_95,
        "win_rate": wins / n,
    }


def compute_mfe_mae_efficiency(
    trades: Iterable[Mapping[str, Any]],
) -> dict[str, float | int | None]:
    """Compute MFE capture efficiency and MAE tail-loss avoidance across closed trades."""
    closed = [
        t for t in trades
        if isinstance(t, Mapping) and str(t.get("status", "CLOSED")).upper() == "CLOSED"
    ]
    capture_ratios: list[float] = []
    avoidance_ratios: list[float] = []
    mfes: list[float] = []
    maes: list[float] = []

    for trade in closed:
        pnl = finite_number(trade.get("pnl"))
        mfe = finite_number(trade.get("mfe_pnl"))
        mae = finite_number(trade.get("mae_pnl"))
        max_risk = finite_number(trade.get("max_risk"))

        if mfe is not None:
            mfes.append(mfe)
            if pnl is not None and mfe > 1e-6:
                capture_ratios.append(max(-1.0, min(1.5, pnl / mfe)))

        if mae is not None:
            maes.append(mae)
            if pnl is not None and max_risk is not None and max_risk > 0 and mae < 0:
                realized_loss = abs(min(pnl, 0.0))
                avoidance_ratios.append(max(0.0, min(1.0, 1.0 - realized_loss / max_risk)))

    return {
        "evaluated_trades": len(closed),
        "mfe_capture_ratio": (sum(capture_ratios) / len(capture_ratios)) if capture_ratios else None,
        "mae_avoidance_ratio": (sum(avoidance_ratios) / len(avoidance_ratios)) if avoidance_ratios else None,
        "avg_mfe_pnl": (sum(mfes) / len(mfes)) if mfes else None,
        "avg_mae_pnl": (sum(maes) / len(maes)) if maes else None,
    }


def _normalize_comparable_action(action: str | None, scores: Mapping[str, float]) -> str:
    """Map an executed strategy action to the closest scored counterfactual action."""
    if action in scores:
        return str(action)
    if action in ("ENTER", "ADJUST_PROFIT_TARGET", None) and "HOLD" in scores:
        return "HOLD"
    return "HOLD" if "HOLD" in scores else next(iter(scores))


def evaluate_counterfactual_regret(
    log_path: Path = DEFAULT_TRAJECTORY_PATH,
    session: str | None = None,
) -> dict[str, Any]:
    """Join SNAPSHOT and HINDSIGHT_LABEL trajectory records to compute counterfactual regret."""
    path = Path(log_path)
    if not path.exists():
        return {
            "labeled_ticks": 0,
            "evaluated_decisions": 0,
            "mean_action_regret": None,
            "median_action_regret": None,
            "zero_regret_rate": None,
            "mean_grid_regret": None,
            "optimal_action_counts": {},
            "chosen_action_counts": {},
            "by_strategy": {},
            "by_regime": {},
        }

    snapshots: dict[str, dict[str, Any]] = {}
    labels: list[dict[str, Any]] = []

    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("schema_version") != 1:
                continue
            if session is not None and row.get("session") != session:
                continue
            event = row.get("event")
            if event == "SNAPSHOT" and "record_id" in row:
                snapshots[str(row["record_id"])] = row
            elif event == "HINDSIGHT_LABEL" and "source_id" in row:
                labels.append(row)

    all_regrets: list[float] = []
    grid_regrets: list[float] = []
    zero_regret_hits = 0
    optimal_counts: Counter[str] = Counter()
    chosen_counts: Counter[str] = Counter()

    strat_regrets: dict[str, list[float]] = defaultdict(list)
    strat_zero_hits: Counter[str] = Counter()
    strat_future_pnls: dict[str, list[float]] = defaultdict(list)
    strat_maes: dict[str, list[float]] = defaultdict(list)

    regime_regrets: dict[str, list[float]] = defaultdict(list)
    regime_returns: dict[str, list[float]] = defaultdict(list)
    regime_optimal_actions: dict[str, Counter[str]] = defaultdict(Counter)

    labeled_ticks = 0
    for label_row in labels:
        source_id = str(label_row.get("source_id", ""))
        snap = snapshots.get(source_id)
        if snap is None:
            continue
        strat_labels = label_row.get("strategies") or {}
        if not strat_labels:
            continue

        labeled_ticks += 1
        regime = classify_market_regime(snap.get("market"))
        snap_strategies = snap.get("strategies") or {}

        # Best possible score across the entire 20-strategy grid on this tick
        best_grid_score: float | None = None
        for s_label in strat_labels.values():
            scores = s_label.get("scores") or {}
            for val in scores.values():
                num = finite_number(val)
                if num is not None and (best_grid_score is None or num > best_grid_score):
                    best_grid_score = num

        for name, s_label in strat_labels.items():
            scores_raw = s_label.get("scores") or {}
            scores = {
                k: num for k, v in scores_raw.items()
                if (num := finite_number(v)) is not None
            }
            if not scores:
                continue

            optimal_action = str(s_label.get("action") or max(scores, key=scores.get))
            best_score = max(scores.values())

            snap_state = snap_strategies.get(name) or {}
            raw_action = snap_state.get("action")
            chosen_action = _normalize_comparable_action(raw_action, scores)
            chosen_score = scores.get(chosen_action, best_score)

            regret = max(0.0, best_score - chosen_score)
            all_regrets.append(regret)
            strat_regrets[name].append(regret)
            regime_regrets[regime].append(regret)
            regime_returns[regime].append(chosen_score)

            if regret <= 1e-9 or chosen_action == optimal_action:
                zero_regret_hits += 1
                strat_zero_hits[name] += 1

            optimal_counts[optimal_action] += 1
            chosen_counts[chosen_action] += 1
            regime_optimal_actions[regime][optimal_action] += 1

            if best_grid_score is not None:
                grid_regrets.append(max(0.0, best_grid_score - chosen_score))

            fut_pnl = finite_number(s_label.get("future_pnl"))
            if fut_pnl is not None:
                strat_future_pnls[name].append(fut_pnl)
            mae_val = finite_number(s_label.get("max_adverse_excursion"))
            if mae_val is not None:
                strat_maes[name].append(mae_val)

    total_decisions = len(all_regrets)
    by_strategy: dict[str, Any] = {}
    for name, regrets in sorted(strat_regrets.items()):
        count = len(regrets)
        fut = strat_future_pnls[name]
        maes = strat_maes[name]
        by_strategy[name] = {
            "decisions": count,
            "mean_regret": sum(regrets) / count if count else None,
            "optimal_action_rate": strat_zero_hits[name] / count if count else None,
            "avg_future_pnl": sum(fut) / len(fut) if fut else None,
            "avg_adverse_excursion": sum(maes) / len(maes) if maes else None,
        }

    by_regime: dict[str, Any] = {}
    for regime, regrets in sorted(regime_regrets.items()):
        count = len(regrets)
        tail = compute_sortino_and_tail_metrics(regime_returns[regime])
        by_regime[regime] = {
            "decisions": count,
            "mean_regret": sum(regrets) / count if count else None,
            "sortino_ratio": tail["sortino_ratio"],
            "optimal_actions": dict(regime_optimal_actions[regime]),
        }

    return {
        "labeled_ticks": labeled_ticks,
        "evaluated_decisions": total_decisions,
        "mean_action_regret": (sum(all_regrets) / total_decisions) if total_decisions else None,
        "median_action_regret": median(all_regrets) if total_decisions else None,
        "zero_regret_rate": (zero_regret_hits / total_decisions) if total_decisions else None,
        "mean_grid_regret": (sum(grid_regrets) / len(grid_regrets)) if grid_regrets else None,
        "optimal_action_counts": dict(optimal_counts),
        "chosen_action_counts": dict(chosen_counts),
        "by_strategy": by_strategy,
        "by_regime": by_regime,
    }
