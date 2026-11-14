# freqtrade-ki-bot

Freqtrade-Spot-Bot für Binance: eine 1-Minuten-Scalping-Strategie (`QuickScalpStrategy`) läuft zusammen mit einem zweiten Container, der dieselbe Strategie zyklisch nachoptimiert, jeden geschlossenen Trade journaliert und Ergebnisse per Telegram meldet. Der Optimizer schreibt ausschließlich die Parameterdatei der Strategie – nie `config.json`, nie Python-Code.

## Funktionen

**Strategie `QuickScalpStrategy`** (Zeitrahmen 1m, nur Long, `INTERFACE_VERSION = 3`)
- Entry `pullback`: Bounce nach Pullback im lokalen Uptrend, zusätzlich BTC/USDT-5m-Uptrend als Marktfilter, Pair-5m über EMA20, Anti-„Falling Knife“ (keine zwei roten Lower-Lows), kein Kaskaden-Abverkauf, Nähe zur EMA, RSI-Fenster, Volumen-, ATR- und Body-Filter. Momentum-Chase ist bewusst entfernt.
- Exit: ROI-Tabelle 0,45 % → 0,22 % (0/4/10/25 Min), Stoploss −2,2 %, `custom_stoploss` zieht den Stop bei +0,35 % / +0,45 % auf −0,1 % / −0,05 %, `custom_exit` mit `quick_tp`, `time_tp`, `late_tp`, `stale_cut`, `stale_flat`.
- Protections: `CooldownPeriod` (3 Kerzen), `StoplossGuard` (2 Stops in 90 Kerzen je Pair), `MaxDrawdown` (8 % in 120 Kerzen).
- Optimierbare Parameter (`optimize=True, load=True`): `ema_fast_length`, `ema_slow_length`, `rsi_length`, `rsi_buy_floor`, `rsi_buy_threshold`, `rsi_sell_threshold`, `volume_factor`, `atr_multiplier`, `quick_tp`.

**Auto-Optimizer** (`user_data/optimizer/optimizer.py`, parallel im eigenen Container)
- Hybrid-Trigger: Poll alle 300 s (120 s bei Distress/Streak), Normal-Lauf ab 12 neuen geschlossenen Trades und 4 h Pause, im „settled“-Zustand 12 h, Distress-Pfad ab 1 h, Wake-up wenn die Live-Winrate um 0,10 fällt.
- Losing-Streak-Schnellpfad: ab 3 Verlusten in Folge Gemini-Analyse plus heuristische Defensive (Volume-Faktor hoch, RSI-Fenster enger, `quick_tp` früher) ohne Hyperopt.
- Normal-Zyklus: Live-Analyse → LLM-Beratung → Daten-Download → Baseline-Backtest (Hold-out 7 Tage) → Hyperopt (45 Tage Trainingsfenster) → Mischen Hyperopt/LLM → Clamp auf `max_delta` → Hold-out-Backtest → Entscheidung → `reload_config`, sonst Rollback aus `.bak`.
- Übernahme nur bei Score-Verbesserung ≥ 3 %, nicht schlechterem Profit, Drawdown ≤ 115 % und mindestens 8 Trades im Hold-out.
- Sicherungen: `PARAM_BOUNDS` mit Min/Max und maximalem Schritt pro Lauf, Invarianten (`ema_slow ≥ ema_fast + 5`, RSI-Fenster), atomares Schreiben, `fcntl`-Lock gegen parallele Zyklen, `state.json`, `audit_log.jsonl`, Markdown-Reports in `reports/`.

**Trade-Journal** (`user_data/optimizer/trade_journal.py`)
- Jeder geschlossene Trade als Block in `trade_journal.md` plus JSONL-Zeile, Exit-Grund auf Deutsch erklärt.
- Alle `JOURNAL_TELEGRAM_BATCH` Trades eine Telegram-Zusammenfassung mit Quote und Summe; beim ersten Lauf nur Baseline-Import ohne Telegram.

**LLM-Advisor**: Gemini `generateContent` oder eine OpenAI-kompatible API, Antwort strikt als JSON (`analysis_de`, `spaces`, `params`, `confidence`); Vorschläge werden gegen die erlaubten Bounds gefiltert. Das Modell darf keinen Code liefern.

## Technik

- Python 3, `pandas`, `talib.abstract`, `requests`; Strategie als Freqtrade-`IStrategy`.
- Freqtrade `freqtradeorg/freqtrade:stable`, zwei Services in einem `docker-compose.yaml`.
- `freqtrade`-Service: `trade` mit SQLite (`tradesv3.sqlite`), Logfile, REST-API auf Port 8080, Netze `default` + externes `cloudflare-tunnel-net`, feste DNS-Server (1.1.1.1, 8.8.8.8).
- `optimizer`-Service: gleiches Image, Entrypoint `python /freqtrade/user_data/optimizer/optimizer.py`, ruft Freqtrade-CLI (`download-data`, `hyperopt`, `backtesting`, `hyperopt-show`) als Subprozess und die Bot-API (`/api/v1/ping`, `/trades`, `/profit`, `/reload_config`).
- Austausch nur über das gemountete `./user_data`; `stop_grace_period` 20 s/30 s, damit Webhook-Redeploys nicht in den Timeout laufen.

## Projektstruktur

```text
.
├── docker-compose.yaml          # Services freqtrade + optimizer, Netze, Env-Mapping
├── .env.example                 # Vorlage für alle Laufzeit-Variablen
├── scripts/redeploy.sh          # Webhook-Redeploy: git reset, Rechte, compose up, Mount-Check
└── user_data/
    ├── config.json              # Freqtrade-Konfiguration (Binance, Pairlist, API, Telegram)
    ├── strategies/
    │   ├── QuickScalpStrategy.py    # aktive Strategie
    │   ├── QuickScalpStrategy.json  # vom Optimizer gepflegte Parameter (+ .bak)
    │   └── FreqaiExampleStrategy.py # ungenutzte FreqAI-Beispielstrategie
    └── optimizer/
        ├── optimizer.py         # Trigger, Hyperopt/Backtest, LLM, Clamp, Reports
        └── trade_journal.py     # Journal-Dateien + Telegram-Batches
```

Laufzeit-Artefakte (nicht im Repo): `user_data/logs/`, `user_data/data/`, `user_data/backtest_results/`, `user_data/optimizer/{state.json,audit_log.jsonl,reports/}`, `*.sqlite`.

## Start

```bash
cp .env.example .env      # Werte eintragen, Datei bleibt lokal
sudo chown -R 1000:1000 user_data
sudo docker compose up -d
sudo docker compose ps
```

- Der Mount ist relativ zum Compose-Verzeichnis: `./user_data:/freqtrade/user_data`. Der Stack muss immer aus demselben Ordner gestartet werden, sonst entstehen zwei verschiedene Datenstände.
- Logs: `user_data/logs/freqtrade.log` (Bot), `user_data/optimizer/optimizer.log` (Optimizer), Reports unter `user_data/optimizer/reports/`.
- Webhook-Redeploy: `sh scripts/redeploy.sh` – macht `git fetch`/`git reset --hard origin/main`, setzt Rechte, `docker compose up -d --force-recreate --pull missing -t 30`. Kein `build`, weil fertige Images genutzt werden. Das Skript bricht ab, wenn es nicht aus `.../Projekte/freqtrade-ki-bot` läuft.
- Ohne Docker: `freqtrade trade --config user_data/config.json --strategy QuickScalpStrategy`; der Optimizer läuft mit `python user_data/optimizer/optimizer.py` und erwartet `FREQTRADE_API_URL`, `FREQTRADE_API_USER`, `FREQTRADE_API_PASS`, `FREQTRADE_CONFIG`, `STRATEGY_NAME`.

## Konfiguration

Alle Werte kommen aus `.env` (siehe `.env.example`); `docker-compose.yaml` mappt sie auf die Freqtrade- bzw. Optimizer-Variablen.

| Variable | Zweck |
|---|---|
| `TZ` | Zeitzone der Container-Logs |
| `EXCHANGE_KEY`, `EXCHANGE_SECRET` | Binance-API-Zugang → `FREQTRADE__EXCHANGE__KEY/SECRET` |
| `API_USERNAME`, `API_PASSWORD` | Login der Freqtrade-REST-API (auch vom Optimizer genutzt) |
| `JWT_SECRET_KEY`, `WS_TOKEN` | Signatur/Websocket-Token des API-Servers |
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram-Bot und Zielchat für Meldungen und Journal |
| `GEMINI_API_KEY`, `GEMINI_ON_STREAK` | LLM-Beratung; Schalter für Analyse im Streak-Pfad |
| `LLM_MODEL`, `LLM_API_URL`, `LLM_API_KEY` | Modellname bzw. OpenAI-kompatible Alternative zu Gemini |
| `OPTIMIZER_POLL_SECONDS` / `_FAST_SECONDS` | Poll-Intervall normal / bei Streak und Distress |
| `OPTIMIZER_TRADE_THRESHOLD`, `OPTIMIZER_MIN_HOURS`, `OPTIMIZER_SETTLED_HOURS` | Trigger: neue Trades und Mindestpause je Modus |
| `OPTIMIZER_DISTRESS_*` | Schwellen für den verkürzten Distress-Pfad (Trades, Winrate, Ø-Profit) |
| `OPTIMIZER_LOSE_STREAK`, `OPTIMIZER_STREAK_MIN_HOURS`, `OPTIMIZER_STREAK_SKIP_HYPEROPT` | Verlustserien-Trigger und Pause |
| `OPTIMIZER_RUN_ON_START` | sofortiger Zyklus beim Start statt Warten auf neue Trades |
| `JOURNAL_ENABLED`, `JOURNAL_TELEGRAM_BATCH`, `JOURNAL_LOOKBACK` | Journal an/aus, Batch-Größe, Abrufmenge |
| `JOURNAL_FILE`, `JOURNAL_JSONL` | Ziele für Markdown- und JSONL-Journal |

Zusätzlich liest der Optimizer Variablen, die nur im Compose-File mit Defaults gesetzt sind: `LOOKBACK_DAYS`, `HOLDOUT_DAYS`, `HYPEROPT_EPOCHS`, `HYPEROPT_LOSS`, `HYPEROPT_JOBS`, `MIN_HOLDOUT_TRADES`, `IMPROVEMENT_THRESHOLD`, `MAX_DD_RATIO`, `PLATEAU_KEEP_STREAK`, `LIVE_TRADE_LOOKBACK`, `GEMINI_ADVISOR`, `WAKE_WINRATE_DROP`.

Die Handelsseite steckt in `user_data/config.json`: Spot, Binance, `dry_run: true`, Wallet 1000 USDT, 15 USDT je Trade, max. 8 offene Trades, 1m, VolumePairList über 60 Paare mit Age-/Price-/Volatility-/Spread-Filter, leere `pair_whitelist` und mehrere Blacklists (Stablecoins, Leveraged Token).

## Sicherheitshinweis

- `.env` ist per `.gitignore` ausgeschlossen; `.env.example` enthält nur Namen und Kommentare. Keys gehören ausschließlich in `.env` auf dem Zielhost.
- `user_data/config.json` liegt mit Telegram-Token und Chat-ID im Repo; im Betrieb werden die Geheimnisse über `FREQTRADE__TELEGRAM__*`, `FREQTRADE__API_SERVER__*` und `FREQTRADE__EXCHANGE__*` aus der Umgebung übersetzt. Die Werte in der Datei sollten leer bleiben, sonst stehen sie in der Git-Historie.
- Der API-Server lauscht auf `0.0.0.0:8080`, ist aber nur im Docker-Netz und über den Cloudflare-Tunnel erreichbar; `enable_openapi` ist aus. Passwort, JWT und WS-Token kommen aus `.env`, nicht aus dem Repo.
- `dry_run: true` ist der Standard. Bei `dry_run: false` handelt der Bot echtes Geld auf Binance-Spot; die Strategie arbeitet mit Market-Orders, engen ROI-Zielen und hartem Stop bei −2,2 %. Verluste bis zur Stop-Höhe je Trade sind möglich, ebenso Fehlfüllungen bei illiquiden Paaren.
- `force_entry_enable: true` erlaubt manuelle Force-Entries über API/Telegram – dieser Pfad umgeht die Entry-Filter der Strategie.
- Der Optimizer ist durch Bounds, `max_delta`, Hold-out-Prüfung und `.bak`-Rollback begrenzt, ersetzt aber keine Validierung: Backtest-Optimierung ist keine Garantie für Live-Ergebnisse. Das LLM liefert nur Parameter-Vorschläge, keinen ausführbaren Code.

## Hinweise

- `FreqaiExampleStrategy.py` ist eine ungenutzte Beispielstrategie; `config.json` setzt `strategy` auf `QuickScalpStrategy` und enthält keinen `freqai`-Block.
- Parameternamen in `.env.example` (`OPTIMIZER_*`) und im Optimizer (`TRADE_THRESHOLD`, `POLL_INTERVAL_SECONDS`, …) unterscheiden sich; die Zuordnung passiert ausschließlich im `environment`-Block des Compose-Files. Ein direkter Python-Start braucht daher die Optimizer-Namen.
- Der Optimizer erwartet das Verzeichnis `user_data/optimizer` im Containerpfad; `trade_journal` wird als Modul aus demselben Ordner importiert.
- `redeploy.sh` benötigt Docker-Zugriff (ggf. via `sudo -n`) und eine vorhandene `.env`, `docker-compose.yaml` und `user_data/config.json`, sonst bricht es mit Fehler ab.
- Der erste Optimizer-Lauf setzt eine Baseline (geschlossene Trades, Journal-Import) und wartet danach auf neue Trades; ohne `OPTIMIZER_RUN_ON_START=true` passiert beim Start nichts.
