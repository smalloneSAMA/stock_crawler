#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
T策略分析引擎 —— 日内做T + 波段做T 双维度统计

功能：
  一、日内做T策略
    统计当日内上涨幅度% / 下跌幅度% 达到各梯度(1%-9%)时，
    对应的反T / 正T 胜率。

  二、波段做T策略（5/10/20/30日区间）
    统计一段时间(5/10/20/30日)内涨跌幅达到各梯度时，
    对应的 反T / 正T 胜率，并附带技术指标分析（量能、MACD、RSI等）。
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple
import os
import zipfile
from xml.sax.saxutils import escape as xml_escape


# ════════════════════════════════════════════════════════════════
#  技术指标辅助函数（复用 stock_analyzer 的计算逻辑）
# ════════════════════════════════════════════════════════════════

def _ma(data: List[float], period: int) -> Optional[float]:
    if len(data) < period:
        return None
    return sum(data[:period]) / period


def _ema(data: List[float], period: int) -> Optional[float]:
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for i in range(period, len(data)):
        ema = data[i] * k + ema * (1 - k)
    return ema


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = 0, 0
    for i in range(period):
        diff = closes[i] - closes[i + 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def _macd_info(closes: List[float]) -> Dict:
    """计算MACD并返回方向判断"""
    dif = _ema(closes, 12)
    dea = _ema(closes, 26)
    if dif is None or dea is None:
        return {"DIF": None, "DEA": None, "柱": None, "方向": "数据不足"}
    bar = dif - dea
    if dif > dea and bar > 0:
        direction = "金叉向上"
    elif dif < dea and bar < 0:
        direction = "死叉向下"
    elif dif > dea:
        direction = "DIF在DEA上方"
    else:
        direction = "DIF在DEA下方"
    return {"DIF": round(dif, 4), "DEA": round(dea, 4), "柱": round(bar, 4), "方向": direction}


def _volume_status(volumes: List[float], idx: int, avg_period: int = 20) -> str:
    """判断成交量状态：放量 / 缩量 / 正常"""
    if len(volumes) < idx + avg_period + 1:
        return "N/A"
    current = volumes[idx]
    avg = sum(volumes[idx + 1:idx + 1 + avg_period]) / avg_period
    if avg == 0:
        return "N/A"
    ratio = current / avg
    if ratio > 1.5:
        return "放量"
    elif ratio < 0.7:
        return "缩量"
    else:
        return "正常"


def _macd_divergence(closes: List[float], idx: int, lookback: int = 30) -> str:
    """
    判断MACD顶背离/底背离
    顶背离: 价格创新高但MACD柱(或DIF)未创新高
    底背离: 价格创新低但MACD柱(或DIF)未创新低
    """
    if len(closes) < idx + lookback + 10:
        return "无"

    # 计算该段内的DIF值
    segment_closes = closes[idx:idx + lookback]
    if len(segment_closes) < 26:
        return "无"

    dif_vals = []
    for i in range(len(segment_closes)):
        seg = segment_closes[i:]
        d = _ema(seg, 12)
        de = _ema(seg, 26)
        if d is not None and de is not None:
            dif_vals.append(d - de)
        else:
            dif_vals.append(0)

    if len(dif_vals) < 10:
        return "无"

    # 当前价格和DIF
    cur_price = segment_closes[0]
    cur_dif = dif_vals[0]

    # 找过去lookback中的价格极值和对应DIF
    max_price_idx = max(range(len(segment_closes)), key=lambda i: segment_closes[i])
    min_price_idx = min(range(len(segment_closes)), key=lambda i: segment_closes[i])

    max_price = segment_closes[max_price_idx]
    min_price = segment_closes[min_price_idx]

    # 顶背离：当前价格接近或创新高，但DIF低于之前高点的DIF
    if max_price_idx > 0 and cur_price >= max_price * 0.98:
        peak_dif = dif_vals[max_price_idx]
        if cur_dif < peak_dif * 0.95:
            return "顶背离"

    # 底背离：当前价格接近或创新低，但DIF高于之前低点的DIF
    if min_price_idx > 0 and cur_price <= min_price * 1.02:
        trough_dif = dif_vals[min_price_idx]
        if cur_dif > trough_dif * 1.05:
            return "底背离"

    return "无"


# ════════════════════════════════════════════════════════════════
#  一、日内做T策略分析
# ════════════════════════════════════════════════════════════════


def _percentile(data_sorted: List[float], p: float) -> float:
    """计算百分位值"""
    if not data_sorted:
        return 0.0
    k = (len(data_sorted) - 1) * p / 100.0
    f = int(k)
    c = f + 1 if f + 1 < len(data_sorted) else f
    return data_sorted[f] + (k - f) * (data_sorted[c] - data_sorted[f])


def analyze_intraday_t(
    data: List[Dict],
    max_gradient: float = 10.0,
    step: float = 0.5,
) -> Dict:
    """
    日内做T策略分析（新版）

    第一部分：基础统计与分布
      - 总交易日、上涨日(收盘>开盘)、下跌日(收盘<开盘)
      - 上涨幅度%/下跌幅度% 的百分位、均值、最大、最小值

    第二部分：反T策略（先卖后买）
      卖价 = 开盘价 × (1 + 阈值%)
      触发条件：上涨幅度% >= 阈值%  (价格冲高到卖价)
      盈利条件：收盘价 < 卖出价  (收盘回落，买回更便宜)
      收益% = (卖出价 - 收盘价) / 开盘价 × 100

    第三部分：正T策略（先买后卖）
      买价 = 开盘价 × (1 - 阈值%)
      触发条件：下跌幅度% <= -阈值% (价格下探到买价)
      盈利条件：收盘价 > 买入价  (收盘回升，卖出更贵)
      收益% = (收盘价 - 买入价) / 开盘价 × 100

    Args:
        data: K线数据（最新在前）
        max_gradient: 最大统计梯度百分比（默认10%）
        step: 梯度步长（默认0.5%）

    Returns:
        {
            "基础统计": {...},
            "反T": {...},
            "正T": {...},
        }
    """
    total_days = len(data)
    if total_days == 0:
        return {"数据不足": True, "总交易日": 0}

    # ── 提取关键字段 ──
    opens = []
    closes = []
    up_amps = []    # 上涨幅度% = (最高-开盘)/开盘
    down_amps = []  # 下跌幅度% = (最低-开盘)/开盘
    day_chg = []    # 日内涨跌幅% = (收盘-开盘)/开盘

    for row in data:
        o = row.get("开盘价", 0) or 0
        c = row.get("收盘价", 0) or 0
        up = row.get("上涨幅度%")
        down = row.get("下跌幅度%")

        opens.append(o)
        closes.append(c)

        ua = up if (up is not None and isinstance(up, (int, float))) else 0
        da = down if (down is not None and isinstance(down, (int, float))) else 0
        up_amps.append(ua)
        down_amps.append(da)

        dc = ((c - o) / o * 100) if o > 0 else 0
        day_chg.append(dc)

    up_days = sum(1 for dc in day_chg if dc > 0)
    down_days = sum(1 for dc in day_chg if dc < 0)
    flat_days = total_days - up_days - down_days

    # ── Part 1: 基础统计与分布 ──
    up_sorted = sorted(up_amps)
    down_sorted = sorted(down_amps)  # 负值在前
    down_abs_sorted = sorted([abs(d) for d in down_amps])  # 绝对值升序

    percentiles = [5, 10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 95]
    up_pct = {p: round(_percentile(up_sorted, p), 2) for p in percentiles}
    down_pct = {p: round(_percentile(down_abs_sorted, p), 2) for p in percentiles}

    up_mean = round(sum(up_amps) / total_days, 2) if total_days > 0 else 0
    down_mean = round(sum(abs(d) for d in down_amps) / total_days, 2) if total_days > 0 else 0
    up_max = round(max(up_amps), 2) if up_amps else 0
    up_min = round(min(up_amps), 2) if up_amps else 0
    down_max = round(max(abs(d) for d in down_amps), 2) if down_amps else 0
    down_min = round(min(abs(d) for d in down_amps), 2) if down_amps else 0

    basic_stats = {
        "总交易日": total_days,
        "上涨日(收盘>开盘)": up_days,
        "下跌日(收盘<开盘)": down_days,
        "平盘日": flat_days,
        "上涨日占比%": round(up_days / total_days * 100, 1) if total_days > 0 else 0,
        "上涨幅度%": {
            "百分位": up_pct,
            "均值": up_mean,
            "最大值": up_max,
            "最小值": up_min,
        },
        "下跌幅度%(绝对值)": {
            "百分位": down_pct,
            "均值": down_mean,
            "最大值": down_max,
            "最小值": down_min,
        },
    }

    # ── 生成梯度列表（0.5 步进） ──
    gradients = []
    g = step
    while g <= max_gradient + 0.001:
        gradients.append(round(g, 2))
        g += step

    # ── Part 2: 反T策略 ──
    ft_result = {
        "梯度": gradients,
        "触发天数": [],
        "触发占比%": [],
        "盈利天数": [],
        "胜率%": [],
        "平均收益%": [],
        "累计收益%": [],
    }

    for g in gradients:
        trigger_count = 0
        win_count = 0
        total_return = 0.0

        for i in range(total_days):
            if up_amps[i] >= g:  # 上涨幅度% >= 阈值%，触发卖出
                trigger_count += 1
                # 卖出价 = 开盘 × (1 + g/100)
                # 买回价 = 收盘价
                # 实际收益率 = (卖出价 - 买回价) / 卖出价 × 100
                sell_price = opens[i] * (1 + g / 100)
                ret_pct = (sell_price - closes[i]) / sell_price * 100 if sell_price > 0 else 0
                total_return += ret_pct
                if ret_pct > 0:  # 收盘价 < 卖出价，买回更便宜
                    win_count += 1

        ft_result["触发天数"].append(trigger_count)
        ft_result["触发占比%"].append(round(trigger_count / total_days * 100, 1) if total_days > 0 else 0)
        ft_result["盈利天数"].append(win_count)
        ft_result["胜率%"].append(round(win_count / trigger_count * 100, 1) if trigger_count > 0 else 0)
        ft_result["平均收益%"].append(round(total_return / trigger_count, 2) if trigger_count > 0 else 0)
        ft_result["累计收益%"].append(round(total_return, 2))

    # ── Part 3: 正T策略 ──
    zt_result = {
        "梯度": gradients,
        "触发天数": [],
        "触发占比%": [],
        "盈利天数": [],
        "胜率%": [],
        "平均收益%": [],
        "累计收益%": [],
    }

    for g in gradients:
        trigger_count = 0
        win_count = 0
        total_return = 0.0

        for i in range(total_days):
            if down_amps[i] <= -g:  # 下跌幅度% <= -阈值%，触发买入
                trigger_count += 1
                # 买入价 = 开盘 × (1 - g/100)
                # 卖出价 = 收盘价
                # 实际收益率 = (卖出价 - 买入价) / 买入价 × 100
                buy_price = opens[i] * (1 - g / 100)
                ret_pct = (closes[i] - buy_price) / buy_price * 100 if buy_price > 0 else 0
                total_return += ret_pct
                if ret_pct > 0:  # 收盘价 > 买入价，卖出获利
                    win_count += 1

        zt_result["触发天数"].append(trigger_count)
        zt_result["触发占比%"].append(round(trigger_count / total_days * 100, 1) if total_days > 0 else 0)
        zt_result["盈利天数"].append(win_count)
        zt_result["胜率%"].append(round(win_count / trigger_count * 100, 1) if trigger_count > 0 else 0)
        zt_result["平均收益%"].append(round(total_return / trigger_count, 2) if trigger_count > 0 else 0)
        zt_result["累计收益%"].append(round(total_return, 2))

    return {
        "数据不足": False,
        "总交易日": total_days,
        "基础统计": basic_stats,
        "反T": ft_result,
        "正T": zt_result,
    }


# ════════════════════════════════════════════════════════════════
#  二、波段做T策略分析（含技术指标）
# ════════════════════════════════════════════════════════════════

# 波段周期列表
SWING_PERIODS = [5, 10, 20, 30, 90]
# 波段涨跌幅梯度阈值
SWING_THRESHOLDS = [3, 5, 8, 10, 15, 20]
# 信号后持有观察天数 = signal_lookahead_ratio × period
SIGNAL_LOOKAHEAD_RATIO = 0.5


def _build_chronological(data: List[Dict]) -> Tuple[List[Dict], List[float], List[float], List[float], List[float]]:
    """
    将数据转为时间正序（最旧在前），同时提取价格序列
    Returns: (data_asc, closes, highs, lows, volumes)
    """
    data_asc = list(reversed(data))
    closes = [row.get("收盘价", 0) or 0 for row in data_asc]
    highs = [row.get("最高价", 0) or 0 for row in data_asc]
    lows = [row.get("最低价", 0) or 0 for row in data_asc]
    volumes = [row.get("成交量(万手)", 0) or 0 for row in data_asc]
    return data_asc, closes, highs, lows, volumes


def _swing_return(closes: List[float], end_idx: int, period: int) -> float:
    """计算从 end_idx-period 到 end_idx 的区间涨跌幅(%)"""
    if end_idx < period:
        return 0.0
    prev = closes[end_idx - period]
    if prev == 0:
        return 0.0
    return (closes[end_idx] - prev) / prev * 100


def _forward_return(closes: List[float], signal_idx: int, lookahead: int) -> float:
    """信号发生后 lookahead 天的涨跌幅(%)"""
    if signal_idx + lookahead >= len(closes):
        return 0.0
    cur = closes[signal_idx]
    if cur == 0:
        return 0.0
    return (closes[signal_idx + lookahead] - cur) / cur * 100


def _compute_dynamic_thresholds(closes: List[float], period: int, n_thresholds: int = 6) -> List[int]:
    """
    根据股票自身的历史涨跌幅分布，动态计算该周期的阈值列表

    对指定 period，计算所有 N 日区间涨跌幅的绝对值，
    然后按百分位取 n_thresholds 个阈值。
    百分位点均匀分布，覆盖从常见波动到极端波动。

    Returns: 阈值列表（整数百分比），如 [2, 4, 6, 9, 12, 17]
    """
    n = len(closes)
    if n <= period:
        return [3, 5, 8, 10, 15, 20]  # 数据不足时回退到默认值

    # 计算该周期所有 N 日涨跌幅绝对值
    abs_returns = []
    for i in range(period, n):
        prev = closes[i - period]
        if prev > 0:
            ret = abs((closes[i] - prev) / prev * 100)
            abs_returns.append(ret)

    if len(abs_returns) < 20:
        return [3, 5, 8, 10, 15, 20]

    abs_returns.sort()
    total = len(abs_returns)

    # 均匀分布的百分位点，从略高于中位数的位置开始
    # 这样阈值不会太低（避免日常噪声也被算作信号）
    percentiles = [35, 50, 62, 74, 85, 93]
    thresholds = []
    for p in percentiles:
        idx = int(total * p / 100)
        idx = min(idx, total - 1)
        val = abs_returns[idx]
        # 向上取整到整数，且至少为 2
        th = max(2, int(val) + (1 if val - int(val) > 0.01 else 0))
        thresholds.append(th)

    # 去重并确保递增
    unique = []
    for t in thresholds:
        if not unique or t > unique[-1]:
            unique.append(t)
        elif t == unique[-1]:
            # 如果相同，加 1 偏移
            unique.append(t + 1)

    return unique[:n_thresholds]


def analyze_swing_t(data: List[Dict]) -> Dict:
    """
    波段做T策略分析

    对每只股票，在时间正序数据上滑动窗口：
      - 每当 N日涨幅 >= X% → 反T信号
        - 持有观察 N/2 天后，检查价格是否下跌
      - 每当 N日跌幅 >= X% → 正T信号
        - 持有观察 N/2 天后，检查价格是否上涨

    并在每个信号点记录：
      MACD方向、RSI值、成交量状态、顶/底背离

    Args:
        data: K线数据（最新在前），需含价格、成交量等字段

    Returns:
        嵌套 dict:
        {
            periods: { 5: { thresholds: { 3: {正T}/{反T}, 5: {...}, ... } }, ... },
            summary: { 各周期最优梯度等信息 }
        }
    """
    data_asc, closes, highs, lows, volumes = _build_chronological(data)
    n = len(closes)

    if n < 60:
        return {"数据不足": True, "总交易日": n}

    result = {}
    result["数据不足"] = False
    result["总交易日"] = n
    result["时间范围"] = f"{data_asc[0].get('日期','?')} ~ {data_asc[-1].get('日期','?')}"

    # 对每个周期动态计算阈值
    dynamic_thresholds = {}
    for period in SWING_PERIODS:
        dynamic_thresholds[period] = _compute_dynamic_thresholds(closes, period)
    result["_动态阈值"] = dynamic_thresholds

    for period in SWING_PERIODS:
        lookahead = max(3, int(period * SIGNAL_LOOKAHEAD_RATIO))
        period_result = {}
        thresholds = dynamic_thresholds[period]

        for threshold in thresholds:
            zt_signals = []   # 正T信号（买入）
            ft_signals = []   # 反T信号（卖出）

            # 从 period 位置开始，到 n - lookahead 结束（留出观察窗口）
            for i in range(period, n - lookahead):
                ret = _swing_return(closes, i, period)

                # ── 正T信号：N日跌幅 >= threshold% ──
                if ret <= -threshold:
                    fwd_ret = _forward_return(closes, i, lookahead)
                    # 准备技术指标（在信号点 i 处计算）
                    seg_closes = closes[:i + 1]
                    seg_volumes = volumes[:i + 1]
                    rsi_val = _rsi(list(reversed(seg_closes)), 14) if len(seg_closes) >= 15 else None
                    macd = _macd_info(list(reversed(seg_closes))) if len(seg_closes) >= 26 else {"方向": "数据不足"}
                    vol_status = _volume_status(volumes, i, 20)
                    divergence = _macd_divergence(list(reversed(seg_closes)), 0, min(30, len(seg_closes))) if len(seg_closes) >= 30 else "无"

                    zt_signals.append({
                        "日期": data_asc[i].get("日期", ""),
                        "信号时收盘价": closes[i],
                        f"{period}日涨跌幅%": round(ret, 2),
                        "信号后涨幅%": round(fwd_ret, 2),
                        "是否获胜": "是" if fwd_ret > 0 else "否",
                        "RSI": round(rsi_val, 1) if rsi_val is not None else "N/A",
                        "MACD方向": macd.get("方向", "N/A"),
                        "成交量": vol_status,
                        "背离": divergence,
                        "收盘价_信号时": closes[i],
                    })

                # ── 反T信号：N日涨幅 >= threshold% ──
                if ret >= threshold:
                    fwd_ret = _forward_return(closes, i, lookahead)
                    seg_closes = closes[:i + 1]
                    seg_volumes = volumes[:i + 1]
                    rsi_val = _rsi(list(reversed(seg_closes)), 14) if len(seg_closes) >= 15 else None
                    macd = _macd_info(list(reversed(seg_closes))) if len(seg_closes) >= 26 else {"方向": "数据不足"}
                    vol_status = _volume_status(volumes, i, 20)
                    divergence = _macd_divergence(list(reversed(seg_closes)), 0, min(30, len(seg_closes))) if len(seg_closes) >= 30 else "无"

                    ft_signals.append({
                        "日期": data_asc[i].get("日期", ""),
                        "信号时收盘价": closes[i],
                        f"{period}日涨跌幅%": round(ret, 2),
                        "信号后涨幅%": round(fwd_ret, 2),
                        "是否获胜": "是" if fwd_ret < 0 else "否",
                        "RSI": round(rsi_val, 1) if rsi_val is not None else "N/A",
                        "MACD方向": macd.get("方向", "N/A"),
                        "成交量": vol_status,
                        "背离": divergence,
                    })

            # 统计
            zt_total = len(zt_signals)
            zt_wins = sum(1 for s in zt_signals if s["是否获胜"] == "是")
            zt_rate = round(zt_wins / zt_total * 100, 1) if zt_total > 0 else 0
            zt_avg_fwd = round(sum(s["信号后涨幅%"] for s in zt_signals) / zt_total, 2) if zt_total > 0 else 0

            ft_total = len(ft_signals)
            ft_wins = sum(1 for s in ft_signals if s["是否获胜"] == "是")
            ft_rate = round(ft_wins / ft_total * 100, 1) if ft_total > 0 else 0
            ft_avg_fwd = round(sum(s["信号后涨幅%"] for s in ft_signals) / ft_total, 2) if ft_total > 0 else 0

            # ── 技术指标分布统计 ──
            zt_macd_dist = {}
            ft_macd_dist = {}
            for s in zt_signals:
                d = s.get("MACD方向", "N/A")
                zt_macd_dist[d] = zt_macd_dist.get(d, 0) + 1
            for s in ft_signals:
                d = s.get("MACD方向", "N/A")
                ft_macd_dist[d] = ft_macd_dist.get(d, 0) + 1

            # ── 量能辅助分析 ──
            # 按成交量状态分组看胜率
            zt_vol_breakdown = {}
            ft_vol_breakdown = {}
            for s in zt_signals:
                v = s.get("成交量", "N/A")
                if v not in zt_vol_breakdown:
                    zt_vol_breakdown[v] = {"count": 0, "wins": 0}
                zt_vol_breakdown[v]["count"] += 1
                if s["是否获胜"] == "是":
                    zt_vol_breakdown[v]["wins"] += 1
            for s in ft_signals:
                v = s.get("成交量", "N/A")
                if v not in ft_vol_breakdown:
                    ft_vol_breakdown[v] = {"count": 0, "wins": 0}
                ft_vol_breakdown[v]["count"] += 1
                if s["是否获胜"] == "是":
                    ft_vol_breakdown[v]["wins"] += 1

            zt_vol_rates = {}
            for v, d in zt_vol_breakdown.items():
                zt_vol_rates[v] = {
                    "次数": d["count"],
                    "胜率%": round(d["wins"] / d["count"] * 100, 1)
                }
            ft_vol_rates = {}
            for v, d in ft_vol_breakdown.items():
                ft_vol_rates[v] = {
                    "次数": d["count"],
                    "胜率%": round(d["wins"] / d["count"] * 100, 1)
                }

            # ── 背离辅助分析 ──
            zt_div_breakdown = {}
            ft_div_breakdown = {}
            for s in zt_signals:
                dv = s.get("背离", "无")
                if dv not in zt_div_breakdown:
                    zt_div_breakdown[dv] = {"count": 0, "wins": 0}
                zt_div_breakdown[dv]["count"] += 1
                if s["是否获胜"] == "是":
                    zt_div_breakdown[dv]["wins"] += 1
            for s in ft_signals:
                dv = s.get("背离", "无")
                if dv not in ft_div_breakdown:
                    ft_div_breakdown[dv] = {"count": 0, "wins": 0}
                ft_div_breakdown[dv]["count"] += 1
                if s["是否获胜"] == "是":
                    ft_div_breakdown[dv]["wins"] += 1

            zt_div_rates = {}
            for dv, d in zt_div_breakdown.items():
                zt_div_rates[dv] = {
                    "次数": d["count"],
                    "胜率%": round(d["wins"] / d["count"] * 100, 1)
                }
            ft_div_rates = {}
            for dv, d in ft_div_breakdown.items():
                ft_div_rates[dv] = {
                    "次数": d["count"],
                    "胜率%": round(d["wins"] / d["count"] * 100, 1)
                }

            period_result[f"threshold_{threshold}"] = {
                "正T": {
                    "信号次数": zt_total,
                    "获胜次数": zt_wins,
                    "胜率%": zt_rate,
                    "平均信号后收益%": zt_avg_fwd,
                    "MACD分布": zt_macd_dist,
                    "量能分布": zt_vol_rates,
                    "背离分布": zt_div_rates,
                    "信号详情": zt_signals[:10],
                },
                "反T": {
                    "信号次数": ft_total,
                    "获胜次数": ft_wins,
                    "胜率%": ft_rate,
                    "平均信号后收益%": ft_avg_fwd,
                    "MACD分布": ft_macd_dist,
                    "量能分布": ft_vol_rates,
                    "背离分布": ft_div_rates,
                    "信号详情": ft_signals[:10],
                },
            }

        result[period] = period_result

    # ── 汇总：每个周期的最佳梯度 ──
    summary_rows = []
    for period in SWING_PERIODS:
        best_zt = {"threshold": 0, "胜率": 0}
        best_ft = {"threshold": 0, "胜率": 0}
        thresholds = dynamic_thresholds[period]
        for th in thresholds:
            td = result[period][f"threshold_{th}"]
            if td["正T"]["胜率%"] > best_zt["胜率"] and td["正T"]["信号次数"] >= 3:
                best_zt = {"threshold": th, "胜率": td["正T"]["胜率%"], "信号次数": td["正T"]["信号次数"]}
            if td["反T"]["胜率%"] > best_ft["胜率"] and td["反T"]["信号次数"] >= 3:
                best_ft = {"threshold": th, "胜率": td["反T"]["胜率%"], "信号次数": td["反T"]["信号次数"]}
        summary_rows.append({
            "周期": period,
            "最佳正T阈值%": best_zt["threshold"],
            "正T胜率%": best_zt["胜率"],
            "正T信号次数": best_zt["信号次数"],
            "最佳反T阈值%": best_ft["threshold"],
            "反T胜率%": best_ft["胜率"],
            "反T信号次数": best_ft["信号次数"],
        })
    result["汇总"] = summary_rows

    return result


# ════════════════════════════════════════════════════════════════
#  三、生成 Excel 文件（多Sheet）
# ════════════════════════════════════════════════════════════════

def _col_letter(index: int) -> str:
    letter = ''
    while index > 0:
        index -= 1
        letter = chr(65 + (index % 26)) + letter
        index //= 26
    return letter


def _build_text_sheet(lines: List[str], col_width: int = 80) -> str:
    """
    构建一个纯文本说明 sheet（无表头网格，每行一段文字）
    """
    col_letter = 'A'
    row_xmls = []
    for ri, line in enumerate(lines):
        row_num = ri + 1
        if not line:
            row_xmls.append(f'<row r="{row_num}" spans="1:1"></row>')
        else:
            row_xmls.append(
                f'<row r="{row_num}" spans="1:1">'
                f'<c r="A{row_num}" s="0" t="inlineStr">'
                f'<is><t>{xml_escape(line)}</t></is></c>'
                f'</row>'
            )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetFormatPr defaultRowHeight="18"/>
  <cols><col min="1" max="1" width="{col_width}" customWidth="1"/></cols>
  <sheetData>
{chr(10).join(row_xmls)}
  </sheetData>
</worksheet>'''


def _build_sheet_xml(sheet_name: str, headers: List[str], rows: List[List], col_widths: List[int],
                     row_fills: Optional[List[int]] = None) -> str:
    """
    构建一个 sheet 的 XML

    Args:
        row_fills: 每行数据使用的填充色索引（None=默认样式 s=3）
                   fill索引4=浅红(上升)  5=浅绿(下降)
    """
    num_cols = len(headers)
    col_letters = [_col_letter(i + 1) for i in range(num_cols)]
    row_xmls = []

    # 表头行
    header_cells = ''
    for i, h in enumerate(headers):
        cl = col_letters[i]
        header_cells += (
            f'<c r="{cl}1" s="2" t="inlineStr">'
            f'<is><t>{xml_escape(str(h))}</t></is></c>'
        )
    row_xmls.append(f'<row r="1" spans="1:{num_cols}" s="2" ht="20">{header_cells}</row>')

    # 数据行
    for ri, row_data in enumerate(rows):
        row_num = ri + 2
        # 判断该行使用的样式
        if row_fills and ri < len(row_fills) and row_fills[ri] is not None:
            cell_style = row_fills[ri]  # 4=浅红(s=4), 5=浅绿(s=5)
        else:
            cell_style = 3  # 默认
        cells = ''
        for ci, val in enumerate(row_data):
            cl = col_letters[ci]
            if isinstance(val, (int, float)):
                cells += f'<c r="{cl}{row_num}" s="{cell_style}"><v>{val}</v></c>'
            else:
                cells += (
                    f'<c r="{cl}{row_num}" s="{cell_style}" t="inlineStr">'
                    f'<is><t>{xml_escape(str(val) if val is not None else "")}</t></is></c>'
                )
        row_xmls.append(f'<row r="{row_num}" spans="1:{num_cols}">{cells}</row>')

    col_xml = '\n'.join(
        f'    <col min="{i+1}" max="{i+1}" width="{col_widths[i]}" customWidth="1"/>'
        for i in range(num_cols)
    )

    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
    </sheetView>
  </sheetViews>
  <sheetFormatPr defaultRowHeight="18"/>
  <cols>
{col_xml}
  </cols>
  <sheetData>
{chr(10).join(row_xmls)}
  </sheetData>
</worksheet>'''


def generate_t_excel(intraday_result: Dict, swing_result: Dict, stock_name: str, stock_code: str, output_path: str) -> str:
    """
    生成T策略分析 Excel（多Sheet）

    Sheet1: 日内做T统计
    Sheet2: 波段做T汇总
    Sheet3+: 各周期波段做T详情
    """
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="10"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="10"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF4472C4"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9E2F3"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color auto="1"/></left>
      <right style="thin"><color auto="1"/></right>
      <top style="thin"><color auto="1"/></top>
      <bottom style="thin"><color auto="1"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
</styleSheet>'''

    sheets_xml = []
    sheet_names = []

    # ═══ Sheet0: 策略使用说明 ═══
    guide_lines = [
        f"{stock_name}({stock_code}) T策略分析报告 - 使用说明",
        "=" * 60,
        "",
        "【日内做T策略】",
        "",
        "一、反T策略（先卖后买）",
        "  操作：开盘后价格上涨超过阈值%时，以'开盘价x(1+阈值%)'挂卖单卖出，收盘时买回。",
        "  盈利条件：收盘价 < 卖出价（即日内价格冲高后回落，买回价格更低）。",
        "  适用场景：当日股价冲高回落，适合做空T赚差价。",
        "  收益计算：实际收益率 = (卖出价 - 收盘价) / 卖出价 × 100%",
        "  - 梯度%：阈值梯度，从0.5%到10%，步进0.5%",
        "  - 触发天数：历史上该梯度被触发的总天数",
        "  - 触发占比%：触发天数 / 总交易日",
        "  - 盈利天数：触发后收盘价低于卖出价的天数（即盈利）",
        "  - 胜率%：盈利天数 / 触发天数（越高说明该梯度反T越可靠）",
        "  - 平均收益%：所有触发日的平均收益率",
        "  - 累计收益%：所有触发日的累计总收益",
        "  解读：胜率越高、触发天数越多，该梯度的反T策略越值得采用。",
        "       一般推荐选择胜率>55%且触发天数>30的梯度。",
        "",
        "二、正T策略（先买后卖）",
        "  操作：开盘后价格下跌超过阈值%时，以'开盘价x(1-阈值%)'挂买单买入，收盘时卖出。",
        "  盈利条件：收盘价 > 买入价（即日内价格下跌后反弹，卖出价格更高）。",
        "  适用场景：当日股价探底回升，适合做多T赚差价。",
        "  收益计算：实际收益率 = (收盘价 - 买入价) / 买入价 × 100%",
        "  各列含义同反T策略。",
        "",
        "三、基础统计",
        "  - 总交易日：统计区间的全部交易日",
        "  - 上涨日/下跌日：收盘价高于/低于开盘价的天数",
        "  - 百分位分布：上涨幅度%/下跌幅度%在各个百分位的取值",
        "    例如：50%分位=1.37，表示一半的交易日涨幅不超过1.37%",
        "",
        "【波段做T策略】",
        "",
        "一、波段反T（卖出信号）",
        "  逻辑：统计N日内（5/10/20/30日）涨幅达到阈值%时卖出，",
        "        观察未来N/2天股价是否下跌。",
        "  胜率：下跌天数 / 总信号天数",
        "",
        "二、波段正T（买入信号）",
        "  逻辑：统计N日内（5/10/20/30日）跌幅达到阈值%时买入，",
        "        观察未来N/2天股价是否上涨。",
        "  胜率：上涨天数 / 总信号天数",
        "",
        "三、技术指标辅助",
        "  每个信号点附带：",
        "  - MACD方向：金叉向上(多头) / 死叉向下(空头)",
        "  - RSI值：超买(>70) / 超卖(<30) / 中性",
        "  - 成交量：放量(>1.5倍均量) / 缩量(<0.7倍) / 正常",
        "  - 顶/底背离：价格与MACD的背离信号",
        "  结合技术指标可提高胜率，例如：",
        "  - 反T信号+顶背离+超买RSI：胜率更高",
        "  - 正T信号+底背离+超卖RSI：胜率更高",
        "",
        "【汇总表说明】",
        "  每个周期选取胜率最高且信号次数>=3的阈值作为'最佳'。",
        "  实际使用时，应结合信号频率（触发天数）和胜率综合判断。",
        "",
        "【注意事项】",
        "  1. 以上分析基于历史数据，不保证未来表现。",
        "  2. 未考虑交易佣金、印花税等摩擦成本。",
        "  3. 反T需先有持仓或融券，正T需有可用资金。",
        "  4. 极端行情（涨停/跌停）可能导致无法成交。",
        "  5. 建议选择胜率>55%且月均触发>2次的梯度执行。",
    ]
    sheets_xml.append(_build_text_sheet(guide_lines, col_width=90))
    sheet_names.append("策略说明")

    # ═══ Sheet1: 日内基础统计 ═══
    if not intraday_result.get("数据不足", True):
        bs = intraday_result["基础统计"]
        # --- 基础统计区块 ---
        bs_headers = ["指标", "值"]
        bs_widths = [24, 12]
        bs_rows = [
            ["总交易日", bs["总交易日"]],
            ["上涨日(收盘>开盘)", bs["上涨日(收盘>开盘)"]],
            ["下跌日(收盘<开盘)", bs["下跌日(收盘<开盘)"]],
            ["平盘日", bs["平盘日"]],
            ["上涨日占比%", bs["上涨日占比%"]],
        ]
        sheets_xml.append(_build_sheet_xml("日内基础统计", bs_headers, bs_rows, bs_widths))
        sheet_names.append("日内基础统计")

        # --- 涨幅/跌幅分布区块（百分位表） ---
        up_pct = bs["上涨幅度%"]["百分位"]
        down_pct = bs["下跌幅度%(绝对值)"]["百分位"]
        pct_headers = ["百分位", "上涨幅度%", "下跌幅度%(绝对值)"]
        pct_widths = [10, 14, 18]
        pct_rows = []
        for p in sorted(up_pct.keys()):
            pct_rows.append([f"{p}%", up_pct[p], down_pct.get(p, "")])
        # 追加均值/最大/最小
        pct_rows.append(["均值", bs["上涨幅度%"]["均值"], bs["下跌幅度%(绝对值)"]["均值"]])
        pct_rows.append(["最大值", bs["上涨幅度%"]["最大值"], bs["下跌幅度%(绝对值)"]["最大值"]])
        pct_rows.append(["最小值", bs["上涨幅度%"]["最小值"], bs["下跌幅度%(绝对值)"]["最小值"]])
        sheets_xml.append(_build_sheet_xml("日内分布统计", pct_headers, pct_rows, pct_widths))
        sheet_names.append("日内分布统计")

        # ═══ Sheet: 反T策略 ═══
        ft = intraday_result["反T"]
        ft_headers = ["梯度%", "触发天数", "触发占比%", "盈利天数", "胜率%", "平均收益%", "累计收益%"]
        ft_widths = [8, 10, 10, 10, 8, 12, 12]
        ft_rows = []
        for i, g in enumerate(ft["梯度"]):
            ft_rows.append([
                g,
                ft["触发天数"][i],
                ft["触发占比%"][i],
                ft["盈利天数"][i],
                ft["胜率%"][i],
                ft["平均收益%"][i],
                ft["累计收益%"][i],
            ])
        sheets_xml.append(_build_sheet_xml("日内反T策略", ft_headers, ft_rows, ft_widths))
        sheet_names.append("日内反T策略")

        # ═══ Sheet: 正T策略 ═══
        zt = intraday_result["正T"]
        zt_headers = ["梯度%", "触发天数", "触发占比%", "盈利天数", "胜率%", "平均收益%", "累计收益%"]
        zt_widths = [8, 10, 10, 10, 8, 12, 12]
        zt_rows = []
        for i, g in enumerate(zt["梯度"]):
            zt_rows.append([
                g,
                zt["触发天数"][i],
                zt["触发占比%"][i],
                zt["盈利天数"][i],
                zt["胜率%"][i],
                zt["平均收益%"][i],
                zt["累计收益%"][i],
            ])
        sheets_xml.append(_build_sheet_xml("日内正T策略", zt_headers, zt_rows, zt_widths))
        sheet_names.append("日内正T策略")
    else:
        sheets_xml.append(_build_sheet_xml("日内基础统计", ["提示"], [["数据不足"]], [20]))
        sheet_names.append("日内基础统计")

    # ═══ Sheet2: 波段做T汇总 ═══
    if not swing_result.get("数据不足", True):
        summary = swing_result.get("汇总", [])
        headers2 = ["周期(日)", "最佳正T阈值%", "正T胜率%", "正T信号次数", "最佳反T阈值%", "反T胜率%", "反T信号次数"]
        widths2 = [10, 14, 10, 12, 14, 10, 12]
        rows2 = []
        for s in summary:
            rows2.append([s["周期"], s["最佳正T阈值%"], s["正T胜率%"], s["正T信号次数"],
                          s["最佳反T阈值%"], s["反T胜率%"], s["反T信号次数"]])
        sheets_xml.append(_build_sheet_xml("波段做T汇总", headers2, rows2, widths2))
        sheet_names.append("波段做T汇总")

        # ═══ Sheet3+: 各周期波段做T详情 ═══
        dyn_thresholds_all = swing_result.get("_动态阈值", {})
        for period in SWING_PERIODS:
            period_data = swing_result.get(period, {})
            detail_headers = ["阈值%", "方向", "信号次数", "获胜次数", "胜率%", "平均信号后收益%"]
            detail_widths = [8, 8, 10, 10, 8, 16]
            detail_rows = []
            period_ths = dyn_thresholds_all.get(period, SWING_THRESHOLDS)
            for th in period_ths:
                td = period_data.get(f"threshold_{th}", {})
                for direction in ["正T", "反T"]:
                    dd = td.get(direction, {})
                    detail_rows.append([
                        th,
                        direction,
                        dd.get("信号次数", 0),
                        dd.get("获胜次数", 0),
                        dd.get("胜率%", 0),
                        dd.get("平均信号后收益%", 0),
                    ])
            sheets_xml.append(_build_sheet_xml(f"波段{period}日", detail_headers, detail_rows, detail_widths))
            sheet_names.append(f"波段{period}日")

        # ═══ 量能分析：按成交量状态分组看胜率 ═══
        vol_rows = []
        vol_headers = ["周期", "方向", "阈值%", "量能状态", "次数", "胜率%"]
        vol_widths = [8, 8, 8, 10, 8, 8]
        for period in SWING_PERIODS:
            period_data = swing_result.get(period, {})
            period_ths = dyn_thresholds_all.get(period, SWING_THRESHOLDS)
            for th in period_ths:
                td = period_data.get(f"threshold_{th}", {})
                for direction in ["正T", "反T"]:
                    dd = td.get(direction, {})
                    vol_dist = dd.get("量能分布", {})
                    for vol_status, info in sorted(vol_dist.items()):
                        vol_rows.append([
                            f"{period}日", direction, th,
                            vol_status,
                            info.get("次数", 0),
                            info.get("胜率%", 0),
                        ])
        if vol_rows:
            sheets_xml.append(_build_sheet_xml("量能分析", vol_headers, vol_rows, vol_widths))
            sheet_names.append("量能分析")

        # ═══ 背离分析：按背离类型分组看胜率 ═══
        div_rows = []
        div_headers = ["周期", "方向", "阈值%", "背离类型", "次数", "胜率%"]
        div_widths = [8, 8, 8, 10, 8, 8]
        for period in SWING_PERIODS:
            period_data = swing_result.get(period, {})
            period_ths = dyn_thresholds_all.get(period, SWING_THRESHOLDS)
            for th in period_ths:
                td = period_data.get(f"threshold_{th}", {})
                for direction in ["正T", "反T"]:
                    dd = td.get(direction, {})
                    div_dist = dd.get("背离分布", {})
                    for div_type, info in sorted(div_dist.items()):
                        div_rows.append([
                            f"{period}日", direction, th,
                            div_type,
                            info.get("次数", 0),
                            info.get("胜率%", 0),
                        ])
        if div_rows:
            sheets_xml.append(_build_sheet_xml("背离分析", div_headers, div_rows, div_widths))
            sheet_names.append("背离分析")

    # ── 构建多sheet workbook ──
    sheet_refs = '\n'.join(
        f'    <sheet name="{xml_escape(name)}" sheetId="{i+1}" r:id="rId{i+1}"/>'
        for i, name in enumerate(sheet_names)
    )
    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
{sheet_refs}
  </sheets>
</workbook>'''

    sheet_rels = '\n'.join(
        f'  <Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i+1}.xml"/>'
        for i in range(len(sheet_names))
    )
    workbook_rels_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{sheet_rels}
  <Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    content_types_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
'''
    for i in range(len(sheet_names)):
        content_types_xml += f'  <Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
    content_types_xml += '</Types>'

    root_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    # ── 打包（失败自动加时间戳后缀重试） ──
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def _try_write(path):
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', content_types_xml.encode('utf-8'))
            zf.writestr('_rels/.rels', root_rels_xml.encode('utf-8'))
            zf.writestr('xl/workbook.xml', workbook_xml.encode('utf-8'))
            zf.writestr('xl/_rels/workbook.xml.rels', workbook_rels_xml.encode('utf-8'))
            for i, sheet_xml in enumerate(sheets_xml):
                zf.writestr(f'xl/worksheets/sheet{i+1}.xml', sheet_xml.encode('utf-8'))
            zf.writestr('xl/styles.xml', styles_xml.encode('utf-8'))

    try:
        _try_write(output_path)
    except PermissionError:
        import time
        base, ext = os.path.splitext(output_path)
        fallback = f"{base}_{int(time.time())}{ext}"
        print(f"  [注意] Excel文件被占用，另存为: {os.path.basename(fallback)}")
        _try_write(fallback)
        output_path = fallback
    except Exception as e:
        raise RuntimeError(f"写入T策略分析Excel失败: {e}")

    return os.path.abspath(output_path)


def _build_tech_comment(tech: Dict) -> str:
    """生成技术指标的解读文字"""
    parts = []
    parts.append(f"MACD:{tech.get('MACD方向','N/A')}")
    rsi = tech.get("RSI", "N/A")
    if isinstance(rsi, (int, float)):
        if rsi > 70:
            parts.append(f"RSI:{rsi}(超买)")
        elif rsi < 30:
            parts.append(f"RSI:{rsi}(超卖)")
        else:
            parts.append(f"RSI:{rsi}(中性)")
    else:
        parts.append(f"RSI:{rsi}")
    parts.append(f"量:{tech.get('成交量','N/A')}")
    div = tech.get("背离", "无")
    if div != "无":
        parts.append(f"[!]{div}")
    return " | ".join(parts)


# ════════════════════════════════════════════════════════════════
#  四、当前波段T信号分析
# ════════════════════════════════════════════════════════════════

def analyze_current_swing_signal(
    data: List[Dict],
    target_date: str,
    swing_result: Dict,
) -> Optional[Dict]:
    """
    分析指定日期是否适合做波段T

    基于 swing_result 的历史统计，检查 target_date 回溯 N日 的涨跌幅，
    判断触发的是正T还是反T信号，并给出历史胜率。

    Args:
        data: K线数据（最新在前）
        target_date: 目标分析日期 (YYYY-MM-DD)
        swing_result: analyze_swing_t 的返回结果

    Returns:
        {
            "股票名称": ...,
            "股票代码": ...,
            "分析日期": target_date,
            "数据日期范围": ...,
            "信号": [
                {
                    "周期": "5日",
                    "区间涨跌幅%": ...,
                    "方向": "上涨" / "下跌" / "震荡",
                    "触发阈值%": ...,
                    "信号类型": "正T" / "反T" / "无信号",
                    "建议操作": "买入" / "卖出" / "观望",
                    "历史胜率%": ...,
                    "历史信号次数": ...,
                },
                ...
            ],
            "综合建议": "...",
        }
    """
    # 将数据转为时间正序
    data_asc = list(reversed(data))
    closes = [row.get("收盘价", 0) or 0 for row in data_asc]
    dates = [row.get("日期", "") for row in data_asc]

    # 找 target_date 在 data 中的位置
    if target_date not in dates:
        # 尝试找最近的日期
        from datetime import datetime as dt
        target_dt = dt.strptime(target_date, "%Y-%m-%d")
        closest_idx = None
        closest_diff = None
        for i, d in enumerate(dates):
            if d:
                d_dt = dt.strptime(d, "%Y-%m-%d")
                diff = abs((d_dt - target_dt).days)
                if closest_diff is None or diff < closest_diff:
                    closest_diff = diff
                    closest_idx = i
        if closest_idx is not None and closest_diff is not None and closest_diff <= 5:
            target_date = dates[closest_idx]
            target_idx = closest_idx
        else:
            print(f"  [警告] 分析日期 {target_date} 在数据中未找到")
            return None
    else:
        target_idx = dates.index(target_date)

    n_data = len(closes)

    # ── 计算目标日期的技术指标（基于截至目标日的全部数据）──
    seg_closes = closes[:target_idx + 1]
    seg_volumes = [row.get("成交量(万手)", 0) or 0 for row in data_asc[:target_idx + 1]]
    seg_highs = [row.get("最高价", 0) or 0 for row in data_asc[:target_idx + 1]]
    seg_lows = [row.get("最低价", 0) or 0 for row in data_asc[:target_idx + 1]]

    # 反转→最新在前，用于计算指标
    rev_closes = list(reversed(seg_closes))
    rev_volumes = list(reversed(seg_volumes))

    macd_info_val = _macd_info(rev_closes) if len(rev_closes) >= 26 else {"方向": "数据不足"}
    rsi_val = _rsi(rev_closes, 14) if len(rev_closes) >= 15 else None
    vol_status = _volume_status(rev_volumes, 0, 20) if len(rev_volumes) >= 21 else "N/A"
    divergence = _macd_divergence(rev_closes, 0, min(30, len(rev_closes))) if len(rev_closes) >= 30 else "无"

    tech_summary = {
        "MACD方向": macd_info_val.get("方向", "N/A"),
        "RSI": round(rsi_val, 1) if rsi_val is not None else "N/A",
        "成交量": vol_status,
        "背离": divergence,
        "收盘价": closes[target_idx],
    }

    signals = []

    for period in SWING_PERIODS:
        if target_idx < period:
            continue

        # 计算 N 日区间涨跌幅
        prev_close = closes[target_idx - period]
        cur_close = closes[target_idx]
        if prev_close == 0:
            continue
        ret = (cur_close - prev_close) / prev_close * 100

        # 判断方向
        if ret >= 3:
            direction = "上涨"
        elif ret <= -3:
            direction = "下跌"
        else:
            direction = "震荡"

        # 在 swing_result 中找对应周期+阈值的历史胜率
        period_key = period
        best_zt = {"threshold": 0, "胜率": 0, "次数": 0}
        best_ft = {"threshold": 0, "胜率": 0, "次数": 0}

        if period_key in swing_result:
            # 使用动态阈值（从 swing_result 中读取），而不是硬编码的 SWING_THRESHOLDS
            dyn_thresholds = swing_result.get("_动态阈值", {}).get(period, SWING_THRESHOLDS)
            for th in dyn_thresholds:
                tk = f"threshold_{th}"
                if tk in swing_result[period_key]:
                    td = swing_result[period_key][tk]
                    zt = td.get("正T", {})
                    ft = td.get("反T", {})
                    # 检查当前涨跌幅是否 >= 阈值
                    if ret >= th and ft.get("信号次数", 0) >= 3:
                        if ft["胜率%"] > best_ft["胜率"]:
                            best_ft = {"threshold": th, "胜率": ft["胜率%"], "次数": ft["信号次数"]}
                    if ret <= -th and zt.get("信号次数", 0) >= 3:
                        if zt["胜率%"] > best_zt["胜率"]:
                            best_zt = {"threshold": th, "胜率": zt["胜率%"], "次数": zt["信号次数"]}

        # 生成信号条目（附带技术指标）
        entry = {
            "周期": f"{period}日",
            "区间涨跌幅%": round(ret, 2),
            "方向": direction,
            "MACD": tech_summary["MACD方向"],
            "RSI": tech_summary["RSI"],
            "成交量": tech_summary["成交量"],
            "背离": tech_summary["背离"],
        }

        if ret >= 3 and best_ft["threshold"] > 0:
            entry["触发阈值%"] = best_ft["threshold"]
            entry["信号类型"] = "反T"
            entry["建议操作"] = "卖出(反T)"
            entry["历史胜率%"] = best_ft["胜率"]
            entry["历史信号次数"] = best_ft["次数"]
        elif ret <= -3 and best_zt["threshold"] > 0:
            entry["触发阈值%"] = best_zt["threshold"]
            entry["信号类型"] = "正T"
            entry["建议操作"] = "买入(正T)"
            entry["历史胜率%"] = best_zt["胜率"]
            entry["历史信号次数"] = best_zt["次数"]
        else:
            entry["触发阈值%"] = "-"
            entry["信号类型"] = "无信号"
            entry["建议操作"] = "观望"
            entry["历史胜率%"] = "-"
            entry["历史信号次数"] = 0

        signals.append(entry)

    # 技术指标解读
    tech_comment = _build_tech_comment(tech_summary)
    _ = tech_comment  # 供后续使用

    # 综合建议
    zt_count = sum(1 for s in signals if s["信号类型"] == "正T")
    ft_count = sum(1 for s in signals if s["信号类型"] == "反T")
    if zt_count > ft_count and zt_count >= 2:
        overall = "多个周期出现正T信号，建议关注买入机会"
    elif ft_count > zt_count and ft_count >= 2:
        overall = "多个周期出现反T信号，建议关注卖出机会"
    elif zt_count == 1 and ft_count == 0:
        overall = "有正T信号，但仅单个周期确认"
    elif ft_count == 1 and zt_count == 0:
        overall = "有反T信号，但仅单个周期确认"
    else:
        overall = "无明显趋势信号，建议观望"

    return {
        "股票名称": "",
        "股票代码": "",
        "分析日期": target_date,
        "数据日期范围": f"{dates[0] if dates else '?'} ~ {dates[-1] if dates else '?'}",
        "技术指标": tech_summary,
        "信号": signals,
        "综合建议": overall,
    }


# ════════════════════════════════════════════════════════════════
#  五、生成当前波段信号 Excel
# ════════════════════════════════════════════════════════════════

def generate_current_signal_excel(
    signal_result: Dict,
    stock_name: str,
    stock_code: str,
    output_path: str,
) -> str:
    """
    生成当前波段T信号分析 Excel
    """
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="10"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="10"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF4472C4"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9E2F3"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color auto="1"/></left>
      <right style="thin"><color auto="1"/></right>
      <top style="thin"><color auto="1"/></top>
      <bottom style="thin"><color auto="1"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
</styleSheet>'''

    sheets_xml = []
    sheet_names = []

    # ── 标题信息行 ──
    info_headers = ["项目", "内容"]
    info_widths = [16, 40]
    info_rows = [
        ["股票", f"{stock_name}({stock_code})"],
        ["分析日期", signal_result.get("分析日期", "")],
        ["数据日期范围", signal_result.get("数据日期范围", "")],
        ["综合建议", signal_result.get("综合建议", "")],
    ]
    sheets_xml.append(_build_sheet_xml("分析概要", info_headers, info_rows, info_widths))
    sheet_names.append("分析概要")

    # ── 技术指标概览表 ──
    tech = signal_result.get("技术指标", {})
    if tech:
        tech_headers = ["指标", "数值"]
        tech_widths = [12, 24]
        tech_rows = [
            ["MACD方向", tech.get("MACD方向", "N/A")],
            ["RSI", tech.get("RSI", "N/A")],
            ["成交量", tech.get("成交量", "N/A")],
            ["背离", tech.get("背离", "无")],
            ["分析日收盘价", tech.get("收盘价", "")],
        ]
        sheets_xml.append(_build_sheet_xml("技术指标", tech_headers, tech_rows, tech_widths))
        sheet_names.append("技术指标")

    # ── 信号详情表 ──
    sig_headers = ["周期", "方向", "区间涨跌幅%", "触发阈值%", "信号类型", "建议操作",
                   "历史胜率%", "历史信号次数", "MACD", "RSI", "量能", "背离"]
    sig_widths = [8, 8, 14, 12, 10, 14, 12, 14, 12, 8, 8, 8]
    sig_rows = []
    for s in signal_result.get("信号", []):
        sig_rows.append([
            s.get("周期", ""),
            s.get("方向", ""),
            s.get("区间涨跌幅%", 0),
            s.get("触发阈值%", "-"),
            s.get("信号类型", ""),
            s.get("建议操作", ""),
            s.get("历史胜率%", "-"),
            s.get("历史信号次数", 0),
            s.get("MACD", ""),
            s.get("RSI", ""),
            s.get("成交量", ""),
            s.get("背离", ""),
        ])
    sheets_xml.append(_build_sheet_xml("信号详情", sig_headers, sig_rows, sig_widths))
    sheet_names.append("信号详情")

    # ── 解读说明 ──
    guide_lines = [
        f"{stock_name}({stock_code}) 波段T信号分析",
        "=" * 50,
        "",
        f"分析日期: {signal_result.get('分析日期', '')}",
        f"综合建议: {signal_result.get('综合建议', '')}",
        "",
        "【信号解读】",
        "  上涨 → 反T信号: 建议先卖出后买回，赚取回调差价",
        "  下跌 → 正T信号: 建议先买入后卖出，赚取反弹差价",
        "  震荡 → 无信号: 趋势不明朗，建议观望",
        "",
        "【操作建议】",
        "  - 多个周期信号一致时，信号可靠性更高",
        "  - 历史胜率 > 55% 的信号值得执行",
        "  - 结合MACD/RSI等技术指标可进一步提高胜率",
        "",
        "【注意事项】",
        "  1. 以上分析基于历史数据，不保证未来表现",
        "  2. 反T需先有持仓或融券，正T需有可用资金",
        "  3. 建议结合当日实际盘面判断是否执行",
    ]
    sheets_xml.append(_build_text_sheet(guide_lines, col_width=80))
    sheet_names.append("解读说明")

    # ── 构建workbook ──
    sheet_refs = '\n'.join(
        f'    <sheet name="{xml_escape(name)}" sheetId="{i+1}" r:id="rId{i+1}"/>'
        for i, name in enumerate(sheet_names)
    )
    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
{sheet_refs}
  </sheets>
</workbook>'''

    sheet_rels = '\n'.join(
        f'  <Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i+1}.xml"/>'
        for i in range(len(sheet_names))
    )
    workbook_rels_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{sheet_rels}
  <Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    content_types_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
'''
    for i in range(len(sheet_names)):
        content_types_xml += f'  <Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
    content_types_xml += '</Types>'

    root_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    # ── 打包 ──
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    def _try_write(path):
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', content_types_xml.encode('utf-8'))
            zf.writestr('_rels/.rels', root_rels_xml.encode('utf-8'))
            zf.writestr('xl/workbook.xml', workbook_xml.encode('utf-8'))
            zf.writestr('xl/_rels/workbook.xml.rels', workbook_rels_xml.encode('utf-8'))
            for i, sheet_xml in enumerate(sheets_xml):
                zf.writestr(f'xl/worksheets/sheet{i+1}.xml', sheet_xml.encode('utf-8'))
            zf.writestr('xl/styles.xml', styles_xml.encode('utf-8'))

    try:
        _try_write(output_path)
    except PermissionError:
        import time
        base, ext = os.path.splitext(output_path)
        fallback = f"{base}_{int(time.time())}{ext}"
        print(f"  [注意] Excel文件被占用，另存为: {os.path.basename(fallback)}")
        _try_write(fallback)
        output_path = fallback

    return os.path.abspath(output_path)


# ════════════════════════════════════════════════════════════════
#  六、合并多日期波段T信号 Excel
# ════════════════════════════════════════════════════════════════

def _detect_trend_phases(prices: List[float], threshold_pct: float = 5.0) -> List[int]:
    """
    Zigzag趋势检测 —— 模拟人眼看图判断趋势

    算法（两遍扫描）：
      1. 第一遍：从起点开始扫描，追踪价格方向
         - 上升趋势中追踪最高点，从最高点回落超过threshold_pct% → 确认一个"峰"
         - 下降趋势中追踪最低点，从最低点反弹超过threshold_pct% → 确认一个"谷"
         - 峰谷交替记录
      2. 第二遍：在转折点之间填充趋势
         - 峰→谷之间为下降趋势(浅绿)
         - 谷→峰之间为上升趋势(浅红)
         - 转折点当天仍算原趋势，次日才开始新趋势

    threshold_pct: 趋势反转的最小幅度（默认5%，表示回调/反弹超过5%才算反转）

    Returns:
        [4, None, 4, 5, ...]  4=上升(浅红)  5=下降(浅绿)  None=横盘
    """
    n = len(prices)
    fills = [None] * n
    if n < 10:
        return fills

    # 1. 找初始方向（看前15天内是否有超过threshold_pct的变动）
    direction = None  # 1=up, -1=down
    for i in range(1, min(15, n)):
        chg = (prices[i] - prices[0]) / prices[0] * 100
        if chg > threshold_pct:
            direction = 1
            break
        elif chg < -threshold_pct:
            direction = -1
            break

    if direction is None:
        return fills  # 没有明确方向，全部横盘

    # 2. 扫描找出所有转折点（峰和谷）
    turning_pts = []   # [(index, type), ...]  type='peak' or 'trough'
    extreme_idx = 0
    extreme_price = prices[0]

    for i in range(1, n):
        p = prices[i]

        if direction == 1:  # 上升中
            if p > extreme_price:
                extreme_price = p
                extreme_idx = i
            # 从最高点回落超过阈值 → 确认峰
            if (extreme_price - p) / extreme_price * 100 > threshold_pct:
                turning_pts.append((extreme_idx, 'peak'))
                direction = -1
                extreme_price = p
                extreme_idx = i
        else:  # 下降中
            if p < extreme_price:
                extreme_price = p
                extreme_idx = i
            # 从最低点反弹超过阈值 → 确认谷
            if (p - extreme_price) / extreme_price * 100 > threshold_pct:
                turning_pts.append((extreme_idx, 'trough'))
                direction = 1
                extreme_price = p
                extreme_idx = i

    # 加上最后一个端点
    turning_pts.append((n - 1, 'peak' if direction == 1 else 'trough'))

    if not turning_pts:
        return fills

    # 3. 填充趋势
    # 第一个转折点之前：用初始方向（转折当天算原趋势）
    first_idx, first_type = turning_pts[0]
    for i in range(0, first_idx + 1):
        fills[i] = 5 if first_type == 'peak' else 4

    # 转折点之间：从转折点次日开始新趋势
    for k in range(len(turning_pts) - 1):
        i1, t1 = turning_pts[k]
        i2, t2 = turning_pts[k + 1]
        fill_val = 4 if t1 == 'peak' else 5  # peak后↓, trough后↑
        for i in range(i1 + 1, i2 + 1):
            fills[i] = fill_val

    return fills

def generate_consolidated_signal_excel(
    all_signals: List[Dict],
    all_dates: List[str],
    stock_name: str,
    stock_code: str,
    output_path: str,
) -> str:
    """
    生成合并的多日期波段T信号分析 Excel

    Sheet结构:
      - 汇总对比: 所有日期的信号概览表
      - 解读说明: 策略说明
    （不再逐日生成单独Sheet，避免日期过多时文件臃肿）
    """
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="10"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="10"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="6">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF4472C4"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9E2F3"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFC8E0C8"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFE0C8C8"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color auto="1"/></left>
      <right style="thin"><color auto="1"/></right>
      <top style="thin"><color auto="1"/></top>
      <bottom style="thin"><color auto="1"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="6">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
</styleSheet>'''

    sheets_xml = []
    sheet_names = []

    # 波段T信号 - 趋势检测阈值(%): 回调/反弹超过此值才算趋势反转
    _TREND_THRESHOLD_PCT = 5.0

    # ── Sheet1: 汇总对比表 ──
    periods_order = [f"{p}日" for p in SWING_PERIODS]
    summary_headers = ["分析日期", "收盘价", "趋势"] \
                    + [f"{p}日-涨跌幅%" for p in SWING_PERIODS] \
                    + [f"{p}日-建议" for p in SWING_PERIODS] \
                    + ["综合胜率%", "MACD", "RSI", "量能", "背离", "综合建议"]
    summary_widths = [14, 10, 8] + [14]*5 + [14]*5 + [10, 12, 8, 8, 8, 30]
    summary_rows = []

    for i, dt in enumerate(all_dates):
        sig = all_signals[i] if i < len(all_signals) else None
        if not sig:
            continue
        tech = sig.get("技术指标", {})
        row = [dt]
        # 收盘价
        row.append(tech.get("收盘价", ""))
        # 趋势列暂时占位，后续统一填充
        row.append("")
        signal_map = {}
        for s in sig.get("信号", []):
            signal_map[s["周期"]] = s
        for p in periods_order:
            s = signal_map.get(p, {})
            row.append(s.get("区间涨跌幅%", ""))
        for p in periods_order:
            s = signal_map.get(p, {})
            row.append(s.get("建议操作", ""))
        # 综合胜率：看多只算买入信号，看空只算卖出信号
        win_rates = []
        overall = sig.get("综合建议", "")
        is_bullish = "买入" in overall or "正T" in overall
        is_bearish = "卖出" in overall or "反T" in overall
        for p in periods_order:
            s = signal_map.get(p, {})
            wr = s.get("历史胜率%", "")
            action = s.get("建议操作", "")
            if not isinstance(wr, (int, float)) or wr == "-":
                continue
            if is_bullish and "买入" in action:
                win_rates.append(wr)
            elif is_bearish and "卖出" in action:
                win_rates.append(wr)
            elif not is_bullish and not is_bearish:
                win_rates.append(wr)
        avg_win = round(sum(win_rates) / len(win_rates), 1) if win_rates else ""
        row.append(avg_win)
        row.extend([
            tech.get("MACD方向", ""),
            tech.get("RSI", ""),
            tech.get("成交量", ""),
            tech.get("背离", ""),
            sig.get("综合建议", ""),
        ])
        summary_rows.append(row)

    # ── 检测趋势阶段（收盘价按日期从旧到新排列）──
    closes = []
    for sig in all_signals:
        tech = sig.get("技术指标", {})
        c = tech.get("收盘价")
        if isinstance(c, (int, float)):
            closes.append(c)
        else:
            try:
                closes.append(float(c))
            except:
                closes.append(0)

    # closes已经是旧->新顺序，直接传给_detect_trend_phases
    trend_fills = _detect_trend_phases(closes, _TREND_THRESHOLD_PCT)

    # 将趋势标记写入第3列（summary_rows也是旧->新顺序）
    trend_labels = {4: "下降趋势", 5: "上升趋势", None: "横盘状态"}
    for i in range(len(summary_rows)):
        if i < len(trend_fills):
            summary_rows[i][2] = trend_labels.get(trend_fills[i], "")

    sheets_xml.append(_build_sheet_xml(
        "汇总对比", summary_headers, summary_rows, summary_widths,
        row_fills=trend_fills
    ))
    sheet_names.append("汇总对比")

    # ── 后续: 解读说明（不再逐日生成单独Sheet）──
    guide_lines = [
        f"{stock_name}({stock_code}) 波段T信号分析 (多日期合并)",
        "=" * 50,
        "",
        f"分析日期: {all_dates[0] if all_dates else ''} ~ {all_dates[-1] if all_dates else ''} (共{len(all_dates)}天)",
        "",
        "[汇总表解读]",
        "  每行代表一个分析日期，各列显示对应周期的信号",
        "  购买(正T) = 当日下跌超过阈值，建议买入后卖出",
        "  卖出(反T) = 当日上涨超过阈值，建议卖出后买回",
        "  观望 = 涨跌幅未达阈值，无明显信号",
        "",
        "[技术指标]",
        "  MACD: 金叉向上(多头) / 死叉向下(空头)",
        "  RSI: >70超买 / <30超卖",
        "  量能: 放量(>1.5倍均量) / 缩量(<0.7倍) / 正常",
        "  背离: 顶背离(上涨动能衰竭) / 底背离(下跌动能衰竭)",
        "",
        "[注意事项]",
        "  1. 以上分析基于历史数据，不保证未来表现",
        "  2. 未考虑交易佣金、印花税等摩擦成本",
        "  3. 反T需先有持仓或融券，正T需有可用资金",
    ]
    sheets_xml.append(_build_text_sheet(guide_lines, col_width=85))
    sheet_names.append("解读说明")

    # ── 构建workbook ──
    sheet_refs = '\n'.join(
        f'    <sheet name="{xml_escape(name)}" sheetId="{i+1}" r:id="rId{i+1}"/>'
        for i, name in enumerate(sheet_names)
    )
    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
{sheet_refs}
  </sheets>
</workbook>'''

    sheet_rels = '\n'.join(
        f'  <Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i+1}.xml"/>'
        for i in range(len(sheet_names))
    )
    workbook_rels_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{sheet_rels}
  <Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    content_types_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
'''
    for i in range(len(sheet_names)):
        content_types_xml += f'  <Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
    content_types_xml += '</Types>'

    root_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def _try_write(path):
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', content_types_xml.encode('utf-8'))
            zf.writestr('_rels/.rels', root_rels_xml.encode('utf-8'))
            zf.writestr('xl/workbook.xml', workbook_xml.encode('utf-8'))
            zf.writestr('xl/_rels/workbook.xml.rels', workbook_rels_xml.encode('utf-8'))
            for i, sheet_xml in enumerate(sheets_xml):
                zf.writestr(f'xl/worksheets/sheet{i+1}.xml', sheet_xml.encode('utf-8'))
            zf.writestr('xl/styles.xml', styles_xml.encode('utf-8'))

    try:
        _try_write(output_path)
    except PermissionError:
        import time
        base, ext = os.path.splitext(output_path)
        fallback = f"{base}_{int(time.time())}{ext}"
        print(f"  [注意] Excel文件被占用，另存为: {os.path.basename(fallback)}")
        _try_write(fallback)
        output_path = fallback

    return os.path.abspath(output_path)


# ════════════════════════════════════════════════════════════════
#  七、波段涨跌幅历史分布分析（5/10/20/30/90日）
# ════════════════════════════════════════════════════════════════


def _calc_swing_stats_weighted(indices, vals, total_len):
    """
    基于时间顺序计算统计量，较新的数据权重更大（线性衰减）。
    indices: vals 在原始 returns 中的索引位置
    vals: 对应的数值列表
    total_len: 原始 returns 总长度
    """
    if not vals:
        return {'最大值': 0, '平均值': 0, '加权平均': 0, '中位数': 0}
    s_vals = sorted(vals)
    nv = len(s_vals)
    max_v = round(max(s_vals), 2)
    avg = round(sum(s_vals) / nv, 2)
    weights = [(idx + 1) for idx in indices]
    total_w = sum(weights)
    w_avg = round(sum(v * w for v, w in zip(vals, weights)) / total_w, 2) if total_w > 0 else 0
    if nv % 2 == 1:
        med = s_vals[nv // 2]
    else:
        med = (s_vals[nv // 2 - 1] + s_vals[nv // 2]) / 2
    med = round(med, 2)
    return {'最大值': max_v, '平均值': avg, '加权平均': w_avg, '中位数': med}


def _bin_returns_weighted(vals, indices, is_up, bin_width=5.0):
    """
    将数值分桶并统计每个桶内数值的加权数量（每个值权重 = 索引+1，越新权重越大）。
    返回: { "0~5": {"次数": 10, "加权次数": 25.5}, ... }
    """
    if not vals:
        return {}
    max_val = max(vals)
    bins = {}
    b = 0.0
    while b <= max_val + bin_width * 0.001:
        low = round(b, 2)
        high = round(b + bin_width, 2)
        if is_up:
            label = f'{low}~{high}'
        else:
            label = f'-{high}~-{low}'
        count = 0
        w_sum = 0.0
        for v, idx in zip(vals, indices):
            weight = idx + 1
            if low <= v < high or (v == max_val and high > v >= low):
                count += 1
                w_sum += weight
        if count > 0:
            bins[label] = {'次数': count, '加权次数': round(w_sum, 1)}
        b += bin_width
    return bins


def _bin_returns_adaptive(vals, indices, is_up, n_bins=12):
    """
    自适应分箱：局部密度决定等宽/等频的混合比例。
    密集区域偏向等频（区间窄、展示细节），稀疏区域偏向等宽（区间宽、避免碎箱）。
    结果：区间宽度和百分位增长都不极端，分布呈现更自然。

    Args:
        vals: 涨跌幅数值列表
        indices: 对应的时间索引（用于加权计数）
        is_up: True=上涨侧, False=下跌侧
        n_bins: 目标箱数（默认12）
    Returns:
        { "0.0~1.5": {"次数": 35, "加权次数": 123.4}, ... }
    """
    if not vals:
        return {}
    n = len(vals)
    if n <= n_bins:
        n_bins = max(2, n)

    paired = sorted(zip(vals, indices), key=lambda x: x[0])
    sorted_vals = [p[0] for p in paired]
    v_min, v_max = sorted_vals[0], sorted_vals[-1]
    total_width = v_max - v_min
    if total_width <= 0:
        total_width = 0.01

    # ── 全局平均密度（用于归一化）──
    global_density = n / total_width if total_width > 0 else 1
    # 密度窗口宽度：总范围的 5%，至少覆盖 10 个样本
    density_window = max(total_width * 0.05, sorted_vals[min(n-1, 10)] - sorted_vals[0])

    def _density_at_value(v):
        """返回值 v 附近的局部归一化密度 [0, 1]"""
        half = density_window / 2
        lo = v - half
        hi = v + half
        # 二分查找窗口内的样本数
        import bisect
        li = bisect.bisect_left(sorted_vals, lo)
        ri = bisect.bisect_right(sorted_vals, hi)
        cnt = ri - li
        w = sorted_vals[min(ri-1, n-1)] - sorted_vals[li] if ri > li else density_window
        if w < 0.001:
            return 0.5
        local_d = cnt / w if w > 0 else global_density
        ratio = local_d / global_density if global_density > 0 else 0.5
        return max(0.0, min(1.0, ratio))

    # ── 对每个边界位置计算自适应位置 ──
    adaptive_bounds = [v_min]
    for i in range(1, n_bins):
        # 等宽位置
        ew = v_min + total_width * i / n_bins
        # 等频位置
        ef_idx = min(i * n // n_bins, n - 1)
        ef = sorted_vals[ef_idx]
        # 初始混合（用于确定密度检测位置）
        init_bound = ew * 0.4 + ef * 0.6
        # 在该位置检测局部密度
        ld = _density_at_value(init_bound)
        # 密集(ld≈1)→偏等频(ef), 稀疏(ld≈0)→偏等宽(ew)
        bound = ew * (1 - ld) + ef * ld
        adaptive_bounds.append(bound)
    adaptive_bounds.append(v_max)

    # 确保单调递增
    for i in range(1, len(adaptive_bounds)):
        if adaptive_bounds[i] <= adaptive_bounds[i - 1]:
            adaptive_bounds[i] = adaptive_bounds[i - 1] + 0.01

    # ── 按自适应边界分箱（同时记录在 paired 中的位置）──
    pos = 0
    raw_bins = []  # [(lo_r, hi_r, count, w_sum, start_pos, end_pos), ...]
    for b in range(len(adaptive_bounds) - 1):
        lo = adaptive_bounds[b]
        hi = adaptive_bounds[b + 1]
        seg_vals = []
        seg_indices = []
        bin_start = pos
        while pos < n:
            v, idx = paired[pos]
            if lo <= v < hi:
                seg_vals.append(v)
                seg_indices.append(idx)
                pos += 1
            elif b == len(adaptive_bounds) - 2 and lo <= v <= hi + 0.001:
                seg_vals.append(v)
                seg_indices.append(idx)
                pos += 1
            else:
                break
        if seg_vals:
            count = len(seg_vals)
            w_sum = sum(idx + 1 for idx in seg_indices)
            lo_r = round(min(seg_vals), 1)
            hi_r = round(max(seg_vals), 1)
            raw_bins.append((lo_r, hi_r, count, round(w_sum, 1), bin_start, pos))

    # ── 合并过小箱（count < 总样本*1.5% 且 < 2），传播位置 ──
    min_count = max(2, int(n * 0.015))
    filled = []
    for lo_r, hi_r, count, w_sum, st, en in raw_bins:
        if (count < min_count or count == 0) and filled:
            prev = filled[-1]
            filled[-1] = (prev[0], max(prev[1], hi_r), prev[2] + count,
                           round(prev[3] + w_sum, 1), prev[4], en)
        else:
            filled.append((lo_r, hi_r, count, w_sum, st, en))
    if len(filled) >= 2 and filled[0][2] < min_count:
        filled[1] = (filled[0][0], filled[1][1], filled[0][2] + filled[1][2],
                      round(filled[0][3] + filled[1][3], 1), filled[0][4], filled[1][5])
        filled = filled[1:]

    # 合并标签相同的相邻箱
    merged = []
    for lo_r, hi_r, count, w_sum, st, en in filled:
        if merged and merged[-1][0] == lo_r and merged[-1][1] == hi_r:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], hi_r), prev[2] + count,
                           round(prev[3] + w_sum, 1), prev[4], en)
        else:
            merged.append((lo_r, hi_r, count, w_sum, st, en))

    # ── 拆分过大的箱（百分位跳 > 5%）──
    max_count_per_bin = max(1, int(n * 0.05))
    split_bins = []
    for lo_r, hi_r, count, w_sum, st, en in merged:
        if count <= max_count_per_bin:
            split_bins.append((lo_r, hi_r, count, w_sum))
        else:
            # 等频拆分：将子数据按样本数均分
            sub_pairs = paired[st:en]
            sub_n = max(2, (count + max_count_per_bin - 1) // max_count_per_bin)
            # 等频分箱子数据
            per = len(sub_pairs) // sub_n
            rem = len(sub_pairs) % sub_n
            pos2 = 0
            for k in range(sub_n):
                sz = per + (1 if k < rem else 0)
                if sz <= 0:
                    continue
                seg = sub_pairs[pos2:pos2 + sz]
                s_lo = round(min(p[0] for p in seg), 1)
                s_hi = round(max(p[0] for p in seg), 1)
                s_cnt = len(seg)
                s_w = round(sum(p[1] + 1 for p in seg), 1)
                split_bins.append((s_lo, s_hi, s_cnt, s_w))
                pos2 += sz

    # ── 再次合并过小箱 ──
    merged2 = []
    for lo_r, hi_r, count, w_sum in split_bins:
        if (count < min_count) and merged2:
            prev = merged2[-1]
            merged2[-1] = (prev[0], max(prev[1], hi_r), prev[2] + count,
                           round(prev[3] + w_sum, 1))
        else:
            merged2.append((lo_r, hi_r, count, w_sum))

    # ── 生成最终字典（确保标签连续，不出现视觉断裂）──
    result = {}
    # 转为可变列表，按 lo 排序
    edges = [[lo, hi, count, w_sum] for lo, hi, count, w_sum in merged2]
    edges.sort(key=lambda x: x[0])
    # 第一遍：hi 向下一个的 lo 对齐
    for i in range(len(edges) - 1):
        if edges[i + 1][0] > edges[i][1]:
            edges[i][1] = edges[i + 1][0]
    # 第二遍：lo 向前一个的 hi 对齐，统一2位小数
    for i in range(len(edges)):
        if i > 0:
            edges[i][0] = edges[i - 1][1]
        edges[i][0] = round(edges[i][0], 2)
        edges[i][1] = round(edges[i][1], 2)
        lo_r, hi_r, count, w_sum = edges[i]
        if is_up:
            label = f'{lo_r}~{hi_r}'
        else:
            label = f'-{hi_r}~-{lo_r}'
        # 处理重复标签（极少情况）
        base = label
        dup = 1
        while label in result:
            dup += 1
            label = f'{base}#{dup}'
        result[label] = {'次数': count, '加权次数': w_sum}
    return result


def analyze_swing_amplitude_distribution(data, periods=None, bins_per_side=None):
    """
    统计多周期（5/10/20/30/90日）上涨/下跌幅度历史分布。
    采用自适应分箱：在等宽与等频之间取平衡，密集处区间较窄、稀疏处区间较宽，
    使区间宽度和百分位增长都不极端，分布呈现更自然。

    对每个周期：
      1. 在时间正序数据上滑动窗口，计算 N 日区间涨跌幅
      2. 自适应分箱，统计每箱出现次数（含加权次数）
      3. 分别统计上涨侧(>0)和下跌侧(<0)的分布
      4. 计算最大值、平均值、加权平均（线性衰减权重）、中位数

    Args:
        data: K线数据（最新在前）
        periods: 统计周期列表（默认 [5, 10, 20, 30, 90]）
        n_bins: 每侧目标箱数。None 时根据样本数自动计算：max(8, min(50, n//10))。
        上涨侧和下跌侧各自独立计算，不强制相同。

    Returns:
        {
            "5": {
                "数据不足": False,
                "样本数": N,
                "range": "min~max",
                "上涨幅度": { "样本数": N_up, "分布": {...}, "最大值": ..., "平均值": ..., "加权平均": ..., "中位数": ... },
                "下跌幅度": { ... },
            },
            ...
        }
    """
    if periods is None:
        periods = [5, 10, 20, 30, 90]

    data_asc = list(reversed(data))
    closes = [row.get('收盘价', 0) or 0 for row in data_asc]
    n = len(closes)

    result = {}
    for period in periods:
        if n <= period:
            result[str(period)] = {'数据不足': True, '样本数': 0}
            continue

        returns = []
        for i in range(period, n):
            prev = closes[i - period]
            cur = closes[i]
            if prev == 0:
                continue
            ret = (cur - prev) / prev * 100
            returns.append(ret)

        if not returns:
            result[str(period)] = {'数据不足': True, '样本数': 0}
            continue

        total = len(returns)
        up_indices = [i for i, r in enumerate(returns) if r > 0]
        down_indices = [i for i, r in enumerate(returns) if r < 0]
        up_returns = [returns[i] for i in up_indices]
        down_returns_abs = [abs(returns[i]) for i in down_indices]

        # 每侧独立计算目标箱数（根据各自样本量，同时保证5%约束）
        if bins_per_side is None:
            n_up = max(8, min(50, len(up_returns) // 10))
            n_down = max(8, min(50, len(down_returns_abs) // 10))
            # 保证5%约束所需的最小箱数（与 _bin_returns_adaptive 内部 max_count 一致）
            up_mc = max(1, int(len(up_returns) * 0.05))
            down_mc = max(1, int(len(down_returns_abs) * 0.05))
            n_up = max(n_up, min((len(up_returns) + up_mc - 1) // up_mc, 50))
            n_down = max(n_down, min((len(down_returns_abs) + down_mc - 1) // down_mc, 50))
        else:
            n_up = n_down = bins_per_side

        # 自适应分箱（等宽与等频的平衡）
        up_dist = _bin_returns_adaptive(up_returns, up_indices, is_up=True, n_bins=n_up)
        down_dist = _bin_returns_adaptive(down_returns_abs, down_indices, is_up=False, n_bins=n_down)

        up_stats = _calc_swing_stats_weighted(up_indices, up_returns, total)
        down_stats = _calc_swing_stats_weighted(down_indices, down_returns_abs, total)

        result[str(period)] = {
            '数据不足': False,
            '样本数': total,
            'range': f'{round(min(returns), 2)} ~ {round(max(returns), 2)}',
            '时间范围': f"{data_asc[period].get('日期', '?')} ~ {data_asc[-1].get('日期', '?')}",
            '上涨幅度': {
                '样本数': len(up_returns),
                '分布': up_dist,
                '最大值': up_stats['最大值'],
                '平均值': up_stats['平均值'],
                '加权平均': up_stats['加权平均'],
                '中位数': up_stats['中位数'],
            },
            '下跌幅度': {
                '样本数': len(down_returns_abs),
                '分布': down_dist,
                '最大值': down_stats['最大值'],
                '平均值': down_stats['平均值'],
                '加权平均': down_stats['加权平均'],
                '中位数': down_stats['中位数'],
            },
        }
    # ── 整体数据时间范围 ──
    if data_asc:
        result['_meta'] = {
            '时间范围': f"{data_asc[0].get('日期', '?')} ~ {data_asc[-1].get('日期', '?')}"
        }
    return result


def generate_amplitude_distribution_excel(dist_data, stock_name, stock_code, output_path, data_time_range=None):
    """
    生成波段涨跌幅历史分布分析 Excel
    """
    from xml.sax.saxutils import escape as xml_escape

    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="10"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="10"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF4472C4"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9E2F3"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color auto="1"/></left>
      <right style="thin"><color auto="1"/></right>
      <top style="thin"><color auto="1"/></top>
      <bottom style="thin"><color auto="1"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
</styleSheet>'''

    sheets_xml = []
    sheet_names = []

    # Strategy description sheet
    guide_lines = [
        '%s(%s) 波段涨跌幅历史分布分析' % (stock_name, stock_code),
        '=' * 60,
        '',
        '【策略说明】',
        '  统计 5/10/20/30/90 日周期内股价的上涨/下跌幅度分布。',
        '  采用自适应分箱：密集处区间较窄、稀疏处区间较宽，区间宽度和百分位增长都不极端。',
        '',
        '【分析时间范围】',
        f'  {data_time_range}' if data_time_range else '',
        '',
        '【统计指标】',
        '  最大值: 该周期内最大的涨跌幅绝对值',
        '  平均值: 所有样本的简单算术平均',
        '  加权平均: 按时间线性衰减权重（越近权重越高）',
        '  中位数: 样本按大小排序后的中间值',
        '',
        '【分布解读】',
        '  分布表显示了涨跌幅在各自适应区间内的分布次数和累积百分位。',
        '  上涨幅度侧（正收益）和下跌幅度侧（负收益绝对值）分别统计。',
        '  加权次数 = 每个样本权重（位置索引+1）之和，越新的数据权重越大。',
        '  百分位 = 当前区间上限以内所有样本的累积占比（从小到大）。',
        '',
        '【用法参考】',
        '  - 观察哪个区间的密度最高，判断最常见的波段幅度',
        '  - 比较加权平均与简单平均，判断近期趋势是否偏离历史均值',
        '  - 最大值可辅助设置止盈止损参考线',
        '  - 中位数反映典型的波段幅度（不受极端值影响）',
        '  - 百分位可以判断当前涨跌幅处于历史的什么位置：',
        '    比如90日涨20%处于85%分位，说明仅15%的历史样本涨幅更大',
        '    百分位越高说明越极端，反转概率越大',
    ]
    sheets_xml.append(_build_text_sheet(guide_lines, col_width=90))
    sheet_names.append('分布分析说明')

    periods_order = ['5', '10', '20', '30', '90']
    for period_str in periods_order:
        pd = dist_data.get(period_str, {})
        if pd.get('数据不足', True):
            continue

        up = pd.get('上涨幅度', {})
        down = pd.get('下跌幅度', {})
        period_time_range = pd.get('时间范围', '')

        # Stats block
        stats_headers = ['指标', '上涨幅度', '下跌幅度(绝对值)']
        stats_widths = [16, 14, 18]
        stats_rows = [
            ['时间范围', period_time_range, ''],
            ['样本数', up.get('样本数', 0), down.get('样本数', 0)],
            ['最大值(%)', up.get('最大值', 0), down.get('最大值', 0)],
            ['平均值(%)', up.get('平均值', 0), down.get('平均值', 0)],
            ['加权平均(%)', up.get('加权平均', 0), down.get('加权平均', 0)],
            ['中位数(%)', up.get('中位数', 0), down.get('中位数', 0)],
        ]
        sheets_xml.append(_build_sheet_xml('波段' + period_str + '日分布', stats_headers, stats_rows, stats_widths))
        sheet_names.append('波段' + period_str + '日分布')

        # Distribution details
        up_dist = up.get('分布', {})
        down_dist = down.get('分布', {})
        up_total = up.get('样本数', 0) or 1
        down_total = down.get('样本数', 0) or 1

        # 按区间上限升序排序，用于计算累积百分位
        up_labels_sorted = sorted(up_dist.keys(), key=lambda x: float(x.split('~')[0]))
        down_labels_asc = sorted(down_dist.keys(), key=lambda x: float(x.split('~')[0]))

        # 计算每个区间的累积百分位（上涨：从小到大累加）
        up_cumul = {}  # label -> 累积百分位%
        cumul_count = 0
        for lab in up_labels_sorted:
            cumul_count += up_dist[lab]['次数']
            up_cumul[lab] = round(cumul_count / up_total * 100, 1)

        # 计算每个区间的累积百分位（下跌：从小到大累加）
        down_cumul = {}  # label -> 累积百分位%
        cumul_count = 0
        for lab in down_labels_asc:
            cumul_count += down_dist[lab]['次数']
            down_cumul[lab] = round(cumul_count / down_total * 100, 1)

        # 下跌侧按从大到小显示（与之前一致）
        down_labels_display = sorted(down_dist.keys(), key=lambda x: -float(x.split('~')[1].replace('-', '')))

        dist_headers = ['上涨区间', '上涨次数', '上涨概率%', '上涨加权次数', '上涨百分位%',
                        '下跌区间', '下跌次数', '下跌概率%', '下跌加权次数', '下跌百分位%']
        dist_widths = [14, 10, 10, 14, 12, 14, 10, 10, 14, 12]
        dist_rows = []

        max_len = max(len(up_labels_sorted), len(down_labels_display))
        for i in range(max_len):
            row = []
            if i < len(up_labels_sorted):
                lab = up_labels_sorted[i]
                up_prob = round(up_dist[lab]['次数'] / up_total * 100, 1)
                row.extend([lab, up_dist[lab]['次数'], up_prob, up_dist[lab]['加权次数'], up_cumul[lab]])
            else:
                row.extend(['', '', '', '', ''])
            if i < len(down_labels_display):
                lab = down_labels_display[i]
                down_prob = round(down_dist[lab]['次数'] / down_total * 100, 1)
                row.extend([lab, down_dist[lab]['次数'], down_prob, down_dist[lab]['加权次数'], down_cumul[lab]])
            else:
                row.extend(['', '', '', '', ''])
            dist_rows.append(row)

        sheets_xml.append(_build_sheet_xml('波段' + period_str + '日详情', dist_headers, dist_rows, dist_widths))
        sheet_names.append('波段' + period_str + '日详情')

    if len(sheets_xml) <= 1:
        sheets_xml.append(_build_sheet_xml('数据状态', ['提示'], [['数据不足，无法生成分布分析']], [30]))
        sheet_names.append('数据状态')

    # Build workbook
    sheet_refs = '\n'.join(
        '    <sheet name="' + xml_escape(name) + '" sheetId="' + str(i+1) + '" r:id="rId' + str(i+1) + '"/>'
        for i, name in enumerate(sheet_names)
    )
    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
''' + sheet_refs + '''
  </sheets>
</workbook>'''

    sheet_rels = '\n'.join(
        '  <Relationship Id="rId' + str(i+1) + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet' + str(i+1) + '.xml"/>'
        for i in range(len(sheet_names))
    )
    workbook_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
''' + sheet_rels + '''
  <Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    content_types_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
'''
    for i in range(len(sheet_names)):
        content_types_xml += '  <Override PartName="/xl/worksheets/sheet' + str(i+1) + '.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
    content_types_xml += '</Types>'

    root_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def _try_write(path):
        import zipfile
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', content_types_xml.encode('utf-8'))
            zf.writestr('_rels/.rels', root_rels_xml.encode('utf-8'))
            zf.writestr('xl/workbook.xml', workbook_xml.encode('utf-8'))
            zf.writestr('xl/_rels/workbook.xml.rels', workbook_rels_xml.encode('utf-8'))
            for i, sheet_xml in enumerate(sheets_xml):
                zf.writestr('xl/worksheets/sheet' + str(i+1) + '.xml', sheet_xml.encode('utf-8'))
            zf.writestr('xl/styles.xml', styles_xml.encode('utf-8'))

    try:
        _try_write(output_path)
    except PermissionError:
        import time
        base, ext = os.path.splitext(output_path)
        fallback = base + '_' + str(int(time.time())) + ext
        print('  [注意] Excel文件被占用，另存为: ' + os.path.basename(fallback))
        _try_write(fallback)
        output_path = fallback

    return os.path.abspath(output_path)
