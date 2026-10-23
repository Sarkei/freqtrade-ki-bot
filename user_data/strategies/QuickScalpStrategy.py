# --- QuickScalpStrategy.py ---
# Ziel: hohe Winrate mit kleinen Gewinnen – KEINE Chase-Entries in Dumps.
# Entry nur: Pullback-Bounce im lokalen + BTC-Uptrend (5m).
# Momentum-Chase bewusst aus (hat 12 parallele Minus-Trades erzeugt).
# Optimizer: nur Nudges in QuickScalpStrategy.json

from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IStrategy, IntParameter, informative


class QuickScalpStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1m"
    can_short = False
    # Informative 5m braucht mehr History
    startup_candle_count = 120

    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    minimal_roi = {
        "0": 0.0045,
        "4": 0.0035,
        "10": 0.0028,
        "25": 0.0022,
    }

    stoploss = -0.022

    trailing_stop = False
    trailing_only_offset_is_reached = True

    use_custom_stoploss = True
    # Nur geschlossene Kerzen – kein Mid-Candle-Chase
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = True
    ignore_roi_if_entry_signal = False

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 3},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 90,
                "trade_limit": 2,
                "stop_duration_candles": 30,
                "only_per_pair": True,
                "required_profit": 0.0,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 120,
                "trade_limit": 8,
                "stop_duration_candles": 45,
                "max_allowed_drawdown": 0.08,
            },
        ]

    ema_fast_length = IntParameter(5, 12, default=8, space="buy", optimize=True, load=True)
    ema_slow_length = IntParameter(14, 30, default=21, space="buy", optimize=True, load=True)

    rsi_length = IntParameter(6, 14, default=10, space="buy", optimize=True, load=True)
    rsi_buy_floor = IntParameter(35, 50, default=42, space="buy", optimize=True, load=True)
    rsi_buy_threshold = IntParameter(50, 65, default=56, space="buy", optimize=True, load=True)
    rsi_sell_threshold = IntParameter(70, 90, default=78, space="sell", optimize=True, load=True)

    volume_factor = DecimalParameter(
        0.8, 2.0, default=1.15, decimals=2, space="buy", optimize=True, load=True
    )
    atr_multiplier = DecimalParameter(
        1.2, 3.0, default=2.0, decimals=2, space="sell", optimize=True, load=True
    )
    quick_tp = DecimalParameter(
        0.0028, 0.006, default=0.0038, decimals=4, space="sell", optimize=True, load=True
    )

    @informative("5m", "BTC/USDT")
    def populate_indicators_btc_5m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=12)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=26)
        return dataframe

    @informative("5m")
    def populate_indicators_5m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        fast = int(self.ema_fast_length.value)
        slow = max(int(self.ema_slow_length.value), fast + 5)

        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=fast)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=slow)
        dataframe["ema_micro"] = ta.EMA(dataframe, timeperiod=5)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=int(self.rsi_length.value))
        dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["body"] = (dataframe["close"] - dataframe["open"]).abs()
        # Mikro-Struktur: Falling-Knife erkennen
        dataframe["lower_low"] = dataframe["low"] < dataframe["low"].shift(1)
        dataframe["red"] = dataframe["close"] < dataframe["open"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Nur Pullback-Bounce – kein Momentum-Chase.
        Zusätzlich: BTC 5m uptrend + Pair 5m über EMA20 + kein Dump.
        """
        uptrend = dataframe["ema_fast"] > dataframe["ema_slow"]
        trend_rising = (
            (dataframe["ema_fast"] > dataframe["ema_fast"].shift(1))
            & (dataframe["ema_fast"].shift(1) >= dataframe["ema_fast"].shift(2))
        )

        # BTC-Marktfilter (Informative-Spalten)
        btc_up = (
            (dataframe["ema_fast_5m_btc_usdt"] > dataframe["ema_slow_5m_btc_usdt"])
            & (dataframe["close_5m_btc_usdt"] > dataframe["ema_fast_5m_btc_usdt"])
        )
        # Pair auf 5m nicht im Abwärtstrend
        pair_5m_ok = dataframe["close_5m"] >= dataframe["ema20_5m"]

        # Kein Falling Knife: nicht 2 rote Lower-Lows hintereinander
        not_dumping = ~(
            dataframe["red"]
            & dataframe["red"].shift(1)
            & dataframe["lower_low"]
            & dataframe["lower_low"].shift(1)
        )
        # Kurzfristig nicht weiter einbrechen
        not_cascade = dataframe["close"] >= dataframe["close"].shift(3)

        near_ema = dataframe["low"] <= dataframe["ema_fast"] * 1.004
        not_extended = dataframe["close"] <= dataframe["ema_fast"] * 1.008

        bounce = dataframe["close"] > dataframe["open"]
        # Vorherige Kerze war Pullback (rot oder unter EMA) → dann Bounce
        prior_pull = (dataframe["close"].shift(1) < dataframe["open"].shift(1)) | (
            dataframe["low"].shift(1) <= dataframe["ema_fast"].shift(1) * 1.002
        )
        reclaim = dataframe["close"] >= dataframe["ema_micro"]
        strong_close = dataframe["close"] >= (
            dataframe["low"] + 0.58 * (dataframe["high"] - dataframe["low"])
        )

        rsi_pullback = (dataframe["rsi"] >= self.rsi_buy_floor.value) & (
            dataframe["rsi"] <= self.rsi_buy_threshold.value
        )
        vol_ok = dataframe["volume"] > dataframe["volume_mean"] * self.volume_factor.value
        atr_ok = dataframe["atr"] > (dataframe["close"] * 0.0012)
        body_ok = dataframe["body"] > (dataframe["atr"] * 0.08)

        pullback = (
            uptrend
            & trend_rising
            & btc_up
            & pair_5m_ok
            & not_dumping
            & not_cascade
            & near_ema
            & not_extended
            & bounce
            & prior_pull
            & reclaim
            & strong_close
            & rsi_pullback
            & vol_ok
            & atr_ok
            & body_ok
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[pullback, "enter_long"] = 1
        dataframe.loc[pullback, "enter_tag"] = "pullback"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        down_cross = (dataframe["ema_fast"] < dataframe["ema_slow"]) & (
            dataframe["ema_fast"].shift(1) >= dataframe["ema_slow"].shift(1)
        )
        rsi_hot = dataframe["rsi"] > self.rsi_sell_threshold.value
        fade = (
            (dataframe["close"] < dataframe["ema_fast"])
            & (dataframe["close"] < dataframe["open"])
            & (dataframe["rsi"] < dataframe["rsi"].shift(1))
        )

        dataframe.loc[
            (down_cross | rsi_hot | fade) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        if current_profit >= float(self.quick_tp.value):
            return "quick_tp"
        if trade.open_date_utc is not None:
            minutes = (current_time - trade.open_date_utc).total_seconds() / 60.0
            if minutes >= 5 and current_profit >= 0.0032:
                return "time_tp"
            if minutes >= 12 and current_profit >= 0.0022:
                return "late_tp"
            # Früher schneiden wenn Entry falsch war (weniger -2.5% Stops)
            if minutes >= 20 and current_profit <= -0.006:
                return "stale_cut"
            if minutes >= 35 and current_profit <= -0.003:
                return "stale_cut"
            if minutes >= 60 and current_profit < 0.0015:
                return "stale_flat"
        return None

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> float:
        if current_profit >= 0.0045:
            return -0.0005
        if current_profit >= 0.0035:
            return -0.001
        return self.stoploss
