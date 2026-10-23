#!/usr/bin/env python3
"""
QuickScalp Auto-Optimizer (safe incremental fine-tuner)
=======================================================
Läuft parallel zum Trading-Bot. Ändert NIEMALS config.json.

Trigger (Hybrid + Smart):
  - Poll alle ~5 Min (Default), bei Streak/Distress alle ~2 Min
  - Normal: genug neue Trades + Mindestpause
  - Distress: schlechte WR / Ø-Profit → früher
  - Lose-Streak (Default 3 Verluste in Folge) → SOFORT Gemini-Fast-Pfad
    (Analyse letzter Trades + defensive Nudges, ohne langes Hyperopt)
  - Settled/Plateau mit Wake bei WR-Einbruch

Ablauf Normal-Zyklus: Live → Gemini → Hyperopt → Clamp → Hold-out → reload
Ablauf Streak-Zyklus: Live → Gemini-Streak → Heuristik-Defense → Clamp → reload

Schreibt NUR QuickScalpStrategy.json – nie config.json und nie strategy.py.
Gemini darf Code/Strategie-Dateien nicht ändern.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# ----------------------------------------------------------------------
# KONFIGURATION
# ----------------------------------------------------------------------

FREQTRADE_API_URL = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080").rstrip("/")
FREQTRADE_API_USER = os.environ.get("FREQTRADE_API_USER", os.environ.get("API_USERNAME", "admin"))
FREQTRADE_API_PASS = os.environ.get("FREQTRADE_API_PASS", os.environ.get("API_PASSWORD", ""))

CONFIG_PATH = Path(os.environ.get("FREQTRADE_CONFIG", "/freqtrade/user_data/config.json"))
STRATEGY_NAME = os.environ.get("STRATEGY_NAME", "QuickScalpStrategy")
STRATEGY_DIR = Path(os.environ.get("STRATEGY_DIR", "/freqtrade/user_data/strategies"))
STRATEGY_PARAMS_FILE = STRATEGY_DIR / f"{STRATEGY_NAME}.json"
PARAMS_BACKUP = STRATEGY_DIR / f"{STRATEGY_NAME}.json.bak"

OPTIMIZER_DIR = Path(os.environ.get("OPTIMIZER_DIR", "/freqtrade/user_data/optimizer"))
STATE_FILE = OPTIMIZER_DIR / "state.json"
AUDIT_LOG = OPTIMIZER_DIR / "audit_log.jsonl"
REPORTS_DIR = OPTIMIZER_DIR / "reports"
LOCK_FILE = OPTIMIZER_DIR / "optimizer.lock"
BACKTEST_RESULTS = Path("/freqtrade/user_data/backtest_results")

# Hybrid-Trigger
TRADE_THRESHOLD = int(os.environ.get("TRADE_THRESHOLD", "12") or "12")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300") or "300")  # 5 Min
POLL_FAST_SECONDS = int(os.environ.get("POLL_FAST_SECONDS", "120") or "120")  # bei Streak/Distress
MIN_HOURS_BETWEEN_RUNS = float(os.environ.get("MIN_HOURS_BETWEEN_RUNS", "4") or "4")
SETTLED_HOURS_BETWEEN_RUNS = float(os.environ.get("SETTLED_HOURS_BETWEEN_RUNS", "12") or "12")
# Bei klar schlechter Live-Phase früher nachjustieren
DISTRESS_MIN_HOURS = float(os.environ.get("DISTRESS_MIN_HOURS", "1.0") or "1.0")
DISTRESS_TRADE_THRESHOLD = int(os.environ.get("DISTRESS_TRADE_THRESHOLD", "6") or "6")
DISTRESS_WINRATE = float(os.environ.get("DISTRESS_WINRATE", "0.40") or "0.40")
DISTRESS_AVG_PROFIT = float(
    os.environ.get("DISTRESS_AVG_PROFIT")
    or os.environ.get("OPTIMIZER_DISTRESS_AVG_PROFIT")
    or "-0.003"
)
# Losing-Streak → Gemini-Fast (ohne Hyperopt)
LOSE_STREAK_TRIGGER = int(os.environ.get("LOSE_STREAK_TRIGGER", "3") or "3")
STREAK_MIN_HOURS = float(os.environ.get("STREAK_MIN_HOURS", "0.25") or "0.25")  # 15 Min
STREAK_SKIP_HYPEROPT = os.environ.get("STREAK_SKIP_HYPEROPT", "true").lower() in (
    "1",
    "true",
    "yes",
)
PLATEAU_KEEP_STREAK = int(os.environ.get("PLATEAU_KEEP_STREAK", "3") or "3")
LIVE_TRADE_LOOKBACK = int(os.environ.get("LIVE_TRADE_LOOKBACK", "50") or "50")
WAKE_WINRATE_DROP = float(os.environ.get("WAKE_WINRATE_DROP", "0.10") or "0.10")
# Gemini auch bei jedem normalen Zyklus (bereits so); Streak erzwingt Analyse
GEMINI_ON_STREAK = os.environ.get("GEMINI_ON_STREAK", "true").lower() in ("1", "true", "yes")

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "45") or "45")
HOLDOUT_DAYS = int(os.environ.get("HOLDOUT_DAYS", "7") or "7")
DATA_BUFFER_DAYS = int(os.environ.get("DATA_BUFFER_DAYS", "3") or "3")

HYPEROPT_EPOCHS = int(os.environ.get("HYPEROPT_EPOCHS", "80") or "80")
HYPEROPT_LOSS = os.environ.get("HYPEROPT_LOSS", "SharpeHyperOptLoss") or "SharpeHyperOptLoss"
HYPEROPT_JOBS = int(os.environ.get("HYPEROPT_JOBS", "2") or "2")  # NAS-freundlich

MIN_HOLDOUT_TRADES = int(os.environ.get("MIN_HOLDOUT_TRADES", "8") or "8")
IMPROVEMENT_THRESHOLD = float(os.environ.get("IMPROVEMENT_THRESHOLD", "0.03") or "0.03")
MAX_DD_RATIO = float(os.environ.get("MAX_DD_RATIO", "1.15") or "1.15")

LLM_API_URL = os.environ.get("LLM_API_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or (
    LLM_API_KEY if "gemini" in LLM_MODEL.lower() else ""
)
# Gemini als Analyst + Param-Berater (nicht als Code-Autor)
GEMINI_ADVISOR = os.environ.get("GEMINI_ADVISOR", "true").lower() in ("1", "true", "yes")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RUN_ON_START = os.environ.get("OPTIMIZER_RUN_ON_START", "false").lower() in ("1", "true", "yes")

# Absolute Bounds + max. Nudge pro Lauf (kleine Stellschrauben)
# Muss zur Strategie passen.
PARAM_BOUNDS: dict[str, dict[str, dict[str, Any]]] = {
    "buy": {
        "ema_fast_length": {"type": "int", "min": 5, "max": 12, "max_delta": 2},
        "ema_slow_length": {"type": "int", "min": 14, "max": 30, "max_delta": 3},
        "rsi_length": {"type": "int", "min": 6, "max": 14, "max_delta": 2},
        "rsi_buy_floor": {"type": "int", "min": 35, "max": 50, "max_delta": 3},
        "rsi_buy_threshold": {"type": "int", "min": 50, "max": 65, "max_delta": 3},
        "volume_factor": {"type": "float", "min": 0.8, "max": 2.0, "max_delta": 0.15, "decimals": 2},
    },
    "sell": {
        "rsi_sell_threshold": {"type": "int", "min": 70, "max": 90, "max_delta": 3},
        "atr_multiplier": {"type": "float", "min": 1.2, "max": 3.0, "max_delta": 0.25, "decimals": 2},
        "quick_tp": {"type": "float", "min": 0.0028, "max": 0.006, "max_delta": 0.0006, "decimals": 4},
    },
}

DEFAULT_PARAMS: dict[str, Any] = {
    "strategy_name": STRATEGY_NAME,
    "params": {
        "buy": {
            "ema_fast_length": 8,
            "ema_slow_length": 21,
            "rsi_length": 10,
            "rsi_buy_floor": 42,
            "rsi_buy_threshold": 56,
            "volume_factor": 1.15,
        },
        "sell": {
            "rsi_sell_threshold": 78,
            "atr_multiplier": 2.0,
            "quick_tp": 0.0038,
        },
    },
}

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

OPTIMIZER_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(OPTIMIZER_DIR / "optimizer.log"),
    ],
)
log = logging.getLogger("quickscalp-optimizer")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------
# STATE / LOCK
# ----------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "last_processed_closed_trades": 0,
        "last_run_timestamp": None,
        "runs": 0,
        "keep_streak": 0,
        "settled": False,
        "last_accepted_live_winrate": None,
        "consecutive_failures": 0,
    }


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_FILE)


def append_audit(record: dict) -> None:
    record["timestamp"] = utcnow().isoformat() + "Z"
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class CycleLock:
    """Verhindert parallele Optimierungs-Zyklen."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self.fh.close()
            self.fh = None
            raise RuntimeError("Anderer Optimierungs-Zyklus läuft bereits") from e
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *args):
        if self.fh:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


# ----------------------------------------------------------------------
# FREQTRADE REST API
# ----------------------------------------------------------------------

def ft_api(method: str, path: str, timeout: int = 60, **kwargs) -> Any:
    url = f"{FREQTRADE_API_URL}{path}"
    resp = requests.request(
        method,
        url,
        auth=(FREQTRADE_API_USER, FREQTRADE_API_PASS),
        timeout=timeout,
        **kwargs,
    )
    resp.raise_for_status()
    if not resp.content:
        return {}
    return resp.json()


def wait_for_api(max_wait: int = 300) -> None:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            ft_api("GET", "/api/v1/ping")
            log.info("Freqtrade API erreichbar unter %s", FREQTRADE_API_URL)
            return
        except Exception as e:
            log.info("Warte auf Freqtrade API… (%s)", e)
            time.sleep(10)
    raise RuntimeError(f"Freqtrade API nicht erreichbar: {FREQTRADE_API_URL}")


def get_closed_trade_count() -> int:
    data = ft_api("GET", "/api/v1/profit")
    return int(data.get("closed_trade_count", data.get("trade_count", 0)))


def reload_bot_config() -> None:
    ft_api("POST", "/api/v1/reload_config")
    log.info("Bot per /reload_config neu geladen.")


def fetch_recent_trades(limit: int = LIVE_TRADE_LOOKBACK) -> list[dict]:
    """Geschlossene Trades für Muster-Analyse (API)."""
    try:
        data = ft_api("GET", f"/api/v1/trades?limit={limit}")
    except Exception:
        # ältere API-Variante
        data = ft_api("GET", "/api/v1/trades", params={"limit": limit})

    if isinstance(data, dict):
        trades = data.get("trades") or data.get("data") or []
    elif isinstance(data, list):
        trades = data
    else:
        trades = []

    closed = [t for t in trades if t.get("is_open") is False or t.get("close_date") or t.get("close_timestamp")]
    return closed[-limit:]


def _trade_profit(t: dict) -> float | None:
    p = t.get("profit_ratio")
    if p is None:
        p = t.get("close_profit")
    if p is None:
        return None
    return float(p)


def _trade_close_key(t: dict) -> str:
    return str(t.get("close_date") or t.get("close_timestamp") or t.get("open_date") or "")


def compute_lose_streak(trades: list[dict]) -> tuple[int, list[dict]]:
    """Anzahl Verluste am Ende der Timeline + die letzten Verlust-Trades."""
    closed = [t for t in trades if _trade_profit(t) is not None]
    closed.sort(key=_trade_close_key)
    streak = 0
    losers: list[dict] = []
    for t in reversed(closed):
        p = _trade_profit(t)
        if p is not None and p <= 0:
            streak += 1
            losers.append(t)
        else:
            break
    return streak, list(reversed(losers))


def analyze_live_trades(trades: list[dict]) -> dict:
    """Leitet aus Live-Trades ab, welche Spaces sinnvoll sind (+ Lose-Streak)."""
    if not trades:
        return {
            "count": 0,
            "winrate": None,
            "avg_profit": None,
            "lose_streak": 0,
            "spaces": ["buy", "sell"],
            "hints": ["keine Live-Trades – beide Spaces"],
        }

    profits = []
    exit_reasons: dict[str, int] = {}
    for t in trades:
        p = _trade_profit(t)
        if p is None:
            continue
        profits.append(p)
        reason = str(t.get("exit_reason") or t.get("sell_reason") or "unknown")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    lose_streak, streak_losers = compute_lose_streak(trades)

    if not profits:
        return {
            "count": len(trades),
            "winrate": None,
            "avg_profit": None,
            "lose_streak": lose_streak,
            "spaces": ["buy", "sell"],
            "hints": ["keine Profit-Daten"],
        }

    wins = sum(1 for p in profits if p > 0)
    winrate = wins / len(profits)
    avg_profit = sum(profits) / len(profits)
    stop_hits = sum(v for k, v in exit_reasons.items() if "stop" in k.lower())
    roi_exits = sum(v for k, v in exit_reasons.items() if "roi" in k.lower())
    signal_exits = sum(
        v for k, v in exit_reasons.items() if "exit" in k.lower() or "signal" in k.lower()
    )

    spaces = ["buy", "sell"]
    hints = []

    if lose_streak >= LOSE_STREAK_TRIGGER:
        spaces = ["buy", "sell"]
        hints.append(f"lose-streak={lose_streak} → Entry strenger + früheres TP")
    elif winrate < 0.45 and stop_hits >= max(2, len(profits) // 4):
        spaces = ["buy", "sell"]
        hints.append("viele Stop-Verluste → Entry-Filter + ATR feinjustieren")
    elif winrate < 0.45:
        spaces = ["buy"]
        hints.append("niedrige Winrate → Entry (buy) priorisieren")
    elif avg_profit < 0 and roi_exits + signal_exits > stop_hits:
        spaces = ["sell"]
        hints.append("Exits lassen Profit liegen → sell-Space priorisieren")
    else:
        hints.append("ausgewogen → buy+sell Feinschliff")

    return {
        "count": len(profits),
        "winrate": winrate,
        "avg_profit": avg_profit,
        "lose_streak": lose_streak,
        "streak_losers": [
            {
                "pair": t.get("pair"),
                "profit_ratio": _trade_profit(t),
                "exit_reason": t.get("exit_reason") or t.get("sell_reason"),
                "enter_tag": t.get("enter_tag") or t.get("buy_tag"),
            }
            for t in streak_losers
        ],
        "exit_reasons": exit_reasons,
        "spaces": spaces,
        "hints": hints,
    }


# ----------------------------------------------------------------------
# PARAMETER: Defaults, Clamp, Validate, Atomic Write
# ----------------------------------------------------------------------

def ensure_params_file() -> dict:
    if not STRATEGY_PARAMS_FILE.exists():
        write_params_atomic(DEFAULT_PARAMS)
        log.info("Default-Parameter geschrieben: %s", STRATEGY_PARAMS_FILE)
    return read_current_params()


def read_current_params() -> dict:
    if STRATEGY_PARAMS_FILE.exists():
        data = json.loads(STRATEGY_PARAMS_FILE.read_text())
        if "params" not in data:
            data = {"strategy_name": STRATEGY_NAME, "params": data}
        return data
    return json.loads(json.dumps(DEFAULT_PARAMS))


def write_params_atomic(payload: dict) -> None:
    """Atomic write – nur Strategy-JSON, nie config.json."""
    if STRATEGY_PARAMS_FILE.exists():
        shutil.copy2(STRATEGY_PARAMS_FILE, PARAMS_BACKUP)

    clean = {
        "strategy_name": STRATEGY_NAME,
        "params": {
            "buy": dict(payload.get("params", payload).get("buy", {})),
            "sell": dict(payload.get("params", payload).get("sell", {})),
        },
    }
    # Hard-Ban: keine roi/stoploss/trailing aus Hyperopt übernehmen
    tmp = STRATEGY_PARAMS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n")
    tmp.replace(STRATEGY_PARAMS_FILE)
    log.info("Parameter atomar geschrieben: %s", STRATEGY_PARAMS_FILE)


def restore_backup_or(params: dict) -> None:
    if PARAMS_BACKUP.exists():
        shutil.copy2(PARAMS_BACKUP, STRATEGY_PARAMS_FILE)
        log.info("Backup wiederhergestellt: %s", PARAMS_BACKUP)
    else:
        write_params_atomic(params)


def _clamp_value(current: Any, proposed: Any, meta: dict) -> Any:
    typ = meta["type"]
    lo, hi = meta["min"], meta["max"]
    max_delta = meta["max_delta"]

    if current is None:
        current = proposed
    if typ == "int":
        cur = int(current)
        prop = int(round(float(proposed)))
        nudged = max(cur - max_delta, min(cur + max_delta, prop))
        return int(max(lo, min(hi, nudged)))

    decimals = int(meta.get("decimals", 2))
    cur = float(current)
    prop = float(proposed)
    nudged = max(cur - max_delta, min(cur + max_delta, prop))
    nudged = max(lo, min(hi, nudged))
    return round(nudged, decimals)


def apply_incremental_nudge(current: dict, proposed: dict) -> tuple[dict, list[str]]:
    """Begrenzt Änderungen auf kleine max_delta-Schritte; strippt gefährliche Keys."""
    cur_p = current.get("params", current)
    prop_p = proposed.get("params", proposed)
    out_buy: dict[str, Any] = {}
    out_sell: dict[str, Any] = {}
    changes: list[str] = []

    for space, out in (("buy", out_buy), ("sell", out_sell)):
        bounds = PARAM_BOUNDS[space]
        cur_space = cur_p.get(space, {})
        prop_space = prop_p.get(space, {})
        for key, meta in bounds.items():
            cur_val = cur_space.get(key, DEFAULT_PARAMS["params"][space].get(key))
            if key in prop_space:
                new_val = _clamp_value(cur_val, prop_space[key], meta)
            else:
                new_val = cur_val
            out[key] = new_val
            if new_val != cur_val:
                changes.append(f"{space}.{key}: {cur_val} → {new_val}")

    result = {"strategy_name": STRATEGY_NAME, "params": {"buy": out_buy, "sell": out_sell}}
    result = validate_and_fix_constraints(result)
    return result, changes


def validate_and_fix_constraints(payload: dict) -> dict:
    """EMA/RSI-Invarianten erzwingen."""
    buy = payload["params"]["buy"]
    sell = payload["params"]["sell"]

    fast = int(buy["ema_fast_length"])
    slow = int(buy["ema_slow_length"])
    if slow < fast + 5:
        buy["ema_slow_length"] = min(30, fast + 5)
        log.info("Constraint: ema_slow auf %s angehoben", buy["ema_slow_length"])

    floor = int(buy.get("rsi_buy_floor", 40))
    rsi_buy = int(buy["rsi_buy_threshold"])
    if floor >= rsi_buy - 3:
        buy["rsi_buy_floor"] = max(35, rsi_buy - 8)
        log.info("Constraint: rsi_buy_floor auf %s gesenkt", buy["rsi_buy_floor"])

    rsi_sell = int(sell["rsi_sell_threshold"])
    if rsi_sell <= rsi_buy + 5:
        sell["rsi_sell_threshold"] = min(90, rsi_buy + 12)
        log.info("Constraint: rsi_sell auf %s angehoben", sell["rsi_sell_threshold"])

    # Nochmal in Bounds
    for space, block in (("buy", buy), ("sell", sell)):
        for key, meta in PARAM_BOUNDS[space].items():
            if key not in block:
                continue
            v = block[key]
            if meta["type"] == "int":
                block[key] = int(max(meta["min"], min(meta["max"], int(v))))
            else:
                block[key] = round(
                    float(max(meta["min"], min(meta["max"], float(v)))),
                    int(meta.get("decimals", 2)),
                )

    return payload


def params_equal(a: dict, b: dict) -> bool:
    return json.dumps(a.get("params", a), sort_keys=True) == json.dumps(b.get("params", b), sort_keys=True)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def run_cli(args: list[str], timeout: int = 7200) -> subprocess.CompletedProcess:
    cmd = ["freqtrade", *args]
    log.info("CLI: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        log.warning(
            "CLI Exit %s\n--- stderr (tail) ---\n%s\n--- stdout (tail) ---\n%s",
            result.returncode,
            (result.stderr or "")[-2500:],
            (result.stdout or "")[-2500:],
        )
    return result


def download_fresh_data(timeframe: str) -> None:
    days = LOOKBACK_DAYS + HOLDOUT_DAYS + DATA_BUFFER_DAYS
    run_cli(
        [
            "download-data",
            "--config",
            str(CONFIG_PATH),
            "--timeframe",
            timeframe,
            "--days",
            str(days),
        ],
        timeout=1800,
    )


def run_hyperopt(timeframe: str, spaces: list[str]) -> bool:
    train_end = utcnow() - timedelta(days=HOLDOUT_DAYS)
    train_start = train_end - timedelta(days=LOOKBACK_DAYS)
    timerange = f"{train_start:%Y%m%d}-{train_end:%Y%m%d}"
    log.info("Hyperopt %s Epochs, Spaces=%s, Fenster=%s", HYPEROPT_EPOCHS, spaces, timerange)

    args = [
        "hyperopt",
        "--config",
        str(CONFIG_PATH),
        "--strategy",
        STRATEGY_NAME,
        "--hyperopt-loss",
        HYPEROPT_LOSS,
        "--spaces",
        *spaces,
        "--timeframe",
        timeframe,
        "-e",
        str(HYPEROPT_EPOCHS),
        "--timerange",
        timerange,
        "--random-state",
        str(int(time.time()) % 10_000),
        "-j",
        str(HYPEROPT_JOBS),
    ]
    result = run_cli(args, timeout=10800)
    return result.returncode == 0


def extract_best_hyperopt_params() -> dict | None:
    result = run_cli(
        ["hyperopt-show", "--best", "-n", "1", "--print-json", "--no-header"],
        timeout=120,
    )
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        log.error("Kein JSON in hyperopt-show.")
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log.error("JSON parse error: %s", e)
        return None

    if "params" in parsed:
        params = parsed["params"]
    elif any(k in parsed for k in ("buy", "sell")):
        params = {k: parsed[k] for k in ("buy", "sell") if k in parsed}
    else:
        log.error("Unerwartetes Format: %s", list(parsed.keys()))
        return None

    # Nur buy/sell behalten
    return {
        "strategy_name": STRATEGY_NAME,
        "params": {
            "buy": dict(params.get("buy") or {}),
            "sell": dict(params.get("sell") or {}),
        },
    }


def _latest_backtest_file() -> Path | None:
    pointer = BACKTEST_RESULTS / ".last_result.json"
    if pointer.exists():
        latest = json.loads(pointer.read_text()).get("latest_backtest")
        if latest:
            path = BACKTEST_RESULTS / latest
            if path.exists():
                return path
    candidates = sorted(
        [p for p in BACKTEST_RESULTS.glob("backtest-result-*.json") if ".meta." not in p.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def run_backtest(timerange: str, timeframe: str) -> dict | None:
    result = run_cli(
        [
            "backtesting",
            "--config",
            str(CONFIG_PATH),
            "--strategy",
            STRATEGY_NAME,
            "--timeframe",
            timeframe,
            "--timerange",
            timerange,
            "--export",
            "trades",
            "--breakdown",
            "day",
        ],
        timeout=3600,
    )
    if result.returncode != 0:
        return None

    result_file = _latest_backtest_file()
    if not result_file:
        log.error("Kein Backtest-Ergebnis.")
        return None

    full = json.loads(result_file.read_text())
    strat = full.get("strategy", {}).get(STRATEGY_NAME)
    if not isinstance(strat, dict):
        if isinstance(full.get("strategy"), dict) and len(full["strategy"]) == 1:
            strat = next(iter(full["strategy"].values()))
        else:
            log.error("Strategie-Stats fehlen.")
            return None

    profit_total_abs = float(strat.get("profit_total_abs", 0.0) or 0.0)
    max_drawdown_abs = abs(float(strat.get("max_drawdown_abs", 0.0) or 0.0)) or 1e-6
    trade_count = int(strat.get("total_trades", 0) or 0)
    winrate = float(strat.get("winrate", 0.0) or 0.0)
    profit_total = float(strat.get("profit_total", 0.0) or 0.0)
    score = profit_total_abs / max_drawdown_abs

    return {
        "profit_total_abs": profit_total_abs,
        "profit_total": profit_total,
        "max_drawdown_abs": max_drawdown_abs,
        "trade_count": trade_count,
        "winrate": winrate,
        "score": score,
        "result_file": result_file.name,
    }


def get_holdout_timerange() -> str:
    end = utcnow()
    start = end - timedelta(days=HOLDOUT_DAYS)
    return f"{start:%Y%m%d}-{end:%Y%m%d}"


# ----------------------------------------------------------------------
# GEMINI ADVISOR (Analyse + Param-Vorschläge, kein Code)
# ----------------------------------------------------------------------

def call_gemini(prompt: str, temperature: float = 0.2) -> str | None:
    """Rohtext von Gemini oder OpenAI-kompatibler API."""
    if LLM_API_URL and LLM_API_KEY:
        try:
            url = (
                LLM_API_URL
                if LLM_API_URL.endswith("/chat/completions")
                else f"{LLM_API_URL}/chat/completions"
            )
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                },
                timeout=90,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning("LLM-API fehlgeschlagen: %s", e)

    if not GEMINI_API_KEY:
        return None

    model = LLM_MODEL.replace("gemini/", "")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        resp = requests.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": temperature},
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.warning("Gemini fehlgeschlagen: %s", e)
        return None


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def get_bot_profit_snapshot() -> dict:
    try:
        return ft_api("GET", "/api/v1/profit")
    except Exception as e:
        log.warning("Profit-Snapshot fehlgeschlagen: %s", e)
        return {}


def build_streak_defense_params(current: dict, live: dict) -> dict:
    """
    Heuristische Sofort-Nudges nach Losing-Streak (ohne Hyperopt):
    strengerer Entry (Volumen/RSI) + etwas früherer Quick-TP.
    """
    buy = dict(current.get("params", {}).get("buy", {}))
    sell = dict(current.get("params", {}).get("sell", {}))
    for k, v in DEFAULT_PARAMS["params"]["buy"].items():
        buy.setdefault(k, v)
    for k, v in DEFAULT_PARAMS["params"]["sell"].items():
        sell.setdefault(k, v)

    buy["volume_factor"] = min(2.0, float(buy["volume_factor"]) + 0.12)
    buy["rsi_buy_floor"] = min(50, int(buy["rsi_buy_floor"]) + 2)
    buy["rsi_buy_threshold"] = max(50, int(buy["rsi_buy_threshold"]) - 2)

    exits = live.get("exit_reasons") or {}
    stop_hits = sum(v for k, v in exits.items() if "stop" in str(k).lower())
    if stop_hits >= 2:
        # Zu oft gestoppt → noch selektiver, TP etwas früher sichern
        buy["volume_factor"] = min(2.0, float(buy["volume_factor"]) + 0.08)
        sell["quick_tp"] = max(0.0028, float(sell["quick_tp"]) - 0.0003)
    else:
        sell["quick_tp"] = max(0.0028, float(sell["quick_tp"]) - 0.0002)

    return {"strategy_name": STRATEGY_NAME, "params": {"buy": buy, "sell": sell}}


def gemini_advise(
    current_params: dict,
    live: dict,
    trades: list[dict],
    *,
    mode: str = "normal",
) -> dict:
    """
    Gemini analysiert Live-Trades/Performance und schlägt kleine Param-Nudges vor.
    mode=streak: Fokus auf die letzten Verluste (schnelle Reaktion).
    Ändert niemals strategy.py – nur strukturierte Vorschläge.
    """
    empty = {
        "enabled": False,
        "analysis": "",
        "spaces": live.get("spaces") or ["buy", "sell"],
        "params": None,
        "raw": None,
    }
    if not GEMINI_ADVISOR and mode != "streak":
        return empty
    if mode == "streak" and not GEMINI_ON_STREAK:
        return empty
    if not GEMINI_API_KEY and not (LLM_API_URL and LLM_API_KEY):
        log.info("Gemini-Advisor aus: kein GEMINI_API_KEY / LLM_API_* gesetzt")
        return empty

    profit = get_bot_profit_snapshot()
    sample = []
    for t in trades[-30:]:
        sample.append(
            {
                "pair": t.get("pair"),
                "profit_ratio": t.get("profit_ratio", t.get("close_profit")),
                "exit_reason": t.get("exit_reason") or t.get("sell_reason"),
                "enter_tag": t.get("enter_tag") or t.get("buy_tag"),
                "open_date": t.get("open_date") or t.get("open_timestamp"),
                "close_date": t.get("close_date") or t.get("close_timestamp"),
            }
        )

    allowed = {
        space: {
            k: {"min": m["min"], "max": m["max"], "max_delta": m["max_delta"]}
            for k, m in keys.items()
        }
        for space, keys in PARAM_BOUNDS.items()
    }

    streak_block = ""
    if mode == "streak":
        streak_block = f"""
KRITISCH: Losing-Streak von {live.get('lose_streak')} Trades.
Analysiere GENAU diese Verlierer und schlage Nudges vor, die DIESES Muster seltener machen
(z.B. strengere Entries bei Chase/FOMO, früherer Quick-TP bei Fade-Outs, höheres volume_factor).

Streak-Verlierer:
{json.dumps(live.get("streak_losers") or [], ensure_ascii=False)[:3000]}
"""

    prompt = f"""Du bist Quant-Analyst für einen Freqtrade Binance-Spot Paper-Bot (QuickScalp, 1m Dual-Entry: pullback + momentum).
Aufgabe: Muster in den Live-Trades erkennen und NUR kleine Parameter-Nudges vorschlagen.
Du darfst KEINEN Python-Code schreiben und keine Strategie-Datei ändern.
Antworte NUR mit einem JSON-Objekt (kein Markdown), Schema:

{{
  "analysis_de": "3-6 Sätze Markt-/Trade-Auswertung auf Deutsch, nüchtern, kein Investment-Tipp",
  "spaces": ["buy"] oder ["sell"] oder ["buy","sell"],
  "params": {{
    "buy": {{ "ema_fast_length": int, ... nur Keys die du ändern willst }},
    "sell": {{ "rsi_sell_threshold": int, ... }}
  }},
  "confidence": 0.0 bis 1.0
}}

Erlaubte Parameter + Bounds + max. Änderung pro Lauf:
{json.dumps(allowed, ensure_ascii=False)}

Aktuelle Parameter:
{json.dumps(current_params.get("params", current_params), ensure_ascii=False)}

Live-Stats:
{json.dumps({k: live.get(k) for k in ("count", "winrate", "avg_profit", "lose_streak", "exit_reasons", "hints")}, ensure_ascii=False)}

Bot-Profit-Snapshot (API):
{json.dumps({k: profit.get(k) for k in ("profit_closed_coin", "profit_closed_ratio", "closed_trade_count", "winning_trades", "losing_trades", "winrate") if k in profit}, ensure_ascii=False)}

Letzte Trades (Stichprobe):
{json.dumps(sample, ensure_ascii=False)[:6000]}
{streak_block}
Regeln:
- Nur Keys aus den erlaubten Bounds vorschlagen
- Werte nahe am aktuellen Stand (kleine Nudges)
- enter_tag pullback vs momentum beachten, wenn erkennbar
- Bei Unsicherheit params leer lassen und spaces=["buy","sell"]
"""

    text = call_gemini(prompt, temperature=0.2 if mode == "streak" else 0.25)
    if not text:
        return empty

    parsed = _extract_json_object(text)
    if not parsed:
        log.warning("Gemini-Antwort war kein gültiges JSON – nur Text-Analyse nutzen")
        return {
            "enabled": True,
            "analysis": text.strip()[:2000],
            "spaces": live.get("spaces") or ["buy", "sell"],
            "params": None,
            "raw": text[:4000],
        }

    spaces = parsed.get("spaces") or live.get("spaces") or ["buy", "sell"]
    spaces = [s for s in spaces if s in ("buy", "sell")]
    if not spaces:
        spaces = ["buy", "sell"]

    gem_params = parsed.get("params") or {}
    proposal = {
        "strategy_name": STRATEGY_NAME,
        "params": {
            "buy": dict(gem_params.get("buy") or {}),
            "sell": dict(gem_params.get("sell") or {}),
        },
    }

    return {
        "enabled": True,
        "analysis": str(parsed.get("analysis_de") or "")[:2500],
        "spaces": spaces,
        "params": proposal,
        "confidence": float(parsed.get("confidence") or 0.0),
        "raw": parsed,
    }


def blend_proposals(current: dict, hyperopt_raw: dict, gemini_raw: dict | None) -> dict:
    """Mischt Hyperopt + Gemini; fehlende Keys bleiben vom jeweils anderen / current."""
    cur = current.get("params", {})
    h = hyperopt_raw.get("params", {})
    g = (gemini_raw or {}).get("params", {}) if gemini_raw else {}

    out_buy: dict[str, Any] = {}
    out_sell: dict[str, Any] = {}

    for space, out in (("buy", out_buy), ("sell", out_sell)):
        keys = set(PARAM_BOUNDS[space]) | set(cur.get(space, {})) | set(h.get(space, {})) | set(
            g.get(space, {})
        )
        for key in keys:
            if key not in PARAM_BOUNDS[space]:
                continue
            meta = PARAM_BOUNDS[space][key]
            hv = h.get(space, {}).get(key)
            gv = g.get(space, {}).get(key)
            cv = cur.get(space, {}).get(key, DEFAULT_PARAMS["params"][space].get(key))

            if hv is not None and gv is not None:
                # Mittelwert → später nochmal gegen current clampen
                if meta["type"] == "int":
                    blended = int(round((float(hv) + float(gv)) / 2.0))
                else:
                    blended = (float(hv) + float(gv)) / 2.0
            elif hv is not None:
                blended = hv
            elif gv is not None:
                blended = gv
            else:
                blended = cv
            out[key] = blended

    return {"strategy_name": STRATEGY_NAME, "params": {"buy": out_buy, "sell": out_sell}}


# ----------------------------------------------------------------------
# REPORTS
# ----------------------------------------------------------------------

def generate_report_text(
    old_metrics: dict,
    new_metrics: dict,
    switched: bool,
    changes: list[str],
    live: dict,
    settled_after: bool,
    gemini: dict | None = None,
) -> str:
    decision = "Kleine Nudges übernommen" if switched else "Keine Übernahme (Plateau / kein sicherer Vorteil)"
    gemini = gemini or {}
    text = (
        f"**Entscheidung:** {decision}\n"
        f"**Settled:** {settled_after}\n"
        f"**Live-Analyse:** n={live.get('count')}, "
        f"winrate={live.get('winrate')}, avg={live.get('avg_profit')}, "
        f"hints={live.get('hints')}\n"
        f"**Änderungen:** {changes or ['(keine)']}\n\n"
        f"- Alt: Profit {old_metrics.get('profit_total_abs'):.4f}, "
        f"DD {old_metrics.get('max_drawdown_abs'):.4f}, "
        f"Trades {old_metrics.get('trade_count')}, "
        f"WR {old_metrics.get('winrate'):.1%}, Score {old_metrics.get('score'):.4f}\n"
        f"- Neu: Profit {new_metrics.get('profit_total_abs'):.4f}, "
        f"DD {new_metrics.get('max_drawdown_abs'):.4f}, "
        f"Trades {new_metrics.get('trade_count')}, "
        f"WR {new_metrics.get('winrate'):.1%}, Score {new_metrics.get('score'):.4f}\n"
    )
    if gemini.get("analysis"):
        text += f"\n**Gemini-Marktauswertung:**\n{gemini['analysis']}\n"

    # Kurze zusätzliche Zusammenfassung nur wenn Advisor nicht schon ausführlich war
    if not gemini.get("analysis"):
        summary = call_gemini(
            "Fasse in 4 kurzen Sätzen auf Deutsch zusammen (kein Investment-Tipp):\n" + text,
            temperature=0.2,
        )
        if summary:
            return summary

    return text


def save_report(text: str, switched: bool) -> Path:
    ts = utcnow().strftime("%Y%m%d_%H%M%S")
    tag = "SWITCH" if switched else "KEEP"
    path = REPORTS_DIR / f"report_{ts}_{tag}.md"
    path.write_text(f"# QuickScalp Optimizer – {ts} UTC\n\n{text}\n", encoding="utf-8")
    return path


def notify_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]},
            timeout=15,
        )
    except Exception as e:
        log.warning("Telegram fehlgeschlagen: %s", e)


# ----------------------------------------------------------------------
# ZYKLUS
# ----------------------------------------------------------------------

def decide_switch(old_m: dict, new_m: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    enough = (
        new_m["trade_count"] >= MIN_HOLDOUT_TRADES and old_m["trade_count"] >= MIN_HOLDOUT_TRADES
    )
    if not enough:
        reasons.append("zu wenig Hold-out-Trades")

    improved = new_m["score"] > old_m["score"] * (1.0 + IMPROVEMENT_THRESHOLD)
    if not improved:
        reasons.append("Score-Verbesserung unter Schwelle")

    # Profit darf nicht schlechter werden (absolute USDT)
    profit_ok = new_m["profit_total_abs"] >= old_m["profit_total_abs"]
    if not profit_ok:
        reasons.append("Profit schlechter")

    dd_ok = new_m["max_drawdown_abs"] <= old_m["max_drawdown_abs"] * MAX_DD_RATIO + 1e-9
    if not dd_ok:
        reasons.append("Drawdown zu hoch")

    # Kein "leerer" Lauf der nur weniger Trades hat und dadurch Score verzerrt
    trade_ok = new_m["trade_count"] >= max(MIN_HOLDOUT_TRADES, int(old_m["trade_count"] * 0.5))
    if not trade_ok:
        reasons.append("zu starker Trade-Count-Einbruch")

    return bool(enough and improved and profit_ok and dd_ok and trade_ok), reasons


def run_streak_cycle(state: dict) -> dict:
    """
    Schnellpfad bei Losing-Streak: Gemini + Heuristik → Clamp → reload.
    Kein Hyperopt (minutenlang), damit Stellschrauben sofort reagieren.
    """
    log.info("=== STREAK-Zyklus START (Gemini-Fast) ===")
    with CycleLock(LOCK_FILE):
        live_trades = fetch_recent_trades(LIVE_TRADE_LOOKBACK)
        live = analyze_live_trades(live_trades)
        streak = int(live.get("lose_streak") or 0)
        log.info(
            "Streak=%s | WR=%s avg=%s losers=%s",
            streak,
            live.get("winrate"),
            live.get("avg_profit"),
            live.get("streak_losers"),
        )

        old_params = ensure_params_file()
        defense = build_streak_defense_params(old_params, live)
        gemini = gemini_advise(old_params, live, live_trades, mode="streak")

        if gemini.get("enabled") and gemini.get("params"):
            blended = blend_proposals(old_params, defense, gemini.get("params"))
        else:
            blended = defense
            if not gemini.get("enabled"):
                gemini = {
                    "enabled": False,
                    "analysis": "Gemini offline – nur Heuristik-Defense",
                    "confidence": 0.4,
                    "spaces": ["buy", "sell"],
                }

        nudged, changes = apply_incremental_nudge(old_params, blended)
        log.info("Streak-Nudges: %s", changes or ["keine"])

        # Cooldown immer setzen – sonst alle 2 Min Spam bei gleicher Streak
        state["last_streak_timestamp"] = utcnow().isoformat()
        state["streak_runs"] = int(state.get("streak_runs", 0)) + 1

        wr = live.get("winrate")
        wr_txt = f"{float(wr) * 100:.0f}%" if wr is not None else "—"
        avg = live.get("avg_profit")
        avg_txt = f"{float(avg) * 100:.2f}%" if avg is not None else "—"

        if params_equal(old_params, nudged):
            msg = (
                f"Optimizer – Lose-Streak {streak}\n"
                f"Keine Parameter-Änderung möglich.\n"
                f"Live: {live.get('count')} Trades | WR {wr_txt} | Ø {avg_txt}\n"
                f"Notiz: {gemini.get('analysis') or '—'}"
            )
            path = save_report(msg, False)
            notify_telegram(msg[:3500])
            append_audit(
                {
                    "switched": False,
                    "reason": "streak_no_delta",
                    "mode": "streak",
                    "live": {k: live.get(k) for k in ("count", "winrate", "avg_profit", "lose_streak", "hints")},
                    "gemini": {"analysis": gemini.get("analysis"), "confidence": gemini.get("confidence")},
                    "report": str(path),
                }
            )
            return state

        write_params_atomic(nudged)
        try:
            reload_bot_config()
        except Exception as e:
            log.error("reload_config nach Streak fehlgeschlagen → Rollback: %s", e)
            restore_backup_or(old_params)
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
            return state

        state["settled"] = False
        state["keep_streak"] = 0
        if live.get("winrate") is not None:
            state["last_accepted_live_winrate"] = live["winrate"]

        change_lines = "\n".join(f"• {c}" for c in (changes or ["(keine)"]))
        msg = (
            f"Optimizer – Lose-Streak {streak}\n"
            f"Parameter angepasst (ohne Hyperopt).\n\n"
            f"{change_lines}\n\n"
            f"Live: {live.get('count')} Trades | WR {wr_txt} | Ø {avg_txt}\n"
            f"Notiz: {gemini.get('analysis') or 'Heuristik-Defense'}"
        )
        path = save_report(msg, True)
        notify_telegram(msg[:3500])
        append_audit(
            {
                "switched": True,
                "reason": "lose_streak_fast",
                "mode": "streak",
                "changes": changes,
                "live": {
                    k: live.get(k)
                    for k in ("count", "winrate", "avg_profit", "lose_streak", "streak_losers", "hints")
                },
                "gemini": {
                    "analysis": gemini.get("analysis"),
                    "confidence": gemini.get("confidence"),
                    "spaces": gemini.get("spaces"),
                },
                "report_file": str(path),
            }
        )
        state["consecutive_failures"] = 0
        log.info("=== STREAK-Zyklus ENDE – %s ===", path.name)
        return state


def run_optimization_cycle(timeframe: str, state: dict) -> dict:
    log.info("=== Optimierungs-Zyklus START ===")
    with CycleLock(LOCK_FILE):
        live_trades = fetch_recent_trades(LIVE_TRADE_LOOKBACK)
        live = analyze_live_trades(live_trades)
        log.info(
            "Live: n=%s winrate=%s avg=%s streak=%s spaces=%s hints=%s",
            live.get("count"),
            live.get("winrate"),
            live.get("avg_profit"),
            live.get("lose_streak"),
            live.get("spaces"),
            live.get("hints"),
        )

        old_params = ensure_params_file()
        gemini = gemini_advise(old_params, live, live_trades)
        if gemini.get("enabled"):
            log.info(
                "Gemini-Advisor aktiv (confidence=%s spaces=%s)",
                gemini.get("confidence"),
                gemini.get("spaces"),
            )
            if gemini.get("analysis"):
                log.info("Gemini: %s", gemini["analysis"][:400])
            # Spaces: Schnittmenge wenn Gemini + Heuristik, sonst Union
            if gemini.get("spaces"):
                live_spaces = set(live.get("spaces") or ["buy", "sell"])
                gem_spaces = set(gemini["spaces"])
                spaces = sorted(live_spaces & gem_spaces) or sorted(live_spaces | gem_spaces)
            else:
                spaces = live.get("spaces") or ["buy", "sell"]
        else:
            spaces = live.get("spaces") or ["buy", "sell"]

        download_fresh_data(timeframe)
        holdout = get_holdout_timerange()
        log.info("Hold-out: %s | Hyperopt-Spaces: %s", holdout, spaces)

        write_params_atomic(old_params)
        old_metrics = run_backtest(holdout, timeframe)
        if old_metrics is None:
            log.error("Baseline-Backtest fehlgeschlagen.")
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
            return state

        ok = run_hyperopt(timeframe, spaces)
        if not ok:
            log.error("Hyperopt fehlgeschlagen – keine Änderung.")
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
            return state

        raw_new = extract_best_hyperopt_params()
        if raw_new is None:
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
            return state

        blended = blend_proposals(old_params, raw_new, gemini.get("params"))
        nudged, changes = apply_incremental_nudge(old_params, blended)
        log.info("Nudges (Hyperopt⊕Gemini→Clamp): %s", changes or ["keine"])

        if params_equal(old_params, nudged):
            log.info("Nach Clamp identisch → KEEP / Plateau-Signal")
            state["keep_streak"] = int(state.get("keep_streak", 0)) + 1
            if state["keep_streak"] >= PLATEAU_KEEP_STREAK:
                state["settled"] = True
                log.info("Settled: %s× ohne sinnvolle Nudges", state["keep_streak"])
            report = generate_report_text(
                old_metrics,
                old_metrics,
                False,
                changes,
                live,
                bool(state.get("settled")),
                gemini,
            )
            path = save_report(report, False)
            notify_telegram(f"QuickScalp Optimizer: ⏸ keine Nudges\n\n{report[:1500]}")
            append_audit(
                {
                    "switched": False,
                    "reason": "no_delta_after_clamp",
                    "live": live,
                    "gemini": {"analysis": gemini.get("analysis"), "confidence": gemini.get("confidence")},
                    "report": str(path),
                }
            )
            state["consecutive_failures"] = 0
            return state

        write_params_atomic(nudged)
        new_metrics = run_backtest(holdout, timeframe)
        if new_metrics is None:
            log.error("Neuer Backtest fehlgeschlagen → Rollback")
            restore_backup_or(old_params)
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
            return state

        switch, reasons = decide_switch(old_metrics, new_metrics)

        if switch:
            write_params_atomic(nudged)
            try:
                reload_bot_config()
            except Exception as e:
                log.error("reload_config fehlgeschlagen → Rollback: %s", e)
                restore_backup_or(old_params)
                state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
                return state

            state["keep_streak"] = 0
            state["settled"] = False
            if live.get("winrate") is not None:
                state["last_accepted_live_winrate"] = live["winrate"]
            log.info("SWITCH %s", changes)
        else:
            write_params_atomic(old_params)
            state["keep_streak"] = int(state.get("keep_streak", 0)) + 1
            if state["keep_streak"] >= PLATEAU_KEEP_STREAK:
                state["settled"] = True
                log.info("Settled nach %s KEEP-Läufen (%s)", state["keep_streak"], ", ".join(reasons))
            else:
                log.info("KEEP (%s)", ", ".join(reasons))

        report = generate_report_text(
            old_metrics,
            new_metrics,
            switch,
            changes,
            live,
            bool(state.get("settled")),
            gemini,
        )
        report_path = save_report(report, switch)
        notify_telegram(
            f"QuickScalp Optimizer: {'✅ Nudge übernommen' if switch else '⏸ Plateau/KEEP'}\n\n{report[:1500]}"
        )
        append_audit(
            {
                "switched": switch,
                "reasons": reasons,
                "changes": changes,
                "old_metrics": old_metrics,
                "new_metrics": new_metrics,
                "live": live,
                "gemini": {
                    "analysis": gemini.get("analysis"),
                    "confidence": gemini.get("confidence"),
                    "spaces": gemini.get("spaces"),
                },
                "settled": state.get("settled"),
                "report_file": str(report_path),
            }
        )
        state["consecutive_failures"] = 0
        log.info("=== Zyklus ENDE – %s ===", report_path.name)
        return state


def is_distress(live: dict) -> bool:
    """Klar schlechte Live-Phase → kürzere Pause / früherer Trigger erlaubt."""
    n = int(live.get("count") or 0)
    if n < DISTRESS_TRADE_THRESHOLD:
        return False
    wr = live.get("winrate")
    avg = live.get("avg_profit")
    bad_wr = wr is not None and float(wr) < DISTRESS_WINRATE
    bad_avg = avg is not None and float(avg) < DISTRESS_AVG_PROFIT
    return bool(bad_wr or bad_avg)


def hours_since_iso(ts: str | None) -> float:
    if not ts:
        return float("inf")
    last = datetime.fromisoformat(ts)
    return (utcnow() - last).total_seconds() / 3600.0


def should_run_streak(state: dict, live: dict) -> tuple[bool, str]:
    streak = int(live.get("lose_streak") or 0)
    if streak < LOSE_STREAK_TRIGGER:
        return False, f"streak {streak}/{LOSE_STREAK_TRIGGER}"
    # Eigene kurze Cooldown nur für Streak-Läufe (nicht den normalen Hyperopt-Takt)
    since_streak = hours_since_iso(state.get("last_streak_timestamp"))
    if since_streak < STREAK_MIN_HOURS:
        return False, f"streak-pause {since_streak:.2f}h < {STREAK_MIN_HOURS}h"
    return True, f"lose-streak-{streak}"


def should_run(state: dict, new_trades: int, hours_since: float, live: dict) -> tuple[bool, str]:
    if state.get("consecutive_failures", 0) >= 5:
        if hours_since < 12:
            return False, "cooldown nach Fehlern (<12h)"

    distress = is_distress(live)
    min_hours = SETTLED_HOURS_BETWEEN_RUNS if state.get("settled") else MIN_HOURS_BETWEEN_RUNS
    if distress:
        min_hours = min(min_hours, DISTRESS_MIN_HOURS)

    last_wr = state.get("last_accepted_live_winrate")
    cur_wr = live.get("winrate")
    wake = False
    if (
        state.get("settled")
        and last_wr is not None
        and cur_wr is not None
        and live.get("count", 0) >= max(10, TRADE_THRESHOLD // 2)
        and cur_wr < float(last_wr) - WAKE_WINRATE_DROP
    ):
        wake = True
        log.info("Wake from settled: Live-WR %.1f%% → %.1f%%", last_wr * 100, cur_wr * 100)

    trade_need = DISTRESS_TRADE_THRESHOLD if distress else TRADE_THRESHOLD
    if new_trades < trade_need and not wake:
        return False, f"Trades {new_trades}/{trade_need}"

    if hours_since < min_hours and not wake:
        tag = " (distress)" if distress else ""
        return False, f"Pause {hours_since:.1f}h < {min_hours}h{tag}"

    if wake:
        return True, "wake-from-settled"
    if distress:
        return True, "distress-fast-path"
    return True, "trades+interval"


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main() -> None:
    from trade_journal import (  # noqa: WPS433 – gleicher Container-Pfad
        JOURNAL_ENABLED,
        JOURNAL_FILE,
        JOURNAL_LOOKBACK,
        JOURNAL_TELEGRAM_BATCH,
        process_trade_journal,
    )

    log.info(
        "Optimizer | threshold=%s poll=%ss/%ss pause=%sh settled=%sh "
        "lose_streak=%s streak_min=%sh epochs=%s",
        TRADE_THRESHOLD,
        POLL_INTERVAL_SECONDS,
        POLL_FAST_SECONDS,
        MIN_HOURS_BETWEEN_RUNS,
        SETTLED_HOURS_BETWEEN_RUNS,
        LOSE_STREAK_TRIGGER,
        STREAK_MIN_HOURS,
        HYPEROPT_EPOCHS,
    )
    log.info(
        "Trade-Journal: enabled=%s batch=%s file=%s",
        JOURNAL_ENABLED,
        JOURNAL_TELEGRAM_BATCH,
        JOURNAL_FILE,
    )
    log.info("Safety: schreibt NUR %s – nie config.json", STRATEGY_PARAMS_FILE.name)

    wait_for_api()
    ensure_params_file()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    timeframe = cfg.get("timeframe", "1m")
    if cfg.get("strategy") and cfg["strategy"] != STRATEGY_NAME:
        log.warning("Config-Strategy=%s ≠ Optimizer=%s", cfg["strategy"], STRATEGY_NAME)

    state = load_state()

    if state.get("last_run_timestamp") is None and not RUN_ON_START:
        try:
            baseline = get_closed_trade_count()
            state["last_processed_closed_trades"] = baseline
            save_state(state)
            log.info("Baseline: %s closed – warte auf +%s neue Trades.", baseline, TRADE_THRESHOLD)
        except Exception:
            log.exception("Baseline fehlgeschlagen")

    if RUN_ON_START:
        log.info("OPTIMIZER_RUN_ON_START → sofortiger Zyklus")
        try:
            state = run_optimization_cycle(timeframe, state)
            state["last_processed_closed_trades"] = get_closed_trade_count()
            state["last_run_timestamp"] = utcnow().isoformat()
            state["runs"] = int(state.get("runs", 0)) + 1
            save_state(state)
        except Exception:
            log.exception("Start-Zyklus fehlgeschlagen")

    while True:
        poll = POLL_INTERVAL_SECONDS
        try:
            current_closed = get_closed_trade_count()
            new_trades = current_closed - int(state.get("last_processed_closed_trades", 0))
            hours_since = hours_since_iso(state.get("last_run_timestamp"))

            # Journal + Telegram-Batch zuerst (unabhängig vom Optimizer-Zyklus)
            try:
                journal_trades = fetch_recent_trades(max(JOURNAL_LOOKBACK, LIVE_TRADE_LOOKBACK))
                state = process_trade_journal(journal_trades, state, notify_telegram)
                save_state(state)
            except Exception:
                log.exception("Trade-Journal fehlgeschlagen")

            live = analyze_live_trades(fetch_recent_trades(LIVE_TRADE_LOOKBACK))
            pending_n = len(state.get("journal_pending") or [])
            if (
                is_distress(live)
                or int(live.get("lose_streak") or 0) >= LOSE_STREAK_TRIGGER - 1
                or pending_n > 0
            ):
                poll = POLL_FAST_SECONDS

            streak_run, streak_why = should_run_streak(state, live)
            run, why = should_run(state, new_trades, hours_since, live)

            if streak_run:
                log.info(
                    "Trigger STREAK: %s (closed=%s live_wr=%s)",
                    streak_why,
                    current_closed,
                    live.get("winrate"),
                )
                state = run_streak_cycle(state)
                save_state(state)
            elif run:
                log.info(
                    "Trigger: %s (neue Trades=%s, %.1fh)",
                    why,
                    new_trades,
                    hours_since if hours_since != float("inf") else -1,
                )
                state = run_optimization_cycle(timeframe, state)
                state["last_processed_closed_trades"] = get_closed_trade_count()
                state["last_run_timestamp"] = utcnow().isoformat()
                state["runs"] = int(state.get("runs", 0)) + 1
                save_state(state)
            else:
                log.info(
                    "Warte (%s | %s) | streak=%s settled=%s | closed=%s wr=%s "
                    "journal_pending=%s poll=%ss",
                    why,
                    streak_why,
                    live.get("lose_streak"),
                    state.get("settled"),
                    current_closed,
                    live.get("winrate"),
                    pending_n,
                    poll,
                )
        except Exception:
            log.exception("Fehler im Optimizer-Loop")

        time.sleep(poll)


if __name__ == "__main__":
    main()
