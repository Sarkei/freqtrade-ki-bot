"""
Trade-Journal: jede geschlossene Trade → Markdown-Log,
alle N Trades → Telegram (klar lesbar, Trade für Trade).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("quickscalp-journal")

JOURNAL_ENABLED = os.environ.get("JOURNAL_ENABLED", "true").lower() in ("1", "true", "yes")
JOURNAL_TELEGRAM_BATCH = int(os.environ.get("JOURNAL_TELEGRAM_BATCH", "5") or "5")
JOURNAL_LOOKBACK = int(os.environ.get("JOURNAL_LOOKBACK", "200") or "200")
JOURNAL_FILE = Path(
    os.environ.get("JOURNAL_FILE", "/freqtrade/user_data/logs/trade_journal.md")
)
JOURNAL_JSONL = Path(
    os.environ.get("JOURNAL_JSONL", "/freqtrade/user_data/logs/trade_journal.jsonl")
)

# Exit-Reason → kurze deutsche Erklärung
EXIT_RULES: dict[str, str] = {
    "quick_tp": "Quick-TP (früher Take-Profit erreicht)",
    "time_tp": "Zeit-TP (nach ~5 Min im Plus mitgenommen)",
    "late_tp": "Später TP (nach längerer Haltedauer mitgenommen)",
    "stale_cut": "Stale-Cut (zu lange im Minus → abgeschnitten)",
    "stale_flat": "Stale-Flat (zu lange ohne klaren Profit → raus)",
    "stop_loss": "Hard-Stoploss",
    "trailing_stop_loss": "Trailing-Stop",
    "exit_signal": "Exit-Signal (Strategie-Verkaufssignal)",
    "roi": "ROI-Tabelle",
    "force_exit": "Manueller Force-Exit",
    "liquidation": "Liquidation",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _trade_id(t: dict) -> int | None:
    for key in ("trade_id", "id"):
        if t.get(key) is not None:
            try:
                return int(t[key])
            except (TypeError, ValueError):
                pass
    return None


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _s(v: Any, default: str = "—") -> str:
    if v is None or v == "":
        return default
    return str(v)


def _fmt_dt(raw: str) -> str:
    """ISO/UTC-String → 'YYYY-MM-DD HH:MM:SS'."""
    s = _s(raw, "")
    if not s or s == "—":
        return "—"
    s = s.replace("T", " ").replace("Z", "").replace("+00:00", "")
    if "." in s:
        s = s.split(".", 1)[0]
    return s.strip()


def _fmt_price(rate: float) -> str:
    if rate >= 100:
        return f"{rate:.2f}"
    if rate >= 1:
        return f"{rate:.4f}"
    if rate >= 0.01:
        return f"{rate:.6f}"
    return f"{rate:.8g}"


def _rule_text(reason: str) -> str:
    key = (reason or "").strip()
    label = EXIT_RULES.get(key)
    if label:
        return f"{key} — {label}"
    return key or "unknown"


def normalize_trade(t: dict) -> dict[str, Any]:
    tid = _trade_id(t)
    profit_ratio = t.get("profit_ratio")
    if profit_ratio is None:
        profit_ratio = t.get("close_profit")
    profit_abs = t.get("profit_abs")
    if profit_abs is None:
        profit_abs = t.get("close_profit_abs")

    stake = t.get("stake_amount")
    if stake is None:
        stake = t.get("stake_amount_open")

    return {
        "trade_id": tid,
        "pair": _s(t.get("pair")),
        "open_date": _fmt_dt(_s(t.get("open_date") or t.get("open_timestamp"))),
        "close_date": _fmt_dt(_s(t.get("close_date") or t.get("close_timestamp"))),
        "open_rate": _f(t.get("open_rate")),
        "close_rate": _f(t.get("close_rate")),
        "stake_amount": _f(stake),
        "amount": _f(t.get("amount")),
        "profit_abs": _f(profit_abs),
        "profit_ratio": _f(profit_ratio),
        "exit_reason": _s(t.get("exit_reason") or t.get("sell_reason"), "unknown"),
        "enter_tag": _s(t.get("enter_tag") or t.get("buy_tag"), "—"),
        "stake_currency": _s(t.get("stake_currency"), "USDT"),
    }


def format_trade_block(tr: dict) -> str:
    """Eine Trade – Markdown (Journal) und Telegram (gleicher Aufbau)."""
    pr = tr["profit_ratio"] * 100.0
    sign = "+" if tr["profit_abs"] >= 0 else ""
    tag = tr["enter_tag"] if tr["enter_tag"] != "—" else "—"
    return (
        f"#{tr['trade_id']}  {tr['pair']}\n"
        f"Einstieg:  {tr['open_date']} @ {_fmt_price(tr['open_rate'])}\n"
        f"Ausstieg:  {tr['close_date']} @ {_fmt_price(tr['close_rate'])}\n"
        f"Einsatz:   {tr['stake_amount']:.2f} {tr['stake_currency']}\n"
        f"Profit:    {sign}{tr['profit_abs']:.4f} {tr['stake_currency']} ({sign}{pr:.2f}%)\n"
        f"Entry-Tag: {tag}\n"
        f"Regel:     {_rule_text(tr['exit_reason'])}\n"
    )


def format_batch_telegram(batch: list[dict]) -> str:
    wins = sum(1 for t in batch if t["profit_ratio"] > 0)
    total_abs = sum(t["profit_abs"] for t in batch)
    total_sign = "+" if total_abs >= 0 else ""

    parts = [
        f"Letzte {len(batch)} Trades",
        f"Quote: {wins}/{len(batch)} | Summe: {total_sign}{total_abs:.4f} USDT",
        "",
    ]
    for tr in batch:
        parts.append(format_trade_block(tr).rstrip())
        parts.append("")  # Leerzeile zwischen Trades
    return "\n".join(parts).rstrip() + "\n"


def _ensure_journal_header() -> None:
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not JOURNAL_FILE.exists():
        JOURNAL_FILE.write_text(
            "# QuickScalp Trade Journal\n\n"
            "Jeder geschlossene Trade (Gewinn und Verlust):\n"
            "Pair, Einstieg, Ausstieg, Einsatz, Profit, Exit-Regel.\n\n"
            "---\n\n",
            encoding="utf-8",
        )


def append_trade_to_journal(tr: dict) -> None:
    _ensure_journal_header()
    block = format_trade_block(tr) + "\n"
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write(block)
    with open(JOURNAL_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(tr, ensure_ascii=False) + "\n")


def _running_stats_line(all_closed_normalized: list[dict]) -> str:
    if not all_closed_normalized:
        return ""
    n = len(all_closed_normalized)
    wins = sum(1 for t in all_closed_normalized if t["profit_ratio"] > 0)
    total = sum(t["profit_abs"] for t in all_closed_normalized)
    wr = wins / n * 100.0
    sign = "+" if total >= 0 else ""
    return (
        f"---\n"
        f"Statistik: {n} Trades | WR {wr:.1f}% ({wins}/{n}) | "
        f"Summe {sign}{total:.4f} USDT | {_utcnow().isoformat()}Z\n\n"
    )


def process_trade_journal(
    closed_trades: list[dict],
    state: dict,
    notify: Callable[[str], None],
) -> dict:
    """
    Neue closed Trades → Journal-Datei.
    Alle JOURNAL_TELEGRAM_BATCH neuen Trades → Telegram.
    Erster Lauf: Baseline (nur Log, kein Telegram-Spam).
    """
    if not JOURNAL_ENABLED:
        return state

    normalized = []
    for t in closed_trades:
        if t.get("is_open") is True:
            continue
        tr = normalize_trade(t)
        if tr["trade_id"] is None:
            continue
        if not (t.get("close_date") or t.get("close_timestamp") or t.get("is_open") is False):
            continue
        normalized.append(tr)

    normalized.sort(key=lambda x: (x["trade_id"], x["close_date"]))

    last_id = state.get("journal_last_trade_id")
    pending: list[dict] = list(state.get("journal_pending") or [])

    if last_id is None:
        _ensure_journal_header()
        if normalized:
            with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
                f.write(
                    f"## Baseline-Import {_utcnow().isoformat()}Z "
                    f"({len(normalized)} Trades, kein Telegram)\n\n"
                )
            for tr in normalized:
                append_trade_to_journal(tr)
            with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
                f.write(_running_stats_line(normalized))
            state["journal_last_trade_id"] = max(t["trade_id"] for t in normalized)
            log.info(
                "Journal-Baseline: %s Trades → %s (Telegram ab jetzt)",
                len(normalized),
                JOURNAL_FILE,
            )
        else:
            state["journal_last_trade_id"] = 0
            log.info("Journal-Baseline: keine closed Trades")
        state["journal_pending"] = []
        return state

    new_ones = [t for t in normalized if t["trade_id"] > int(last_id)]
    if not new_ones:
        return state

    for tr in new_ones:
        append_trade_to_journal(tr)
        pending.append(tr)
        state["journal_last_trade_id"] = tr["trade_id"]
        log.info(
            "Journal: #%s %s profit=%.4f reason=%s",
            tr["trade_id"],
            tr["pair"],
            tr["profit_abs"],
            tr["exit_reason"],
        )

    while len(pending) >= JOURNAL_TELEGRAM_BATCH:
        batch = pending[:JOURNAL_TELEGRAM_BATCH]
        pending = pending[JOURNAL_TELEGRAM_BATCH:]
        msg = format_batch_telegram(batch)
        try:
            notify(msg)
            # Stats nur nach Telegram-Batch ins MD (weniger Spam)
            known = [t for t in normalized if t["trade_id"] <= state["journal_last_trade_id"]]
            with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
                f.write(_running_stats_line(known))
            log.info("Telegram Trade-Batch: %s Trades gesendet", len(batch))
        except Exception as e:
            log.warning("Telegram Batch fehlgeschlagen: %s", e)
            pending = batch + pending
            break

    state["journal_pending"] = [
        {
            "trade_id": t["trade_id"],
            "pair": t["pair"],
            "open_date": t["open_date"],
            "close_date": t["close_date"],
            "open_rate": t["open_rate"],
            "close_rate": t["close_rate"],
            "stake_amount": t["stake_amount"],
            "amount": t["amount"],
            "profit_abs": t["profit_abs"],
            "profit_ratio": t["profit_ratio"],
            "exit_reason": t["exit_reason"],
            "enter_tag": t["enter_tag"],
            "stake_currency": t["stake_currency"],
        }
        for t in pending
    ]
    return state
