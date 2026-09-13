"""Daily, same-resolution port of Fear And Greed Indicator [CC].

Copyright (c) 2019-present, Franklin Moormann (cheatcountry).
Original Pine script distributed under MIT; see docs/licenses/fear-greed-MIT.txt.
The supplied default rep=false shifts price and TR by one chart bar.
No cross-timeframe security() emulation or live-bar estimate is provided.
"""
from typing import Any

import numpy as np
import pandas as pd


def calculate_fear_greed(df: pd.DataFrame) -> dict[str, Any]:
    """Return dated 10/30/2 FGI facts; never convert missing data to neutral."""
    output = {"status": "unavailable", "timeframe": "1D", "parameters": [10, 30, 2],
              "repaint": False, "lag_bars": 1, "reason": "insufficient_or_invalid_daily_bars"}
    if df is None or len(df) < 35 or not {"date", "high", "low", "close"}.issubset(df.columns):
        return output
    bars = df.copy()
    dates = pd.to_datetime(bars["date"], errors="coerce")
    if dates.isna().any() or dates.duplicated().any():
        return output
    bars = bars.assign(date=dates).sort_values("date").reset_index(drop=True)
    # The latest bar is deliberately excluded, including any intraday quote overlay.
    used = bars.iloc[:-1].tail(34)
    prices = used[["high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if (not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any()
            or (prices.high < prices.low).any()
            or (prices.close > prices.high).any() or (prices.close < prices.low).any()):
        return output
    close = prices.close
    previous_close = close.shift(1)
    tr = pd.concat([prices.high - prices.low, (prices.high - previous_close).abs(),
                    (prices.low - previous_close).abs()], axis=1).max(axis=1, skipna=False)
    up = tr.where(close > previous_close, 0.0)
    down = tr.where(close < previous_close, 0.0)

    def wma(series, length):
        weights = np.arange(1, length + 1, dtype=float)
        return series.rolling(length).apply(lambda values: np.dot(values, weights) / weights.sum(), raw=True)

    fgi = wma(wma(up, 10) - wma(down, 10) - wma(up, 30) + wma(down, 30), 2)
    current, previous = float(fgi.iloc[-1]), float(fgi.iloc[-2])
    if not np.isfinite([current, previous]).all():
        return output
    # Pine crossover(sig, 0) includes zero -> positive; do not alert every green bar.
    event = "bullish_cross" if current > 0 and previous <= 0 else (
        "bearish_cross" if current < 0 and previous >= 0 else "none")
    output.update(status="available", reason=None, value=current, previous_value=previous,
                  state="greed" if current > 0 else "fear" if current < 0 else "neutral",
                  event=event, source_bar_date=used.date.iloc[-1].strftime("%Y-%m-%d"),
                  display_bar_date=bars.date.iloc[-1].strftime("%Y-%m-%d"))
    return output


def attach_fear_greed(result, trend_result) -> None:
    """Persist computed facts after LLM output, shared by traditional and agent paths."""
    facts = getattr(trend_result, "fear_greed", None)
    if not isinstance(facts, dict) or not facts:
        facts = {"status": "unavailable", "reason": "missing_trend_data"}
    if not isinstance(getattr(result, "dashboard", None), dict):
        result.dashboard = {}
    result.dashboard["fear_greed"] = dict(facts)


def render_fear_greed(facts, language="zh") -> str:
    """Render deterministic facts, never an AI-generated FGI or trade instruction."""
    if not isinstance(facts, dict) or not facts:
        return ""
    if facts.get("status") != "available":
        return "FGI [CC]：日线数据不足或无效，未生成信号。" if language == "zh" else "FGI [CC]: unavailable; insufficient or invalid daily bars."
    if language != "zh":
        return (f"FGI [CC] (1D, 10/30/2): {facts['value']:.6f}; {facts['state']}; event: {facts['event']}. "
                f"Source bar: {facts['source_bar_date']}; display bar: {facts['display_bar_date']}; lag: 1 bar. "
                "Price-derived auxiliary indicator, not a 0–100 sentiment index or a trade instruction.")
    state = {"greed": "偏多（绿柱）", "fear": "偏空（红柱）", "neutral": "中性（零轴）"}[facts["state"]]
    event = {"bullish_cross": "上穿零轴", "bearish_cross": "下穿零轴", "none": "无新转向"}[facts["event"]]
    return (f"FGI [CC]（日线，10/30/2）：{facts['value']:.6f}；{state}；{event}。"
            f"数据截至 {facts['source_bar_date']}，对应图表柱 {facts['display_bar_date']}；不重绘模式延后一根。"
            "价格波动辅助指标，非0–100情绪指数；不能单独据此买卖，也不能补足新闻、筹码和财报缺失。")
