import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy, merge_informative_pair
from freqtrade.persistence import Trade

logger = logging.getLogger(__name__)


class FreqaiExampleStrategy(IStrategy):
    """
    ULTIMATIVE FreqAI Strategie (v2 - korrigiert & verkabelt)
    Marktfilter + dynamischer Chandelier-Stop + Smart Stake Sizing + Protections
    + korrekt angeschlossene FreqAI-Pipeline (Feature-Engineering, Targets, Prediction).

    WICHTIG für den Betrieb:
    - In der config.json muss "freqaimodel": "LightGBMRegressor" gesetzt sein
      (oder beim Start per --freqaimodel LightGBMRegressor übergeben werden),
      sonst weiß Freqtrade nicht, welches Modell es trainieren soll.
    - Vor dem ersten Start genug Historie laden, z.B.:
      freqtrade download-data --timeframes 5m 15m 1h --timerange 20250101- \
        --pairs .*  (bzw. deine Pairlist)
      FreqAI braucht train_period_days (aktuell 180) an Historie, sonst
      bricht das erste Training ab.
    """

    INTERFACE_VERSION = 3

    # --- 1. GRUND-EINSTELLUNGEN ---
    timeframe = "5m"
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count: int = 200  # großzügig, wegen BTC-SMA50 auf 1h + Shifted Candles

    # --- PROTECTIONS (SCHUTZSCHILD GEGEN PANIK & REVENGE-TRADING) ---
    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration": 60
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 72,
                "trade_limit": 3,
                "stop_duration": 240,
                "only_per_pair": True
            },
            {
                # Schaltet den Bot komplett ab, wenn das Gesamt-Portfolio
                # in kurzer Zeit zu stark abrutscht - Notbremse gegen Blow-up.
                "method": "MaxDrawdown",
                "lookback_period_candles": 288,  # ~24h bei 5m
                "trade_limit": 5,
                "stop_duration_candles": 12,
                "max_allowed_drawdown": 0.15
            }
        ]

    # --- 2. PROFIT & STOP-LOSS SETUP ---
    minimal_roi = {
        "0": 0.05,     # 5% Gewinn sofort mitnehmen
        "20": 0.03,    # nach 20 Min reichen 3%
        "40": 0.015,   # nach 40 Min reichen 1.5%
        "90": 0.0,     # nach 90 Min: Break-even reicht (Trade nicht ewig offen halten)
    }

    use_custom_stoploss = True
    stoploss = -0.10  # absolutes Sicherheitsnetz, falls custom_stoploss mal ausfällt
    trailing_stop = False  # bewusst aus - der Chandelier-Custom-Stoploss übernimmt das Trailing

    # --- 3. FREQAI SETUP ---
    entry_target = 0.0025  # KI muss mind. +0.25% erwarteten Gewinn im Label-Zeitraum sehen

    def informative_pairs(self):
        """
        Der "Bitcoin-Wachhund": zieht sich BTC-Daten (1h), um den Gesamtmarkt zu filtern.
        Das ist eine deterministische Regel, keine ML-Feature - läuft also immer,
        auch bevor/ohne dass die KI trainiert ist.
        """
        return [("BTC/USDT", "1h")]

    # =========================================================================
    # FREQAI FEATURE ENGINEERING
    # Alles mit "%-" Prefix ist ein KI-Feature. FreqAI multipliziert diese
    # Features automatisch über indicator_periods_candles, include_timeframes,
    # include_shifted_candles und include_corr_pairlist aus der config.json.
    # =========================================================================

    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int,
                                        metadata: dict, **kwargs) -> DataFrame:
        """
        Wird für jeden Wert in indicator_periods_candles (aktuell [10, 20]) aufgerufen
        und automatisch über alle include_timeframes / corr_pairs / shifted_candles
        vervielfacht. Hier kommen die "Standard-Handwerkszeug"-Indikatoren rein.
        """
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-mfi-period"] = ta.MFI(dataframe, timeperiod=period)
        dataframe["%-adx-period"] = ta.ADX(dataframe, timeperiod=period)
        dataframe["%-sma-period"] = ta.SMA(dataframe, timeperiod=period)
        dataframe["%-ema-period"] = ta.EMA(dataframe, timeperiod=period)
        dataframe["%-roc-period"] = ta.ROC(dataframe, timeperiod=period)

        dataframe["%-atr-period"] = ta.ATR(dataframe, timeperiod=period)
        dataframe["%-atr_pct-period"] = dataframe["%-atr-period"] / dataframe["close"]

        bollinger = ta.BBANDS(dataframe, timeperiod=period, nbdevup=2.0, nbdevdn=2.0)
        dataframe["%-bb_width-period"] = (
            (bollinger["upperband"] - bollinger["lowerband"]) / dataframe["close"]
        )
        dataframe["%-close_bb_position-period"] = (
            (dataframe["close"] - bollinger["lowerband"])
            / (bollinger["upperband"] - bollinger["lowerband"])
        )

        dataframe["%-relative_volume-period"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        )

        return dataframe

    def feature_engineering_expand_basic(self, dataframe: DataFrame, metadata: dict,
                                          **kwargs) -> DataFrame:
        """
        Wird pro Timeframe/Pair einmal aufgerufen (nicht über periods vervielfacht).
        Für Features, die keinen "period"-Parameter brauchen.
        """
        macd = ta.MACD(dataframe)
        dataframe["%-macd"] = macd["macd"]
        dataframe["%-macdsignal"] = macd["macdsignal"]
        dataframe["%-macdhist"] = macd["macdhist"]

        dataframe["%-pct-change"] = dataframe["close"].pct_change()
        dataframe["%-raw_volume"] = dataframe["volume"]
        dataframe["%-raw_price"] = dataframe["close"]

        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict,
                                      **kwargs) -> DataFrame:
        """
        Läuft nur einmal auf dem Basis-Timeframe. Gut für Zeit-/Kalender-Features.
        """
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        """
        Das Ziel, das die KI lernen soll: die durchschnittliche prozentuale
        Preisbewegung über die nächsten `label_period_candles` Kerzen (aktuell 20 x 5m = ~100 Min).
        Positiver Wert = KI erwartet steigenden Kurs.
        """
        label_period = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-target"] = (
            dataframe["close"]
            .shift(-label_period)
            .rolling(label_period)
            .mean()
            / dataframe["close"]
            - 1
        )
        return dataframe

    # =========================================================================
    # STANDARD STRATEGY HOOKS
    # =========================================================================

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self.freqai_info = self.config["freqai"]

        # --- FreqAI-Pipeline anwerfen ---
        # Berechnet alle %-Features, trainiert/lädt das Modell, erzeugt &-target
        # sowie do_predict und DI_values im dataframe.
        dataframe = self.freqai.start(dataframe, metadata, self)

        # --- Deterministischer Bitcoin-Markt-Filter (keine ML, harte Regel) ---
        pair = metadata.get("pair", "")
        if pair != "BTC/USDT":
            informative = self.dp.get_pair_dataframe(pair="BTC/USDT", timeframe="1h")
            informative["btc_sma_50"] = ta.SMA(informative, timeperiod=50)
            informative["btc_trend"] = np.where(
                informative["close"] > informative["btc_sma_50"], 1, -1
            )
            dataframe = merge_informative_pair(dataframe, informative, self.timeframe, "1h", ffill=True)
        else:
            dataframe["btc_trend_1h"] = 1

        # --- Deterministische Indikatoren für Entry-Filter & Custom-Stoploss ---
        # (bewusst getrennt von den %-ML-Features, damit sie unabhängig vom Modell
        # immer zuverlässig zur Verfügung stehen)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["volume_sma"] = ta.SMA(dataframe["volume"], timeperiod=20)
        # höchster Preis der letzten 20 Kerzen -> Basis für Chandelier-Exit
        dataframe["rolling_high"] = dataframe["high"].rolling(20).max()

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Kaufsignal: nur wenn KI zustimmt UND der Markt strukturell stabil ist.
        """
        conditions = [
            # KI muss die Marktlage als bekannt/sicher einstufen (kein Daten-Outlier)
            dataframe["do_predict"] == 1,
            # KI-Prognose muss über unserem Mindest-Erwartungswert liegen
            dataframe["&-target"] > self.entry_target,
            # Bitcoin-Wachhund: nicht kaufen, wenn der Gesamtmarkt crasht
            dataframe["btc_trend_1h"] > 0,
            # Trendstärke-Filter: keine plan- und trendlosen Seitwärtsphasen traden
            dataframe["adx"] > 20,
            # Volumen-Filter: nur bei überdurchschnittlichem Volumen (Liquidität/Interesse)
            dataframe["volume"] > dataframe["volume_sma"],
        ]

        dataframe.loc[np.bitwise_and.reduce(conditions), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Verkaufssignal der KI - dreht die Prognose ins Negative, raus.
        """
        conditions = [
            dataframe["do_predict"] == 1,
            dataframe["&-target"] < 0,
        ]

        dataframe.loc[np.bitwise_and.reduce(conditions), "exit_long"] = 1
        return dataframe

    def custom_stoploss(self, pair: str, trade: "Trade", current_time: datetime,
                         current_rate: float, current_profit: float, **kwargs) -> float:
        """
        DYNAMISCHER CHANDELIER-STOPLOSS
        Basis-Breite richtet sich nach der aktuellen Volatilität (ATR). Zusätzlich
        wird der Stop enger gezogen, sobald der Trade im Plus ist, um Gewinne
        abzusichern statt sie durch normales Rauschen wieder herzugeben.
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) == 0:
            return -0.05

        last_candle = dataframe.iloc[-1].squeeze()
        if "atr_pct" not in last_candle or pd.isna(last_candle["atr_pct"]):
            return -0.05

        # Basis-Stop: 3x ATR, begrenzt auf 2%-10%
        dynamic_stop = last_candle["atr_pct"] * 3.0
        dynamic_stop = max(min(dynamic_stop, 0.10), 0.02)

        if current_profit > 0.02:
            # Ab +2% Gewinn: Stop auf 60% der ATR-Breite verengen, um Gewinn zu sichern.
            # (Freqtrade lässt den Stop ohnehin nie wieder aufweiten, das hier macht
            # das Verengen aber explizit statt es dem Zufall zu überlassen.)
            dynamic_stop = max(dynamic_stop * 0.6, 0.01)

        return -dynamic_stop

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: Optional[float], max_stake: float,
        leverage: float, entry_tag: Optional[str], side: str, **kwargs
    ) -> float:
        """
        SMARTE POSITIONSGRÖSSEN-STEUERUNG
        Höhere Einsätze bei starkem KI-Signal, niedrigere Einsätze bei hoher Volatilität.
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) == 0:
            return proposed_stake

        last_candle = dataframe.iloc[-1].squeeze()
        stake_factor = 1.0

        # 1. KI-Signalstärke (Vertrauens-Bonus)
        if "&-target" in last_candle and not pd.isna(last_candle["&-target"]):
            target_val = last_candle["&-target"]
            if target_val > self.entry_target:
                signal_strength = target_val / self.entry_target
                confidence_bonus = min((signal_strength - 1.0) * 0.2, 0.40)
                stake_factor += max(confidence_bonus, 0.0)

        # 2. Risiko-Bremse (Volatilität)
        if "atr_pct" in last_candle and not pd.isna(last_candle["atr_pct"]):
            atr_pct = last_candle["atr_pct"]
            if atr_pct > 0.02:
                stake_factor *= 0.6
            elif atr_pct > 0.01:
                stake_factor *= 0.8
            elif atr_pct < 0.003:
                stake_factor *= 1.2

        final_stake = proposed_stake * stake_factor

        # 3. Sicherheits-Limits: nie mehr als 30% des für diesen Trade erlaubten Maximums
        if max_stake:
            final_stake = min(final_stake, max_stake * 0.30)

        if min_stake is not None:
            final_stake = max(final_stake, min_stake)

        logger.info(
            f"[{pair}] Smart Stake: {final_stake:.2f} USDT "
            f"(Standard: {proposed_stake:.2f} USDT, Multiplikator: {stake_factor:.2f}x)"
        )

        return final_stake