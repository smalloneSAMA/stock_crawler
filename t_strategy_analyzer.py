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
SWING_PERIODS = [5, 10, 20, 30]
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

    for period in SWING_PERIODS:
        lookahead = max(3, int(period * SIGNAL_LOOKAHEAD_RATIO))
        period_result = {}

        for threshold in SWING_THRESHOLDS:
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

            # 技术指标分布统计
            zt_macd_dist = {}
            ft_macd_dist = {}
            for s in zt_signals:
                d = s.get("MACD方向", "N/A")
                zt_macd_dist[d] = zt_macd_dist.get(d, 0) + 1
            for s in ft_signals:
                d = s.get("MACD方向", "N/A")
                ft_macd_dist[d] = ft_macd_dist.get(d, 0) + 1

            period_result[f"threshold_{threshold}"] = {
                "正T": {
                    "信号次数": zt_total,
                    "获胜次数": zt_wins,
                    "胜率%": zt_rate,
                    "平均信号后收益%": zt_avg_fwd,
                    "MACD分布": zt_macd_dist,
                    "信号详情": zt_signals[:10],  # 只保留前10条详情
                },
                "反T": {
                    "信号次数": ft_total,
                    "获胜次数": ft_wins,
                    "胜率%": ft_rate,
                    "平均信号后收益%": ft_avg_fwd,
                    "MACD分布": ft_macd_dist,
                    "信号详情": ft_signals[:10],
                },
            }

        result[period] = period_result

    # ── 汇总：每个周期的最佳梯度 ──
    summary_rows = []
    for period in SWING_PERIODS:
        best_zt = {"threshold": 0, "胜率": 0}
        best_ft = {"threshold": 0, "胜率": 0}
        for th in SWING_THRESHOLDS:
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


def _build_sheet_xml(sheet_name: str, headers: List[str], rows: List[List], col_widths: List[int]) -> str:
    """构建一个 sheet 的 XML"""
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
        cells = ''
        for ci, val in enumerate(row_data):
            cl = col_letters[ci]
            if isinstance(val, (int, float)):
                cells += f'<c r="{cl}{row_num}" s="3"><v>{val}</v></c>'
            else:
                cells += (
                    f'<c r="{cl}{row_num}" s="3" t="inlineStr">'
                    f'<is><t>{xml_escape(str(val) if val is not None else "")}</t></is></c>'
                )
        row_xmls.append(f'<row r="{row_num}" spans="1:{num_cols}" s="3">{cells}</row>')

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
        for period in SWING_PERIODS:
            period_data = swing_result.get(period, {})
            detail_headers = ["阈值%", "方向", "信号次数", "获胜次数", "胜率%", "平均信号后收益%"]
            detail_widths = [8, 8, 10, 10, 8, 16]
            detail_rows = []
            for th in SWING_THRESHOLDS:
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
