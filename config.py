from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    parts = [x.strip() for x in str(raw).split(",")]
    return tuple([x for x in parts if x])


def _sp500_style_liquid_alpha_extension() -> tuple[str, ...]:
    """
    Default wider Alpha universe (large-cap US equities typical of S&P 500 liquidity).
    Excludes US.VOO (CORE is handled separately). Override via TP_ALPHA_EXTENDED_SYMBOLS.
    """
    return (
        "US.JPM",
        "US.JNJ",
        "US.V",
        "US.UNH",
        "US.PG",
        "US.XOM",
        "US.CVX",
        "US.MRK",
        "US.PFE",
        "US.WMT",
        "US.COST",
        "US.PEP",
        "US.KO",
        "US.CSCO",
        "US.IBM",
        "US.ORCL",
        "US.INTC",
        "US.AMD",
        "US.COP",
        "US.SLB",
        "US.DOW",
        "US.MMM",
        "US.LOW",
        "US.TGT",
        "US.NKE",
        "US.SBUX",
        "US.MCD",
        "US.AMT",
        "US.NEE",
        "US.LMT",
        "US.RTX",
        "US.DE",
        "US.BKNG",
        "US.TMO",
        "US.ABT",
        "US.CVS",
        "US.MDT",
        "US.CI",
        "US.HUM",
        "US.GILD",
        "US.ISRG",
        "US.BMY",
        "US.SCHW",
        "US.TFC",
        "US.C",
        "US.USB",
        "US.PNC",
        "US.MET",
        "US.AIG",
        "US.ALL",
        "US.SPGI",
        "US.BLK",
        "US.SYK",
        "US.ZTS",
        "US.BDX",
        "US.ELV",
        "US.REGN",
        "US.CME",
        "US.PGR",
        "US.ECL",
        "US.SHW",
        "US.ICE",
        "US.NOC",
        "US.GD",
        "US.ADP",
        "US.CSX",
        "US.NSC",
        "US.HON",
        "US.ITW",
        "US.UPS",
        "US.FDX",
        "US.QCOM",
        "US.TXN",
        "US.AMAT",
        "US.LRCX",
        "US.KLAC",
        "US.ADI",
        "US.MU",
        "US.PANW",
        "US.CRWD",
        "US.SNOW",
        "US.NET",
        "US.DDOG",
        "US.ZS",
    )


def _alpha_extended_from_env() -> tuple[str, ...]:
    raw = os.getenv("TP_ALPHA_EXTENDED_SYMBOLS", "").strip()
    if raw:
        return tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    return _sp500_style_liquid_alpha_extension()


@dataclass(frozen=True)
class FutuConfig:
    """
    Stage-1 (quote only):
    - Connect to FutuOpenD for market data
    - Do NOT create trade contexts / do NOT place orders
    """

    host: str = os.getenv("FUTU_HOST", "127.0.0.1")
    port: int = int(os.getenv("FUTU_PORT", "11111"))


@dataclass(frozen=True)
class TradeConfig:
    """
    Live trading guardrails (manual-confirm only):
    - Always uses REAL (TrdEnv.REAL)
    - Assume trade is unlocked manually in OpenD GUI
    - Orders require explicit terminal confirmation (type YES)
    """

    trade_env: str = "REAL"  # fixed per requirement
    allow_real_trading: bool = True  # still gated by runtime "YES"

    # Risk limits
    # Prefer MAX_ORDER_VALUE_USD (V2 naming), fallback to TP_MAX_ORDER_USD (legacy)
    max_order_usd: float = float(os.getenv("MAX_ORDER_VALUE_USD", os.getenv("TP_MAX_ORDER_USD", "200")))
    min_order_qty: int = int(os.getenv("MIN_ORDER_QTY", "1"))
    limit_price_factor: float = float(os.getenv("TP_LIMIT_PRICE_FACTOR", "0.995"))

    # Universe layers for V5 trend-momentum
    core_etf_symbols: tuple[str, ...] = ("US.VOO", "US.QQQ")
    quality_tech_symbols: tuple[str, ...] = ("US.AAPL", "US.MSFT", "US.NVDA", "US.AMZN", "US.META", "US.GOOGL")
    high_vol_symbols: tuple[str, ...] = ("US.HUT", "US.MSTR", "US.STRF", "US.COIN", "US.PLTR")
    strong_exception_symbols: tuple[str, ...] = ("US.HUT", "US.MSTR", "US.STRF", "US.COIN", "US.NVDA")
    no_rsi_limit_symbols: tuple[str, ...] = ("US.NVDA", "US.HUT", "US.MSTR", "US.COIN")
    benchmark_symbol: str = os.getenv("TP_BENCHMARK_SYMBOL", "US.VOO")
    regime_symbol: str = os.getenv("TP_REGIME_SYMBOL", "US.SPY")
    core_symbol: str = os.getenv("TP_CORE_SYMBOL", "US.VOO")
    spy_symbol: str = os.getenv("TP_SPY_SYMBOL", "US.SPY")
    high_vol_alpha_symbols: tuple[str, ...] = ("US.HUT", "US.MSTR", "US.STRF", "US.COIN")
    trend_core_alpha_symbols: tuple[str, ...] = ("US.NVDA", "US.AAPL", "US.MSFT", "US.META", "US.AMZN")
    # Wider Alpha scanner list (S&P-style large caps); merged into tradable Alpha filter.
    alpha_extended_symbols: tuple[str, ...] = field(default_factory=_alpha_extended_from_env)
    # WeChat confirmation security allowlist
    # NOTE:
    # - Some wxauto-style libraries report sender as phone number; others report as wxid.
    # - Keep both to avoid rejecting valid confirmations.
    wechat_allowed_senders: tuple[str, ...] = _csv_env("WECHAT_ALLOWED_SENDERS", "15927132988,wxid_x69j8xxsmhhp22")
    wechat_allowed_chats: tuple[str, ...] = _csv_env("WECHAT_ALLOWED_CHATS", "文件传输助手")
    wechat_inbox_path: str = os.getenv("WECHAT_INBOX_PATH", os.path.join("logs", "wechat_inbox.jsonl"))

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.core_etf_symbols + self.quality_tech_symbols + self.high_vol_symbols

    @property
    def alpha_tradable_codes(self) -> frozenset[str]:
        """Symbols eligible for Alpha momentum scanning (watchlist + extensions are intersected here)."""
        return frozenset(
            str(x).strip().upper()
            for x in (
                self.high_vol_alpha_symbols + self.trend_core_alpha_symbols + self.alpha_extended_symbols
            )
            if str(x).strip()
        )


@dataclass(frozen=True)
class StrategyConfig:
    # Kline / indicators (V2 uses daily K)
    min_trading_days: int = int(os.getenv("TP_MIN_TRADING_DAYS", "250"))
    ma_windows: tuple[int, ...] = (20, 60, 200)
    rsi_period: int = int(os.getenv("TP_RSI_PERIOD", "14"))

    # V5 scoring / filtering
    buy_score_threshold: int = int(os.getenv("TP_BUY_SCORE_THRESHOLD", "60"))
    rank_top_pct_threshold: float = float(os.getenv("TP_RANK_TOP_PCT_THRESHOLD", "0.70"))
    ret63_period_days: int = int(os.getenv("TP_RET63_PERIOD_DAYS", "63"))
    poll_interval_seconds: int = int(os.getenv("TP_POLL_INTERVAL_SECONDS", "300"))
    confirm_code_expire_seconds: int = int(os.getenv("TP_CONFIRM_CODE_EXPIRE_SECONDS", "300"))
    notify_cooldown_seconds: int = int(os.getenv("TP_NOTIFY_COOLDOWN_SECONDS", "1800"))
    daily_report_local_hhmm: str = os.getenv("TP_DAILY_REPORT_LOCAL_HHMM", "22:10")
    display_timezone: str = os.getenv("TP_DISPLAY_TIMEZONE", "Europe/Berlin")

    # V5 exits / risk pause
    stop_loss_pct: float = float(os.getenv("TP_STOP_LOSS_PCT", "7.0"))
    trailing_activate_pct: float = float(os.getenv("TP_TRAILING_ACTIVATE_PCT", "10.0"))
    trailing_drawdown_pct: float = float(os.getenv("TP_TRAILING_DRAWDOWN_PCT", "12.0"))
    max_account_drawdown_pct: float = float(os.getenv("TP_MAX_ACCOUNT_DRAWDOWN_PCT", "15.0"))
    max_consecutive_loss: int = int(os.getenv("TP_MAX_CONSECUTIVE_LOSS", "3"))
    pause_new_buy_days: int = int(os.getenv("TP_PAUSE_NEW_BUY_DAYS", "7"))
    drawdown_equity_base_usd: float = float(os.getenv("TP_DRAWDOWN_EQUITY_BASE_USD", "10000"))
    reentry_breakout_lookback_days: int = int(os.getenv("TP_REENTRY_BREAKOUT_LOOKBACK_DAYS", "20"))
    reentry_min_days_since_sell: int = int(os.getenv("TP_REENTRY_MIN_DAYS_SINCE_SELL", "3"))
    min_holding_days: int = int(os.getenv("TP_MIN_HOLDING_DAYS", "10"))
    strong_rsi_upper: float = float(os.getenv("TP_STRONG_RSI_UPPER", "80.0"))
    partial_take_profit_pct: float = float(os.getenv("TP_PARTIAL_TAKE_PROFIT_PCT", "10.0"))
    partial_take_profit_ratio: float = float(os.getenv("TP_PARTIAL_TAKE_PROFIT_RATIO", "0.5"))
    core_etf_cash_pct: float = float(os.getenv("TP_CORE_ETF_CASH_PCT", "0.25"))
    quality_tech_cash_pct: float = float(os.getenv("TP_QUALITY_TECH_CASH_PCT", "0.20"))
    high_vol_cash_pct: float = float(os.getenv("TP_HIGH_VOL_CASH_PCT", "0.20"))
    v8_initial_cash_pct: float = float(os.getenv("TP_V8_INITIAL_CASH_PCT", "0.20"))
    v8_add_cash_pct: float = float(os.getenv("TP_V8_ADD_CASH_PCT", "0.20"))
    v8_max_add_count: int = int(os.getenv("TP_V8_MAX_ADD_COUNT", "2"))
    v8_breakout_lookback_days: int = int(os.getenv("TP_V8_BREAKOUT_LOOKBACK_DAYS", "20"))
    v8_chop_window_days: int = int(os.getenv("TP_V8_CHOP_WINDOW_DAYS", "10"))
    v8_chop_abs_ret_pct: float = float(os.getenv("TP_V8_CHOP_ABS_RET_PCT", "12.0"))

    # V11 portfolio allocation
    core_ratio: float = float(os.getenv("TP_CORE_RATIO", "0.70"))
    alpha_ratio: float = float(os.getenv("TP_ALPHA_RATIO", "0.30"))

    # V11 alpha sleeves
    alpha_max_holding_count: int = int(os.getenv("TP_ALPHA_MAX_HOLDING_COUNT", "3"))
    alpha_single_max_pct: float = float(os.getenv("TP_ALPHA_SINGLE_MAX_PCT", "0.40"))
    alpha_high_vol_cash_pct: float = float(os.getenv("TP_ALPHA_HIGH_VOL_CASH_PCT", "0.35"))
    alpha_trend_core_cash_pct: float = float(os.getenv("TP_ALPHA_TREND_CORE_CASH_PCT", "0.25"))
    # Aggressive defaults for HIGH_VOL (e.g., HUT/MSTR/COIN):
    # - Wider stop-loss to avoid getting shaken out
    # - Less restrictive (breakout/cooldown) so it can re-enter stronger rebounds
    alpha_high_vol_stop_loss_pct: float = float(os.getenv("TP_ALPHA_HIGH_VOL_STOP_LOSS_PCT", "12.0"))
    alpha_high_vol_trailing_pct: float = float(os.getenv("TP_ALPHA_HIGH_VOL_TRAILING_PCT", "12.0"))
    # HIGH_VOL entry hardening (to reduce whipsaw losses)
    alpha_high_vol_buy_score_threshold: int = int(os.getenv("TP_ALPHA_HIGH_VOL_BUY_SCORE_THRESHOLD", "70"))
    alpha_high_vol_require_breakout: bool = os.getenv("TP_ALPHA_HIGH_VOL_REQUIRE_BREAKOUT", "0").strip() not in (
        "0",
        "false",
        "False",
        "no",
        "NO",
    )
    alpha_high_vol_reentry_wait_days: int = int(os.getenv("TP_ALPHA_HIGH_VOL_REENTRY_WAIT_DAYS", "0"))
    alpha_trend_stop_loss_pct: float = float(os.getenv("TP_ALPHA_TREND_STOP_LOSS_PCT", "10.0"))
    alpha_trend_trailing_pct: float = float(os.getenv("TP_ALPHA_TREND_TRAILING_PCT", "20.0"))
    alpha_trend_reentry_wait_days: int = int(os.getenv("TP_ALPHA_TREND_REENTRY_WAIT_DAYS", "10"))

    # V11 CORE DCA + Dip Buy
    core_dca_interval_days: int = int(os.getenv("TP_CORE_DCA_INTERVAL_DAYS", "7"))
    core_dca_chunks: int = int(os.getenv("TP_CORE_DCA_CHUNKS", "10"))
    core_dip_level_1: float = float(os.getenv("TP_CORE_DIP_LEVEL_1", "-0.05"))
    core_dip_level_2: float = float(os.getenv("TP_CORE_DIP_LEVEL_2", "-0.10"))
    core_dip_multiplier_1: float = float(os.getenv("TP_CORE_DIP_MULTIPLIER_1", "1.5"))
    core_dip_multiplier_2: float = float(os.getenv("TP_CORE_DIP_MULTIPLIER_2", "2.0"))
    # 尚未持有 CORE 时，不向微信推送任何 CORE/VOO 买入类消息（含风险提示与待确认买单）；有持仓后恢复。设为 0 关闭。
    core_silence_push_until_holding: bool = os.getenv(
        "TP_CORE_SILENCE_PUSH_UNTIL_HOLDING", "1"
    ).strip() not in ("0", "false", "False", "no", "NO")
    # CORE (e.g., VOO) conservative exits: allow manual SELL suggestions when risk triggers.
    core_exit_enabled: bool = os.getenv("TP_CORE_EXIT_ENABLED", "1").strip() not in ("0", "false", "False", "no", "NO")
    core_stop_loss_pct: float = float(os.getenv("TP_CORE_STOP_LOSS_PCT", "15.0"))
    core_trailing_drawdown_pct: float = float(os.getenv("TP_CORE_TRAILING_DRAWDOWN_PCT", "12.0"))
    core_exit_on_ma200_break: bool = os.getenv("TP_CORE_EXIT_ON_MA200_BREAK", "1").strip() not in (
        "0",
        "false",
        "False",
        "no",
        "NO",
    )
    spy_drawdown_pause_pct: float = float(os.getenv("TP_SPY_DRAWDOWN_PAUSE_PCT", "20.0"))

    # --- Optional overlay (VIX / VOO / sentiment): only tightens ranks in ATTACK; never overrides DEFENSE ---
    risk_overlay_enabled: bool = os.getenv("TP_RISK_OVERLAY_ENABLED", "1").strip() not in ("0", "false", "False", "no", "NO")
    vix_cautious_level: float = float(os.getenv("TP_VIX_CAUTIOUS", "25"))
    vix_capital_level: float = float(os.getenv("TP_VIX_CAPITAL", "35"))
    sentiment_cautious: float = float(os.getenv("TP_SENTIMENT_CAUTIOUS", "-0.35"))
    sentiment_capital: float = float(os.getenv("TP_SENTIMENT_CAPITAL", "-0.65"))
    overlay_rank_bump_cautious: float = float(os.getenv("TP_OVERLAY_RANK_BUMP_CAUTIOUS", "0.05"))
    overlay_rank_bump_capital: float = float(os.getenv("TP_OVERLAY_RANK_BUMP_CAPITAL", "0.12"))
    # Cap total enhancement sleeve exposure (HUT/MSTR/COIN/…) vs total assets (advisory clamp in sizing path).
    alpha_enhancement_max_pct: float = float(os.getenv("TP_ALPHA_ENHANCEMENT_MAX_PCT", "10.0"))
    # Realized vol annualized cap for enhancement names (log-only / refresh filter); live uses existing score gates.
    enhancement_vol_cap_ann_pct: float = float(os.getenv("TP_ENHANCEMENT_VOL_CAP_ANN_PCT", "120.0"))

    # Buy-entry quality guardrails. These filters run after the score model and before a pending order is created.
    buy_max_ret5d_pct: float = float(os.getenv("TP_BUY_MAX_RET5D_PCT", "8.0"))
    buy_min_ret5d_pct: float = float(os.getenv("TP_BUY_MIN_RET5D_PCT", "-18.0"))
    buy_max_rsi: float = float(os.getenv("TP_BUY_MAX_RSI", "76.0"))
    buy_max_ma20_extension_pct: float = float(os.getenv("TP_BUY_MAX_MA20_EXTENSION_PCT", "12.0"))
    buy_max_ma60_extension_pct: float = float(os.getenv("TP_BUY_MAX_MA60_EXTENSION_PCT", "28.0"))
    buy_relaxed_min_rank_pct: float = float(os.getenv("TP_BUY_RELAXED_MIN_RANK_PCT", "0.70"))
    buy_relaxed_min_base_score: int = int(os.getenv("TP_BUY_RELAXED_MIN_BASE_SCORE", "70"))
    high_vol_buy_max_ret5d_pct: float = float(os.getenv("TP_HIGH_VOL_BUY_MAX_RET5D_PCT", "12.0"))
    high_vol_buy_max_ma20_extension_pct: float = float(os.getenv("TP_HIGH_VOL_BUY_MAX_MA20_EXTENSION_PCT", "18.0"))
    high_vol_buy_max_ma60_extension_pct: float = float(os.getenv("TP_HIGH_VOL_BUY_MAX_MA60_EXTENSION_PCT", "45.0"))


FUTU = FutuConfig()
STRATEGY = StrategyConfig()
TRADE = TradeConfig()

# V11 named constants (requested)
CORE_SYMBOL = TRADE.core_symbol
CORE_RATIO = STRATEGY.core_ratio
ALPHA_RATIO = STRATEGY.alpha_ratio
WECHAT_ALLOWED_SENDERS = list(TRADE.wechat_allowed_senders)

