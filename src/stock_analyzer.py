#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
股票分析决策引擎 —— 趋势选时 / 估值定仓 / 波动降本

根据已获取的 K 线数据 + 财务数据，生成当日分析决策清单。
投资风格：价值投资 + 做T降本。

三大维度：
  1. 趋势选时  — 基于多周期均线、MACD、RSI、布林带的综合评分
  2. 估值定仓  — 基于历史PE/PB百分位、ROE质量的仓位建议
  3. 波动降本  — 基于ATR、布林带、支撑阻力的网格/做T方案
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple


# ════════════════════════════════════════════════════════════════
#  技术指标计算
# ════════════════════════════════════════════════════════════════

def _ma(data: List[float], period: int) -> Optional[float]:
    """简单移动平均"""
    if len(data) < period:
        return None
    return round(sum(data[:period]) / period, 2)


def _ema(data: List[float], period: int) -> Optional[float]:
    """指数移动平均"""
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period  # 初始 SMA
    for i in range(period, len(data)):
        ema = data[i] * k + ema * (1 - k)
    return round(ema, 2)


def _macd_direction(closes: List[float]) -> str:
    """判断MACD方向"""
    dif = _ema(closes, 12)
    dea = _ema(closes, 26)
    if dif is None or dea is None:
        return "数据不足"
    bar = round(dif - dea, 2)
    # 看最近两期的柱体变化
    if dif > dea and bar > 0:
        return "金叉向上(多头)"
    elif dif < dea and bar < 0:
        return "死叉向下(空头)"
    elif dif > dea:
        return "DIF在DEA上方(偏多)"
    else:
        return "DIF在DEA下方(偏空)"


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """计算 RSI（相对强弱指标）"""
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
    return round(100 - 100 / (1 + rs), 1)


def _bollinger(closes: List[float], period: int = 20, mult: float = 2.0) -> Optional[Dict]:
    """计算布林带"""
    if len(closes) < period:
        return None
    mid = _ma(closes, period)
    if mid is None:
        return None
    # 计算标准差
    samples = closes[:period]
    mean = sum(samples) / period
    variance = sum((x - mean) ** 2 for x in samples) / period
    std = variance ** 0.5
    upper = round(mid + mult * std, 2)
    lower = round(mid - mult * std, 2)
    bandwidth = round((upper - lower) / mid * 100, 2) if mid > 0 else 0
    price = closes[0]
    # 价格在布林带中的位置（0~100%）
    pos = round((price - lower) / (upper - lower) * 100, 1) if upper != lower else 50
    return {
        "上轨": upper,
        "中轨": mid,
        "下轨": lower,
        "带宽%": bandwidth,
        "价格位置%": pos,
        "位置描述": "上轨上方" if pos > 100 else ("下轨下方" if pos < 0 else
                  "上轨附近" if pos > 80 else ("下轨附近" if pos < 20 else "中轨附近")),
    }


def _calc_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """计算 ATR（平均真实波幅）"""
    if len(closes) < period + 1:
        return None
    tr_list = []
    for i in range(period):
        h, l, pc = highs[i], lows[i], closes[i + 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
    return round(sum(tr_list) / len(tr_list), 2)


def _support_resistance_levels(closes: List[float], highs: List[float], lows: List[float], price: float, atr: float):
    """计算多级支撑阻力"""
    recent_highs = highs[:60] if len(highs) >= 60 else highs
    recent_lows = lows[:60] if len(lows) >= 60 else lows
    recent_closes = closes[:60] if len(closes) >= 60 else closes

    h60 = max(recent_highs) if recent_highs else price
    l60 = min(recent_lows) if recent_lows else price

    step = max(atr, (h60 - l60) * 0.05) if atr else (h60 - l60) * 0.05

    return {
        "支撑3(强)": round(price - step * 3, 2),
        "支撑2": round(price - step * 2, 2),
        "支撑1(近)": round(price - step, 2),
        "阻力1(近)": round(price + step, 2),
        "阻力2": round(price + step * 2, 2),
        "阻力3(强)": round(price + step * 3, 2),
        "60日最低": round(l60, 2),
        "60日最高": round(h60, 2),
    }


def _visual_bar(value: float, max_val: float = 100, width: int = 20) -> str:
    """生成视觉进度条，如 [====      ]"""
    filled = max(0, min(width, int(value / max_val * width)))
    return "=" * filled + " " * (width - filled)


# ════════════════════════════════════════════════════════════════
#  一、趋势选时
# ════════════════════════════════════════════════════════════════

def analyze_trend(closes: List[float], highs: List[float], lows: List[float], volumes: List[float]) -> Dict:
    """
    趋势分析 —— 判断该不该动手

    评分体系（满分100）：
      - 均线位置（50分）：价格在MA5/10/20/30/60之上各加10分
      - 均线排列（20分）：短>中>长各加6~7分
      - MACD动量（15分）：金叉/柱体方向
      - RSI动能（10分）：趋势强度
      - 成交量（5分）：量价配合
    """
    if len(closes) < 60:
        return {"score": 50, "verdict": "数据不足", "detail": {}, "raw": {}}

    price = closes[0]
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma30 = _ma(closes, 30)
    ma60 = _ma(closes, 60)
    ma120 = _ma(closes, 120) if len(closes) >= 120 else None

    # 技术指标
    bb = _bollinger(closes, 20, 2)
    rsi_val = _rsi(closes, 14)

    # MACD
    dif = _ema(closes, 12)
    dea = _ema(closes, 26)
    macd_bar = round(dif - dea, 2) if dif and dea else None
    macd_dir = _macd_direction(closes)

    # 计算乖离率（价格偏离均线的百分比）
    def _deviation(ma_val):
        return round((price - ma_val) / ma_val * 100, 2) if ma_val and ma_val > 0 else None

    # -- 评分明细 --
    score = 50
    items = []

    # ① 均线位置（50分）
    for name, val, w in [("MA5", ma5, 10), ("MA10", ma10, 10), ("MA20", ma20, 10),
                          ("MA30", ma30, 10), ("MA60", ma60, 10)]:
        if val:
            deviation = _deviation(val)
            if price > val:
                score += w
                items.append(f"价格↑{name}({val}) 乖离+{deviation}% → +{w}分")
            else:
                score -= w
                items.append(f"价格↓{name}({val}) 乖离{deviation}% → -{w}分")

    # ② 均线排列（20分）
    if ma5 and ma10:
        if ma5 > ma10:
            score += 7
            items.append(f"MA5({ma5}) > MA10({ma10}) 短线多头 → +7分")
        else:
            score -= 7
            items.append(f"MA5({ma5}) < MA10({ma10}) 短线空头 → -7分")
    if ma10 and ma20:
        if ma10 > ma20:
            score += 7
            items.append(f"MA10({ma10}) > MA20({ma20}) 中线多头 → +7分")
        else:
            score -= 7
            items.append(f"MA10({ma10}) < MA20({ma20}) 中线空头 → -7分")
    if ma20 and ma60:
        if ma20 > ma60:
            score += 6
            items.append(f"MA20({ma20}) > MA60({ma60}) 长线多头 → +6分")
        else:
            score -= 6
            items.append(f"MA20({ma20}) < MA60({ma60}) 长线空头 → -6分")

    # ③ MACD 动量（15分）
    if macd_bar is not None:
        if dif and dea and dif > dea and macd_bar > 0:
            score += 15
            items.append(f"MACD金叉中(DIF:{dif},DEA:{dea},柱:{macd_bar}) → +15分")
        elif dif and dea and dif < dea and macd_bar < 0:
            score -= 15
            items.append(f"MACD死叉中(DIF:{dif},DEA:{dea},柱:{macd_bar}) → -15分")
        elif dif and dea and dif > dea:
            score += 5
            items.append(f"MACD偏多(DIF略高) → +5分")
        else:
            score -= 5
            items.append(f"MACD偏空(DEA略高) → -5分")

    # ④ RSI 动能（10分）
    if rsi_val is not None:
        if 40 <= rsi_val <= 60:
            score += 0
            items.append(f"RSI({rsi_val}) 中性区间 → 0分")
        elif rsi_val > 60:
            score += 5
            items.append(f"RSI({rsi_val}) 偏强区间 → +5分")
        elif rsi_val > 80:
            score += 10
            items.append(f"RSI({rsi_val}) 超买区间，注意回调风险 → +10分")
        elif rsi_val < 40:
            score -= 5
            items.append(f"RSI({rsi_val}) 偏弱区间 → -5分")
        elif rsi_val < 20:
            score -= 10
            items.append(f"RSI({rsi_val}) 超卖区间，关注反弹机会 → -10分")

    # ⑤ 成交量趋势（5分）
    if len(volumes) >= 10:
        avg_vol_5 = sum(volumes[:5]) / 5
        avg_vol_10 = sum(volumes[:10]) / 10
        if avg_vol_5 > avg_vol_10 * 1.2:
            score += 5
            items.append(f"近5日均量比近10日+{round((avg_vol_5/avg_vol_10-1)*100,1)}% 放量 → +5分")
        elif avg_vol_5 < avg_vol_10 * 0.8:
            score -= 5
            items.append(f"近5日均量比近10日{round((avg_vol_5/avg_vol_10-1)*100,1)}% 缩量 → -5分")
        else:
            items.append(f"成交量平稳 → 0分")

    # 附加：布林带位置
    bb_pos_text = ""
    if bb:
        bb_pos_text = f"布林带{bb['位置描述']}({bb['价格位置%']}%)"

    score = max(0, min(100, score))

    # -- 结论 --
    if score >= 70:
        verdict = "[加仓]"
        reason = "趋势偏多，可择机加仓"
    elif score >= 50:
        verdict = "[持仓观望]"
        reason = "趋势中性，持仓等待方向"
    elif score >= 30:
        verdict = "[谨慎减仓]"
        reason = "趋势偏弱，逢反弹减仓"
    else:
        verdict = "[减仓/离场]"
        reason = "趋势明显弱势，减仓控制风险"

    # -- 近期涨跌幅 --
    chg_5d = round((closes[0] - closes[5]) / closes[5] * 100, 2) if len(closes) >= 6 else None
    chg_20d = round((closes[0] - closes[min(19, len(closes)-1)]) / closes[min(19, len(closes)-1)] * 100, 2) if len(closes) >= 20 else None
    chg_60d = round((closes[0] - closes[min(59, len(closes)-1)]) / closes[min(59, len(closes)-1)] * 100, 2) if len(closes) >= 60 else None

    return {
        "score": score,
        "score_bar": _visual_bar(score, 100, 20),
        "verdict": verdict,
        "reason": reason,
        "detail": {
            "当前价": price,
            "均线": {
                "MA5": ma5, "MA10": ma10, "MA20": ma20,
                "MA30": ma30, "MA60": ma60,
                "MA120": ma120,
                "均线排列": ("多头排列" if all(x and price > x for x in [ma5,ma10,ma20,ma60] if x)
                           else "空头排列" if all(x and price < x for x in [ma5,ma10,ma20,ma60] if x)
                           else "交叉震荡"),
            },
            "乖离率%": {
                "MA5": _deviation(ma5),
                "MA10": _deviation(ma10),
                "MA20": _deviation(ma20),
                "MA60": _deviation(ma60),
            },
            "技术指标": {
                "MACD": {"DIF": dif, "DEA": dea, "柱": macd_bar, "方向": macd_dir},
                "RSI(14)": rsi_val,
                "RSI判断": ("超买" if rsi_val and rsi_val > 80 else
                          "偏强" if rsi_val and rsi_val > 60 else
                          "偏弱" if rsi_val and rsi_val < 40 else
                          "超卖" if rsi_val and rsi_val < 20 else
                          "中性" if rsi_val else "N/A"),
                "布林带": bb,
                "布林带提示": bb_pos_text,
            },
            "涨跌幅": {"5日": chg_5d, "20日": chg_20d, "60日": chg_60d},
            "评分明细": items,
        },
    }


# ════════════════════════════════════════════════════════════════
#  二、估值定仓
# ════════════════════════════════════════════════════════════════

def analyze_valuation(data: List[Dict]) -> Dict:
    """
    估值分析 —— 基于历史百分位判断贵贱，结合质量确定仓位

    评分综合：PE百分位(30%) + PB百分位(30%) + ROE质量(40%)
    """
    pe_list, pb_list, roe_list, dy_list = [], [], [], []
    for row in data:
        pe = row.get("市盈率TTM")
        pb = row.get("市净率")
        roe = row.get("净资产收益率%")
        dy = row.get("股息率TTM")
        if pe and 0 < pe < 500: pe_list.append(pe)
        if pb and 0 < pb < 100: pb_list.append(pb)
        if roe is not None: roe_list.append(roe)
        if dy and dy > 0: dy_list.append(dy)

    latest = data[0]
    cur_pe, cur_pb, cur_roe, cur_dy, cur_eps = (
        latest.get("市盈率TTM"), latest.get("市净率"),
        latest.get("净资产收益率%"), latest.get("股息率TTM"),
        latest.get("每股收益"))
    cur_ps = latest.get("市销率")
    cur_mc = latest.get("当前市值(亿)")

    if not pe_list or not pb_list:
        return {"score": 50, "verdict": "数据不足", "detail": {}, "raw": {}}

    # -- PE 百分位 --
    pe_sorted = sorted(pe_list)
    pe_min, pe_max = pe_sorted[0], pe_sorted[-1]
    pe_median = pe_sorted[len(pe_sorted)//2]
    pe_below = sum(1 for p in pe_sorted if p <= cur_pe) if cur_pe else 0
    pe_pct = round(pe_below / len(pe_sorted) * 100, 1) if cur_pe else 50
    pe_score = max(0, 100 - pe_pct)

    # -- PB 百分位 --
    pb_sorted = sorted(pb_list)
    pb_min, pb_max = pb_sorted[0], pb_sorted[-1]
    pb_median = pb_sorted[len(pb_sorted)//2]
    pb_below = sum(1 for p in pb_sorted if p <= cur_pb) if cur_pb else 0
    pb_pct = round(pb_below / len(pb_sorted) * 100, 1) if cur_pb else 50
    pb_score = max(0, 100 - pb_pct)

    # -- ROE 质量 --
    avg_roe = round(sum(roe_list) / len(roe_list), 2) if roe_list else 0
    recent_roe = round(sum(roe_list[:min(4, len(roe_list))]) / min(4, len(roe_list)), 2) if roe_list else 0
    roe_score = min(100, recent_roe * 4)

    # -- 股息率评分 --
    dy_score = min(100, (cur_dy or 0) * 20)  # 5%股息率 → 满分

    # -- 综合 --
    val_score = round(pe_score * 0.25 + pb_score * 0.25 + roe_score * 0.35 + dy_score * 0.15, 1)
    val_score = max(0, min(100, val_score))

    # -- 仓位 --
    if val_score >= 80:
        position, pct, pos_reason = "90% (重仓)", 90, "低估区间 + 优质ROE → 适合大仓位布局"
    elif val_score >= 65:
        position, pct, pos_reason = "70% (中高仓)", 70, "偏低估值 → 中等偏上仓位"
    elif val_score >= 45:
        position, pct, pos_reason = "50% (半仓)", 50, "合理估值区间 → 半仓操作"
    elif val_score >= 25:
        position, pct, pos_reason = "30% (低仓)", 30, "偏高估值 → 轻仓参与"
    else:
        position, pct, pos_reason = "10% (观察仓)", 10, "高估区间 → 仅观察仓"

    # -- 估值温度计 --
    temp_bar = _visual_bar(100 - val_score, 100, 20)
    temp_label = "过热" if val_score < 25 else ("偏高" if val_score < 45 else
                  "合理" if val_score < 65 else "偏低" if val_score < 80 else "低估")

    # -- PE分位可视化 --
    pe_bar = _visual_bar(pe_pct, 100, 20)
    pb_bar = _visual_bar(pb_pct, 100, 20)

    return {
        "score": val_score,
        "score_bar": _visual_bar(val_score, 100, 20),
        "verdict": position,
        "reason": pos_reason,
        "detail": {
            "当前估值": {
                "PE(TTM)": cur_pe, "PB": cur_pb, "PS(TTM)": cur_ps,
                "ROE%": cur_roe, "股息率TTM%": cur_dy, "每股收益": cur_eps,
            },
            "历史PE对比": {
                "当前": cur_pe, "最低": round(pe_min, 2), "中位": round(pe_median, 2),
                "最高": round(pe_max, 2),
                "百分位": f"{pe_pct}%",
                "可视化": f"[最低{pe_min:.1f}] {pe_bar} [最高{pe_max:.1f}]  <-当前",
            },
            "历史PB对比": {
                "当前": cur_pb, "最低": round(pb_min, 2), "中位": round(pb_median, 2),
                "最高": round(pb_max, 2),
                "百分位": f"{pb_pct}%",
                "可视化": f"[最低{pb_min:.1f}] {pb_bar} [最高{pb_max:.1f}]  <-当前",
            },
            "质量评估": {
                "近3年均ROE%": avg_roe,
                "近1年均ROE%": recent_roe,
                "ROE质量": ("优秀(>20%)" if recent_roe > 20 else
                          "良好(15~20%)" if recent_roe > 15 else
                          "一般(10~15%)" if recent_roe > 10 else
                          "较差(<10%)"),
                "股息率水平": ("高股息" if cur_dy and cur_dy > 4 else
                            "中等股息" if cur_dy and cur_dy > 2 else "低股息"),
            },
            "估值温度计": f"{temp_bar}  {temp_label}",
            "评分构成": {
                "PE分值(权重25%)": f"{pe_score}分 (PE百分位{pe_pct}%, 越低分越高)",
                "PB分值(权重25%)": f"{pb_score}分 (PB百分位{pb_pct}%, 越低分越高)",
                "ROE分值(权重35%)": f"{roe_score}分 (ROE={recent_roe}%, 越高分越高)",
                "股息分值(权重15%)": f"{dy_score}分 (股息率{cur_dy}%, 越高分越高)",
            },
        },
        "raw": {"pe_list": pe_list, "pb_list": pb_list},
    }


# ════════════════════════════════════════════════════════════════
#  三、波动降本
# ════════════════════════════════════════════════════════════════


def _analyze_t_gradient_stats(data: List[Dict], max_gradient: int = 10) -> Dict:
    """
    T梯度统计分析 —— 按1%梯度统计上涨/下跌幅度的胜率

    对全量历史数据（配置时间范围内），按 1% 为梯度统计：
      - 正T胜率: 上涨幅度% ≥ 目标梯度 的天数占比
        （即：做多T时，日内涨幅达到该梯度的概率）
      - 反T胜率: |下跌幅度%| ≥ 目标梯度 的天数占比
        （即：做空T时，日内跌幅达到该梯度的概率）

    Args:
        data: K线数据（最新在前），需包含 上涨幅度%、下跌幅度% 字段
        max_gradient: 最大统计梯度（默认10%，即统计1%~10%）

    Returns:
        {
            "总交易日": N,
            "梯度列表": [1, 2, 3, ...],
            "正T胜率": [65.2, 42.1, ...],       # 每个梯度的胜率%
            "反T胜率": [58.3, 36.7, ...],
            "正T样本数": [850, 550, ...],        # 达到该梯度的天数
            "反T样本数": [760, 480, ...],
            "最佳正T梯度": {"梯度": 1, "胜率": 65.2},
            "均衡梯度": {"梯度": 2, "正T胜率": 42.1, "反T胜率": 36.7},
        }
    """
    # 提取上涨幅度% 和 下跌幅度%
    up_amps = []   # 上涨幅度% (正值)
    down_amps = [] # 下跌幅度%的绝对值 (正值)
    for row in data:
        up = row.get("上涨幅度%")
        down = row.get("下跌幅度%")
        if up is not None and isinstance(up, (int, float)):
            up_amps.append(abs(up))
        if down is not None and isinstance(down, (int, float)):
            down_amps.append(abs(down))

    total_days = len(data)
    if not up_amps:
        return {"总交易日": total_days, "数据不足": True}

    gradients = list(range(1, max_gradient + 1))
    up_rates = []     # 正T胜率
    down_rates = []   # 反T胜率
    up_counts = []    # 正T达标天数
    down_counts = []  # 反T达标天数

    for g in gradients:
        uc = sum(1 for a in up_amps if a >= g)
        dc = sum(1 for a in down_amps if a >= g)
        up_counts.append(uc)
        down_counts.append(dc)
        up_rates.append(round(uc / total_days * 100, 1) if total_days > 0 else 0)
        down_rates.append(round(dc / total_days * 100, 1) if total_days > 0 else 0)

    # 找最佳正T梯度（胜率 >= 50% 的最大梯度）
    best_up_g = 0
    for i, r in enumerate(up_rates):
        if r >= 50:
            best_up_g = gradients[i]

    # 找均衡梯度（正T和反T胜率都 >= 30% 的最大梯度）
    balanced_g = 0
    for i in range(len(gradients)):
        if up_rates[i] >= 30 and down_rates[i] >= 30:
            balanced_g = gradients[i]

    return {
        "总交易日": total_days,
        "数据不足": False,
        "梯度列表": gradients,
        "正T胜率": up_rates,
        "反T胜率": down_rates,
        "正T样本数": up_counts,
        "反T样本数": down_counts,
        "最佳正T梯度": {
            "梯度": best_up_g,
            "胜率": up_rates[best_up_g - 1] if best_up_g > 0 else 0,
        } if best_up_g > 0 else None,
        "均衡梯度": {
            "梯度": balanced_g,
            "正T胜率": up_rates[balanced_g - 1] if balanced_g > 0 else 0,
            "反T胜率": down_rates[balanced_g - 1] if balanced_g > 0 else 0,
        } if balanced_g > 0 else None,
    }


def analyze_volatility(data: List[Dict], config: Optional[Dict] = None) -> Dict:
    """
    波动率分析 —— 为做T和网格提供具体价格、数量及风险评估

    Args:
        data: K线数据（最新在前）
        config: 可选参数（grid_levels, atr_multiplier, t_single_qty）
    """
    if len(data) < 20:
        return {"verdict": "数据不足(需≥20条)", "detail": {}}

    closes = [row.get("收盘价", 0) or 0 for row in data]
    highs  = [row.get("最高价", 0) or 0 for row in data]
    lows   = [row.get("最低价", 0) or 0 for row in data]
    volumes = [row.get("成交量(万手)", 0) or 0 for row in data]

    price = closes[0]
    atr14 = _calc_atr(highs, lows, closes, 14)
    if atr14 is None or atr14 == 0:
        return {"verdict": "ATR计算失败", "detail": {}}

    atr_pct = round(atr14 / price * 100, 2)  # ATR相对值

    # -- 波动特征 --
    amps = [row.get("振幅%", 0) or 0 for row in data[:20]]
    avg_amp = round(sum(amps) / len(amps), 2)
    max_amp = round(max(amps), 2)
    min_amp = round(min(amps), 2)
    amp_std = round((sum((a - avg_amp)**2 for a in amps) / len(amps))**0.5, 2)

    # 波动稳定性
    if amp_std < 1:
        stability = "稳定"
    elif amp_std < 2:
        stability = "较稳定"
    elif amp_std < 3:
        stability = "波动较大"
    else:
        stability = "剧烈波动"

    # -- 布林带 --
    bb = _bollinger(closes, 20, 2)

    # -- 支撑阻力 --
    sr = _support_resistance_levels(closes, highs, lows, price, atr14)

    # -- 近期涨跌统计 --
    up_days = sum(1 for i in range(min(20, len(closes)-1)) if closes[i] > closes[i+1])
    down_days = min(20, len(closes)-1) - up_days
    win_rate = round(up_days / (up_days + down_days) * 100, 1)

    # -- 读取配置 --
    cfg = config or {}
    grid_levels = cfg.get("grid_levels", 4)
    atr_mult = cfg.get("atr_multiplier", 0.5)
    single_t_qty = cfg.get("t_single_qty", None)

    # 计算单笔股数
    if single_t_qty is None:
        avg_amount = sum((row.get("成交额（亿）", 0) or 0) for row in data[:20]) / min(20, len(data))
        if avg_amount > 0 and price > 0:
            single_t_qty = max(100, int(avg_amount * 1e7 / price / 1000 / 100) * 100)
        else:
            single_t_qty = 200

    # -- 日内T策略 --
    t_buy_low  = round(price - atr14 * 0.5, 2)
    t_buy_high = round(price - atr14 * 0.2, 2)
    t_sell_low  = round(price + atr14 * 0.2, 2)
    t_sell_high = round(price + atr14 * 0.5, 2)
    t_profit = round(atr14 * 0.4, 2)
    t_profit_pct = round(atr14 * 0.4 / price * 100, 2)

    # -- 网格 --
    grid_spacing = round(atr14 * atr_mult, 2)
    buy_levels = [round(price - grid_spacing * i, 2) for i in range(1, grid_levels + 1)]
    sell_levels = [round(price + grid_spacing * i, 2) for i in range(1, grid_levels + 1)]

    # 每档投入估算（以单笔股数×价格）
    tier_cost = round(single_t_qty * price / 10000, 2)
    total_grid_cost = round(tier_cost * grid_levels, 2)

    # 网格获利估算
    grid_profit_per_tier = round(grid_spacing * single_t_qty, 2)
    grid_profit_total = round(grid_profit_per_tier * grid_levels, 2)

    # -- 网格触及概率估算（基于 ATR 和近期波动）--
    atr_steps = [round(price - atr14 * i, 2) for i in range(1, 4)]
    prob_note = []
    for i, level in enumerate(buy_levels):
        dist_pct = round((price - level) / price * 100, 2)
        prob_note.append(f"买入{i+1}档({level}): 需下跌{dist_pct}%")

    # -- 近期连续涨跌统计 --
    recent_5d_up = sum(1 for i in range(min(5, len(closes)-1)) if closes[i] > closes[i+1])
    recent_5d_down = min(5, len(closes)-1) - recent_5d_up

    # -- T梯度统计（基于全量历史数据） --
    t_gradient_stats = _analyze_t_gradient_stats(data, max_gradient=10)

    return {
        "verdict": "波动分析完成",
        "detail": {
            "波动特征": {
                "ATR(14)": atr14,
                "ATR/股价%": f"{atr_pct}%",
                "近20日均振幅%": avg_amp,
                "近20日最大振幅%": max_amp,
                "振幅标准差": amp_std,
                "波动稳定性": stability,
                "波动评级": "高波动(适合做T)" if avg_amp > 4 else (
                           "中波动(适合网格)" if avg_amp > 2.5 else "低波动(需耐心)"),
            },
            "近期统计": {
                "近20日涨跌比": f"{up_days}涨/{down_days}跌",
                "胜率%": win_rate,
                "近5日": f"{recent_5d_up}涨{recent_5d_down}跌",
                "布林带": bb,
            },
            "关键价位": sr,
            "日内T策略": {
                "买入区间": f"{t_buy_low} ~ {t_buy_high}",
                "卖出区间": f"{t_sell_low} ~ {t_sell_high}",
                "单次T利润": f"{t_profit}元/股 ({t_profit_pct}%)",
                "单笔数量(股)": single_t_qty,
                "单笔动用资金": f"{tier_cost}万元",
                "操作建议": f"回踩{t_buy_low}~{t_buy_high}分批接，反弹至{t_sell_low}~{t_sell_high}分批出",
                "策略说明": "急跌至买入区间分批接，拉升至卖出区间分批出，不恋战",
            },
            "多日波段T网格": {
                "基准价": price,
                "网格间距": grid_spacing,
                f"买入{grid_levels}档": buy_levels,
                f"卖出{grid_levels}档": sell_levels,
                "每档数量(股)": single_t_qty,
                "每档资金": f"{tier_cost}万元",
                "总网格资金": f"{total_grid_cost}万元",
                "单次网格利润": f"{grid_profit_per_tier}元/档",
                "满格总利润": f"{grid_profit_total}",
                "各档位说明": prob_note,
                "策略说明": f"每跌{grid_spacing}买一档 (共{grid_levels}档)，每涨{grid_spacing}卖一档，滚动操作降成本",
            },
            "T梯度统计": t_gradient_stats,
        },
    }


# ════════════════════════════════════════════════════════════════
#  综合：生成决策报告
# ════════════════════════════════════════════════════════════════

def generate_decision_report(
    stock_name: str, stock_code: str, data: List[Dict],
    analysis_config: Optional[Dict] = None,
) -> str:
    """生成完整的当日分析决策清单"""
    closes = [row.get("收盘价", 0) or 0 for row in data]
    highs  = [row.get("最高价", 0) or 0 for row in data]
    lows   = [row.get("最低价", 0) or 0 for row in data]
    volumes = [row.get("成交量(万手)", 0) or 0 for row in data]

    trend = analyze_trend(closes, highs, lows, volumes)
    valuation = analyze_valuation(data)
    volatility = analyze_volatility(data, analysis_config)

    ts = trend["score"]
    vs = valuation.get("score", 50)
    td = trend["detail"]
    vd = valuation.get("detail", {})
    vod = volatility.get("detail", {})

    # -- 综合判断 --
    if ts >= 70 and vs >= 60:
        action, action_reason = "[积极加仓]", "趋势向好 + 估值偏低 → 可加仓"
    elif ts >= 70 and vs < 40:
        action, action_reason = "[谨慎追高]", "趋势虽好但估值已高 → 控制仓位"
    elif ts < 40 and vs >= 60:
        action, action_reason = "[分批建仓]", "估值有吸引力 → 等待企稳后分批加仓"
    elif ts < 40 and vs < 40:
        action, action_reason = "[减仓观望]", "趋势弱 + 估值高 → 减仓控制风险"
    else:
        action, action_reason = "[持仓+做T]", "震荡格局 → 持股同时做T降本"

    lines = []
    def L(s=""): lines.append(s)
    def sep(): L("-" * 62)

    # ═══════ 标题 ═══════
    L("=" * 62)
    L(f"  {stock_name} ({stock_code})  当日分析决策清单")
    L(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    L("=" * 62)
    L()

    # ═══════ 综合行动 ═══════
    sep()
    L(f"  [综合行动] {action}")
    L(f"  {action_reason}")
    L(f"  趋势评分: {ts}/100  {trend['score_bar']}")
    L(f"  估值评分: {vs}/100  {valuation.get('score_bar','')}")
    L(f"  建议仓位: {valuation['verdict']}")
    sep()
    L()

    # ═══════ 一、趋势选时 ═══════
    L("一、趋势选时 -- 现在该不该动手")
    L(f"  -- 价格位置 --")
    L(f"  当前价: {td['当前价']}")
    L(f"  均线排列: {td.get('均线',{}).get('均线排列','')}")
    for k in ['MA5','MA10','MA20','MA30','MA60']:
        v = td.get('均线',{}).get(k)
        if isinstance(v, (int, float)):
            dev = td.get('乖离率%', {}).get(k, '')
            dev_str = f" (乖离{dev:+.2f}%)" if isinstance(dev, (int, float)) else ""
            L(f"    {k}: {v}{dev_str}")
    L()

    L(f"  -- 技术指标 --")
    tech = td.get("技术指标", {})
    macd = tech.get("MACD", {})
    if macd:
        L(f"  MACD: DIF={macd.get('DIF','?')}, DEA={macd.get('DEA','?')}, 柱={macd.get('柱','?')}")
        L(f"  方向: {macd.get('方向','?')}")
    rsi_val = tech.get("RSI(14)")
    rsi_judge = tech.get("RSI判断", "")
    L(f"  RSI(14): {rsi_val}  ({rsi_judge})")
    bb = tech.get("布林带", {})
    if bb:
        L(f"  布林带: 上轨={bb.get('上轨','?')}  中轨={bb.get('中轨','?')}  下轨={bb.get('下轨','?')}")
        L(f"  带宽: {bb.get('带宽%','?')}%  |  价格位置: {bb.get('位置描述','?')}({bb.get('价格位置%','?')}%)")
    L()

    chg = td.get("涨跌幅", {})
    L(f"  -- 近期涨跌 --")
    for k, v in chg.items():
        if v is not None:
            L(f"  {k}: {v:+.2f}%")
    L()

    L(f"  -- 评分明细 --")
    for item in td.get("评分明细", []):
        L(f"    - {item}")
    L(f"  最终评分: {ts}/100  {trend['score_bar']}")
    L(f"  -> 结论: {trend['verdict']} -- {trend['reason']}")
    L()

    # ═══════ 二、估值定仓 ═══════
    L("二、估值定仓 -- 如果动手，仓位给多少")
    cv = vd.get("当前估值", {})
    L(f"  -- 当前估值概览 --")
    L(f"  PE(TTM): {cv.get('PE(TTM)','?')}  |  PB: {cv.get('PB','?')}  |  PS(TTM): {cv.get('PS(TTM)','?')}")
    L(f"  ROE: {cv.get('ROE%','?')}%  |  股息率: {cv.get('股息率TTM%','?')}%  |  EPS: {cv.get('每股收益','?')}")
    L()

    pe_hist = vd.get("历史PE对比", {})
    L(f"  -- PE历史对比 --")
    L(f"  当前: {pe_hist.get('当前','?')}  |  最低: {pe_hist.get('最低','?')}  |  中位: {pe_hist.get('中位','?')}  |  最高: {pe_hist.get('最高','?')}")
    L(f"  百分位: {pe_hist.get('百分位','?')}")
    L(f"  {pe_hist.get('可视化','')}")
    L()
    pb_hist = vd.get("历史PB对比", {})
    L(f"  -- PB历史对比 --")
    L(f"  当前: {pb_hist.get('当前','?')}  |  最低: {pb_hist.get('最低','?')}  |  中位: {pb_hist.get('中位','?')}  |  最高: {pb_hist.get('最高','?')}")
    L(f"  百分位: {pb_hist.get('百分位','?')}")
    L(f"  {pb_hist.get('可视化','')}")
    L()

    qa = vd.get("质量评估", {})
    L(f"  -- 质量评估 --")
    L(f"  近3年均ROE: {qa.get('近3年均ROE%','?')}%  |  ROE质量: {qa.get('ROE质量','?')}")
    L(f"  股息率水平: {qa.get('股息率水平','?')}")
    L()

    temp = vd.get("估值温度计", "")
    L(f"  估值温度计: {temp}")
    L()

    score_detail = vd.get("评分构成", {})
    L(f"  -- 评分构成 --")
    for k, v in score_detail.items():
        L(f"    - {k}: {v}")
    L(f"  最终估值评分: {vs}/100  {valuation.get('score_bar','')}")
    L(f"  -> 建议仓位: {valuation['verdict']} -- {valuation['reason']}")
    L()

    # ═══════ 三、波动降本 ═══════
    L("三、波动降本 -- 做T/网格的具体价格和数量")
    vol = vod.get("波动特征", {})
    L(f"  -- 波动特征 --")
    L(f"  ATR(14): {vol.get('ATR(14)','?')} (占股价{vol.get('ATR/股价%','?')})")
    L(f"  近20日均振幅: {vol.get('近20日均振幅%','?')}%  |  最大振幅: {vol.get('近20日最大振幅%','?')}%")
    L(f"  波动稳定性: {vol.get('波动稳定性','?')}  |  评级: {vol.get('波动评级','?')}")
    L()

    stat = vod.get("近期统计", {})
    L(f"  -- 近期统计 --")
    L(f"  近20日: {stat.get('近20日涨跌比','?')}  |  胜率: {stat.get('胜率%','?')}%")
    L(f"  近5日: {stat.get('近5日','?')}")
    bb_stat = stat.get("布林带", {})
    if bb_stat:
        L(f"  布林带带宽: {bb_stat.get('带宽%','?')}%  |  价格位置: {bb_stat.get('位置描述','?')}")
    L()

    kr = vod.get("关键价位", {})
    if kr:
        L(f"  -- 关键价位 --")
        L(f"  支撑位: 强{sr_format(kr,'支撑3(强)')}  |  中{sr_format(kr,'支撑2')}  |  近{sr_format(kr,'支撑1(近)')}")
        L(f"  阻力位: 近{sr_format(kr,'阻力1(近)')}  |  中{sr_format(kr,'阻力2')}  |  强{sr_format(kr,'阻力3(强)')}")
        L(f"  60日区间: {kr.get('60日最低','?')} ~ {kr.get('60日最高','?')}")
    L()

    intra = vod.get("日内T策略", {})
    if intra:
        L(f"  -- [日内T策略] --")
        L(f"  买入区间: {intra.get('买入区间','?')}")
        L(f"  卖出区间: {intra.get('卖出区间','?')}")
        L(f"  单次T利润: {intra.get('单次T利润','?')}")
        L(f"  单笔数量: {intra.get('单笔数量(股)','?')}股 (动用约{intra.get('单笔动用资金','?')})")
        L(f"  操作: {intra.get('操作建议','?')}")
        L(f"  -> {intra.get('策略说明','')}")
    L()

    grid = vod.get("多日波段T网格", {})
    if grid:
        L(f"  -- [多日波段T网格] --")
        L(f"  基准价: {grid.get('基准价','?')}  |  网格间距: {grid.get('网格间距','?')}")
        buy_lvls = []
        sell_lvls = []
        for k, v in grid.items():
            if '买入' in k and '档' in k: buy_lvls = v
            if '卖出' in k and '档' in k: sell_lvls = v
        for i, (b, s) in enumerate(zip(buy_lvls, sell_lvls), 1):
            L(f"    档{i}:  买入 {b}  /  卖出 {s}")
        L(f"  每档数量: {grid.get('每档数量(股)','?')}股 (每档约{grid.get('每档资金','?')})")
        L(f"  总网格资金: {grid.get('总网格资金','?')}")
        L(f"  满格利润: {grid.get('满格总利润','?')}元")
        L()
        notes = grid.get("各档位说明", [])
        if notes:
            L(f"  -- 各档位下跌幅度 --")
            for note in notes:
                L(f"    - {note}")
        L(f"  -> {grid.get('策略说明','')}")
    L()

    # ── T梯度统计分析 ──
    tg = vod.get("T梯度统计", {})
    if tg and not tg.get("数据不足", True):
        L(f"  -- [T梯度统计分析] --")
        L(f"  基于 {tg.get('总交易日',0)} 个交易日的 上涨幅度%/下跌幅度% 统计")
        L()
        # 表头
        L(f"  {'梯度':>6}  {'正T胜率':>10}  {'正T天数':>8}  {'反T胜率':>10}  {'反T天数':>8}  {'推荐策略':>14}")
        L(f"  {'-'*62}")

        grads = tg.get("梯度列表", [])
        up_rates = tg.get("正T胜率", [])
        up_counts = tg.get("正T样本数", [])
        down_rates = tg.get("反T胜率", [])
        down_counts = tg.get("反T样本数", [])

        for i in range(len(grads)):
            g = grads[i]
            ur = up_rates[i] if i < len(up_rates) else 0
            uc = up_counts[i] if i < len(up_counts) else 0
            dr = down_rates[i] if i < len(down_rates) else 0
            dc = down_counts[i] if i < len(down_counts) else 0

            # 推荐策略
            if ur >= 60:
                tip = "正T优先"
            elif dr >= 60:
                tip = "反T优先"
            elif ur >= 40 and dr >= 40:
                tip = "正反皆可"
            elif ur >= 40:
                tip = "可做正T"
            elif dr >= 40:
                tip = "可做反T"
            else:
                tip = "胜率偏低"

            L(f"  {f'{g}%':>6}  {ur:>8.1f}%  {uc:>8}  {dr:>8.1f}%  {dc:>8}  {tip:>14}")

        L()
        best_up = tg.get("最佳正T梯度")
        balanced = tg.get("均衡梯度")
        if best_up:
            L(f"  -> [正T建议] 推荐梯度: {best_up['梯度']}% (胜率 {best_up['胜率']}%)")
        if balanced:
            L(f"  -> [均衡建议] {balanced['梯度']}% 梯度正反T胜率均衡"
              f" (正T:{balanced['正T胜率']}% / 反T:{balanced['反T胜率']}%)")
        L()

    # ═══════ 底部 ═══════
    sep()
    L("  参考说明:")
    L("  1. 趋势评分 > 70 可加仓，< 30 需减仓")
    L("  2. 估值评分 > 60 可重仓，< 40 应轻仓")
    L("  3. 网格每触发一档即完成一次低买高卖，滚动降成本")
    L("  4. 做T不贪心，日内T有利润就走")
    L()
    L("  免责声明: 以上分析仅供参考，不构成投资建议。")
    L("  投资有风险，交易需谨慎。")
    sep()

    return "\n".join(lines)


def sr_format(d: dict, key: str) -> str:
    v = d.get(key)
    return str(v) if v is not None else "?"


def analyze_and_print(
    stock_name: str, stock_code: str, data: List[Dict],
    analysis_config: Optional[Dict] = None,
) -> str:
    """分析并打印报告，返回报告文本"""
    report = generate_decision_report(stock_name, stock_code, data, analysis_config)
    print("\n" + report)
    return report


def generate_buy_sell_report_md(
    stock_name: str,
    stock_code: str,
    data: List[Dict],
    analysis_config: Optional[Dict] = None,
    intraday_result: Optional[Dict] = None,
    swing_result: Optional[Dict] = None,
    swing_signals: Optional[List[Dict]] = None,
    distribution_result: Optional[Dict] = None,
) -> str:
    """
    生成 Markdown 格式的买卖建议报告

    综合趋势分析 + 估值分析 + 波动降本分析 + 波段T信号 + 日内做T统计 + 波段涨跌幅分布
    """
    closes = [row.get("收盘价", 0) or 0 for row in data]
    highs  = [row.get("最高价", 0) or 0 for row in data]
    lows   = [row.get("最低价", 0) or 0 for row in data]
    volumes = [row.get("成交量(万手)", 0) or 0 for row in data]

    trend = analyze_trend(closes, highs, lows, volumes)
    valuation = analyze_valuation(data)
    volatility = analyze_volatility(data, analysis_config)

    td = trend["detail"]
    vd = valuation.get("detail", {})
    vod = volatility.get("detail", {})

    latest = data[0] if data else {}
    price = latest.get("收盘价", "?")
    latest_date = latest.get("日期", "")
    total_days = len(data)

    lines = []
    def L(s=""): lines.append(s)

    now_str = datetime.now().strftime("%Y-%m-%d")
    code_clean = stock_code.replace(".SZ", "").replace(".SH", "")

    # ═══════ 标题 ═══════
    L(f"### **{stock_name}([{stock_code}](https://{code_clean}.sz/)) - 次日交易决策支持报告**")
    L()
    L(f"**报告日期：** {now_str}")
    L(f"**分析数据截止：** {latest_date}")
    L(f"**目标交易日期：** {latest_date}次日")
    L()
    L("---")
    L()

    # ═══════ 一、股票基础信息与估值分位 ═══════
    L("### **一、 股票基础信息与估值分位**")
    L()

    cv = vd.get("当前估值", {})
    cur_pe = cv.get("PE(TTM)", "?")
    cur_pb = cv.get("PB", "?")
    cur_roe = cv.get("ROE%", "?")
    cur_dy = cv.get("股息率TTM%", "?")
    cur_eps = cv.get("每股收益", "?")
    cur_mc = latest.get("当前市值(亿)", "?")

    # 价格百分位计算
    sorted_closes = sorted([row.get("收盘价", 0) or 0 for row in data if row.get("收盘价")])
    if sorted_closes and price != "?":
        below = sum(1 for c in sorted_closes if c <= price)
        price_pct = round(below / len(sorted_closes) * 100, 1)
        price_min = round(min(sorted_closes), 2)
        price_median = round(sorted_closes[len(sorted_closes)//2], 2)
        price_max = round(max(sorted_closes), 2)
        price_pct_str = f"**{price_pct}%**"
        price_note = f"基于{total_days}个交易日数据统计（最低{price_min}元，中位{price_median}元，最高{price_max}元）。"
        if price_pct >= 90:
            price_note += "当前价格处于历史极高位。"
        elif price_pct >= 70:
            price_note += "当前价格处于历史偏高位。"
        elif price_pct >= 30:
            price_note += "当前价格处于历史中位区间。"
        else:
            price_note += "当前价格处于历史低位区间。"
    else:
        price_pct_str = "N/A"
        price_note = "数据不足"

    # PE百分位
    pe_hist = vd.get("历史PE对比", {})
    pe_pct_str = pe_hist.get("百分位", "N/A")
    pe_min = pe_hist.get("最低", "?")
    pe_med = pe_hist.get("中位", "?")
    pe_max = pe_hist.get("最高", "?")

    # PB百分位
    pb_hist = vd.get("历史PB对比", {})
    pb_pct_str = pb_hist.get("百分位", "N/A")
    pb_min = pb_hist.get("最低", "?")
    pb_med = pb_hist.get("中位", "?")
    pb_max = pb_hist.get("最高", "?")

    # ROE质量
    qa = vd.get("质量评估", {})
    roe_quality = qa.get("ROE质量", "?")

    L("| 项目 | 数值 | 历史分位 | 说明 |")
    L("| --- | --- | --- | --- |")
    L(f"| **最新收盘价** | {price} 元 | {price_pct_str} | {price_note} |")
    pe_pct_display = pe_pct_str if pe_pct_str != "N/A" else "N/A"
    pe_note = f"基于历史数据统计（最低{pe_min}，中位{pe_med}，最高{pe_max}）。"
    if "%" in str(pe_pct_str):
        pe_val = float(str(pe_pct_str).replace("%", ""))
        if pe_val >= 80:
            pe_note += "当前PE处于历史高位，估值偏贵。"
        elif pe_val >= 60:
            pe_note += "当前PE处于历史中偏高位。"
        elif pe_val >= 40:
            pe_note += "当前PE处于历史中位区间。"
        else:
            pe_note += "当前PE处于历史低位，估值有吸引力。"
    L(f"| **市盈率TTM** | {cur_pe} | **{pe_pct_str}** | {pe_note} |")
    pb_note = f"基于历史数据统计（最低{pb_min}，中位{pb_med}，最高{pb_max}）。"
    if "%" in str(pb_pct_str):
        pb_val = float(str(pb_pct_str).replace("%", ""))
        if pb_val >= 80:
            pb_note += "当前PB处于历史高位。"
        elif pb_val >= 60:
            pb_note += "当前PB处于历史中偏高位。"
        elif pb_val >= 40:
            pb_note += "当前PB处于历史中位区间。"
        else:
            pb_note += "当前PB处于历史低位。"
    L(f"| **市净率MRQ** | {cur_pb} | **{pb_pct_str}** | {pb_note} |")

    # 市值百分位
    mc_values = [row.get("当前市值(亿)", 0) or 0 for row in data if row.get("当前市值(亿)")]
    if mc_values and cur_mc != "?":
        mc_sorted = sorted(mc_values)
        mc_below = sum(1 for m in mc_sorted if m <= cur_mc)
        mc_pct = round(mc_below / len(mc_sorted) * 100, 1)
        mc_min = round(min(mc_sorted), 2)
        mc_med = round(mc_sorted[len(mc_sorted)//2], 2)
        mc_max = round(max(mc_sorted), 2)
        mc_note = f"基于历史数据统计（最低{mc_min}亿，中位{mc_med}亿，最高{mc_max}亿）。"
        if mc_pct >= 90:
            mc_note += "当前市值处于历史极高位。"
        elif mc_pct >= 70:
            mc_note += "当前市值处于历史偏高位。"
        else:
            mc_note += "当前市值处于历史中低位。"
        L(f"| **总市值** | {cur_mc} 亿元 | **{mc_pct}%** | {mc_note} |")
    else:
        L(f"| **总市值** | {cur_mc} 亿元 | N/A | 数据不足 |")
    L(f"| **ROE** | {cur_roe}% | {roe_quality} | {qa.get('近3年均ROE%', '?')}% |")
    dy_level = qa.get("股息率水平", "?")
    L(f"| **股息率** | {cur_dy}% | {dy_level} | 近3年均ROE {qa.get('近3年均ROE%', '?')}% |")
    L()
    L("> **说明：** 估值分位基于全量历史数据计算，百分位越低表示估值越便宜。")
    L()
    L("---")
    L()

    # ═══════ 二、价格与波段涨跌幅历史分位 ═══════
    L("### **二、 价格与波段涨跌幅历史分位**")
    L()
    L("本部分评估当前价格在不同周期内的位置，判断是处于高位还是低位。")
    L()

    period_labels = {'5': 5, '10': 10, '20': 20, '30': 30, '90': 90}

    # 计算各周期最新涨跌幅
    def _latest_swing_ret(period_days):
        if len(closes) <= period_days:
            return None
        prev = closes[period_days]
        if prev == 0:
            return None
        return round((closes[0] - prev) / prev * 100, 2)

    L("| 周期 | 最新波段涨跌幅 | 所处历史分位 | 历史均值 | 历史中位数 | 解读 |")
    L("| --- | --- | --- | --- | --- | --- |")

    for p_str, p_days in sorted(period_labels.items()):
        latest_ret = _latest_swing_ret(p_days)
        if latest_ret is None:
            continue

        # 从 distribution_result 获取分布数据
        dist_pd = (distribution_result or {}).get(p_str, {})
        if dist_pd.get("数据不足", True):
            direction = "上涨" if latest_ret > 0 else "下跌"
            L(f"| **{p_str}日** | {latest_ret:+.2f}% | 数据不足 | - | - | 数据不足以计算历史分位 |")
            continue

        up_s = dist_pd.get("上涨幅度", {})
        down_s = dist_pd.get("下跌幅度", {})
        up_dist = up_s.get("分布", {})
        down_dist = down_s.get("分布", {})
        up_total = up_s.get("样本数", 0) or 1
        down_total = down_s.get("样本数", 0) or 1

        def _calc_exact_percentile_up(ret, dist, total):
            """计算上涨百分位（按区间上限升序累加，与Excel一致）"""
            labels = sorted(dist.keys(), key=lambda x: float(x.split("~")[0]))
            cumul = 0
            for lab in labels:
                lo = float(lab.split("~")[0])
                hi = float(lab.split("~")[1])
                cumul += dist[lab]['次数']
                if lo <= ret < hi or (ret == 0 and lo == 0):
                    return round(cumul / total * 100, 1), lab
            # 如果大于所有区间上限
            return round(cumul / total * 100, 1), labels[-1] if labels else ""

        def _calc_exact_percentile_down(ret_abs, dist, total):
            """计算下跌百分位（按绝对值升序累加，与Excel一致）
               dist的key格式为 "-高~-低" 如 "-10~-5" 表示 -10%~-5%
            """
            # 按绝对值升序排序：-5~0 (abs 0-5), -10~-5 (abs 5-10), ...
            labels = sorted(dist.keys(), key=lambda x: -float(x.split("~")[1]))
            cumul = 0
            for lab in labels:
                lo = float(lab.split("~")[0])  # 更负的值
                hi = float(lab.split("~")[1])  # 更接近0的值
                abs_lo = abs(lo)  # 绝对值下限
                abs_hi = abs(hi)  # 绝对值上限
                cumul += dist[lab]['次数']
                # 检查ret_abs是否落在当前区间（绝对值角度）
                # 区间范围从 abs_hi~abs_lo（因为hi更接近0）
                # 例如 "-10~-5": hi=-5, lo=-10, abs范围 5~10
                if (abs_hi <= ret_abs < abs_lo) or (ret_abs == 0 and abs_hi == 0):
                    return round(cumul / total * 100, 1), lab
                # 特殊情况：ret_abs正好等于区间的边界
                if abs(ret_abs - abs_lo) < 0.001:
                    return round(cumul / total * 100, 1), lab
            # 大于所有区间
            return round(cumul / total * 100, 1), labels[-1] if labels else ""

        up_mean = up_s.get("平均值", 0)
        up_median = up_s.get("中位数", 0)
        down_mean = down_s.get("平均值", 0)
        down_median = down_s.get("中位数", 0)

        if latest_ret >= 0:
            mean_val = up_mean
            median_val = up_median
            ex_pct, found_lab = _calc_exact_percentile_up(latest_ret, up_dist, up_total)
            pct_desc = f"**{ex_pct}%**"
            # 解读
            if ex_pct <= 20:
                note = f"近{p_str}日涨幅{latest_ret:+.2f}%，仅{ex_pct}%的历史上涨幅度小于此值，处于历史极低分位，表明近{p_str}日涨幅远低于历史常态。"
            elif ex_pct <= 40:
                note = f"近{p_str}日涨幅{latest_ret:+.2f}%，{ex_pct}%的历史上涨幅度小于此值，低于历史均值{mean_val:.2f}%，处于历史较低分位。"
            elif ex_pct <= 60:
                note = f"近{p_str}日涨幅{latest_ret:+.2f}%，{ex_pct}%的历史上涨幅度小于此值，接近历史中位数{median_val:.2f}%，处于历史中位区间。"
            elif ex_pct <= 80:
                note = f"近{p_str}日涨幅{latest_ret:+.2f}%，{ex_pct}%的历史上涨幅度小于此值，高于历史均值{mean_val:.2f}%，处于历史较高分位。"
            else:
                note = f"近{p_str}日涨幅{latest_ret:+.2f}%，{ex_pct}%的历史上涨幅度小于此值，远高于历史均值{mean_val:.2f}%，处于历史极高位置，仅{100-ex_pct:.1f}%的时间涨得更多。"
            L(f"| **{p_str}日** | {latest_ret:+.2f}% | {pct_desc} | {mean_val:.2f}% (涨幅) | {median_val:.2f}% (涨幅) | {note} |")
        else:
            ret_abs = abs(latest_ret)
            mean_val = down_mean
            median_val = down_median
            ex_pct, found_lab = _calc_exact_percentile_down(ret_abs, down_dist, down_total)
            pct_desc = f"**{ex_pct}%**"
            # 解读：百分位 = 跌幅至少达到当前值的样本占比
            if ex_pct <= 10:
                note = f"近{p_str}日下跌{ret_abs:.2f}%，仅{ex_pct}%的历史下跌幅度超过此值，属于极端超跌，回调极深，反弹概率大。"
            elif ex_pct <= 30:
                note = f"近{p_str}日下跌{ret_abs:.2f}%，{ex_pct}%的历史下跌幅度超过此值，超过历史均值{mean_val:.2f}%，回调较深。"
            elif ex_pct <= 60:
                note = f"近{p_str}日下跌{ret_abs:.2f}%，{ex_pct}%的历史下跌幅度超过此值，接近历史中位数{median_val:.2f}%，处于中等回调水平。"
            else:
                note = f"近{p_str}日下跌{ret_abs:.2f}%，{ex_pct}%的历史下跌幅度超过此值，小于历史中位数{median_val:.2f}%，回调幅度较小。"
            L(f"| **{p_str}日** | {latest_ret:+.2f}% | {pct_desc} | {mean_val:.2f}% (跌幅) | {median_val:.2f}% (跌幅) | {note} |")

    L()
    L("---")
    L()

    # ═══════ 三、当日综合策略信号 ═══════
    L("### **三、 当日综合策略信号（核心结论）**")
    L()

    # 使用 swing_signals (来自 analyze_current_swing_signal 的返回)
    signal_row = None
    if swing_signals and len(swing_signals) > 0:
        signal_row = swing_signals[-1]  # 取最新的

    if signal_row and signal_row.get("信号"):
        sig_list = signal_row["信号"]
        tech = signal_row.get("技术指标", {})
        overall = signal_row.get("综合建议", "")

        L("| 项目 | 信号及数值 | 解读 |")
        L("| --- | --- | --- |")

        # 各周期信号
        for sig in sig_list:
            period = sig["周期"]
            ret = sig.get("区间涨跌幅%", 0)
            action = sig.get("建议操作", "观望")
            action_display = {
                "买入(正T)": "**买入(正T)**",
                "卖出(反T)": "**卖出(反T)**",
                "观望": "**观望**",
            }.get(action, f"**{action}**")
            threshold = sig.get("触发阈值%", "-")
            win_rate = sig.get("历史胜率%", "-")

            if action == "买入(正T)":
                note = f"近{period}下跌{abs(ret):.2f}%超过阈值{threshold}%，触发波段正T买入信号。"
            elif action == "卖出(反T)":
                note = f"近{period}上涨{ret:.2f}%达到阈值{threshold}%，触发波段反T卖出信号。"
            else:
                note = f"近{period}涨跌幅{ret:+.2f}%，未达阈值，无强烈交易信号。"
            L(f"| **{period}周期建议** | {action_display} | {note} |")

        # 计算综合胜率（取所有周期信号胜率的均值，与Excel一致）
        win_rates = []
        for s in sig_list:
            wr = s.get("历史胜率%", "")
            if isinstance(wr, (int, float)):
                win_rates.append(wr)
        combined_win_rate = round(sum(win_rates) / len(win_rates), 1) if win_rates else "-"
        L(f"| **综合胜率** | **{combined_win_rate}%** | 基于历史统计，所有触发信号的周期（正T/反T）的平均胜率。 |")
        L(f"| **MACD方向** | **{tech.get('MACD方向', 'N/A')}** | 中期趋势指标，对交易信号方向形成支撑或警示。 |")
        rsi_val = tech.get("RSI", "N/A")
        rsi_note = "处于超买区域(>70)，注意回调风险。" if isinstance(rsi_val, (int, float)) and rsi_val > 70 else \
                   "处于超卖区域(<30)，关注反弹机会。" if isinstance(rsi_val, (int, float)) and rsi_val < 30 else \
                   "处于30-70中性区间。"
        L(f"| **RSI值** | **{rsi_val}** | {rsi_note} |")
        L(f"| **量能状态** | **{tech.get('成交量', 'N/A')}** | 成交量放量/缩量/正常，影响信号强度。 |")
        L(f"| **背离类型** | **{tech.get('背离', '无')}** | 顶背离或底背离增强/削弱信号可靠性。 |")
        L(f"| **综合建议** | **{overall}** | **这是本报告的最终交易指向。** |")
    else:
        # 无信号数据，使用趋势分析结论
        L("| 项目 | 结论 | 说明 |")
        L("| --- | --- | --- |")
        L(f"| **趋势评分** | **{trend['score']}/100** | {trend['verdict']} - {trend['reason']} |")
        L(f"| **估值评分** | **{valuation.get('score', '?')}/100** | {valuation['verdict']} |")
        tech = td.get("技术指标", {})
        macd = tech.get("MACD", {})
        L(f"| **MACD方向** | **{macd.get('方向', 'N/A')}** | 中期趋势方向 |")
        rsi_val = tech.get("RSI(14)", "N/A")
        L(f"| **RSI值** | **{rsi_val}** | {tech.get('RSI判断', '')} |")
        L(f"| **综合建议** | **{trend['verdict']} + {valuation['verdict']}** | 结合趋势与估值的综合判断 |")

    L()
    L("---")
    L()

    # ═══════ 四、辅助决策信息 ═══════
    L("### **四、 辅助决策信息**")
    L()

    # 4.1 日内做T策略参考
    L("#### **4.1 日内做T策略参考**")
    L()
    # 提前初始化日内T变量，防止后面引用时报错
    best_zt_idx = -1
    best_ft_idx = -1
    zt_th = zt_win = zt_day = zt_ret = 0
    ft_th = ft_win = ft_day = ft_ret = 0

    if intraday_result and not intraday_result.get("数据不足", True):
        bs = intraday_result.get("基础统计", {})
        total_td = bs.get("总交易日", 0)
        L(f"如果您计划进行日内交易，可以参考以下基于{total_td}个交易日统计的最佳阈值：")
        L()

        # 找最佳正T梯度
        zt = intraday_result.get("正T", {})
        zt_grads = zt.get("梯度", [])
        zt_wins = zt.get("胜率%", [])
        zt_days = zt.get("触发天数", [])
        zt_returns = zt.get("平均收益%", [])
        best_zt_idx = -1
        for i in range(len(zt_grads)):
            if zt_days[i] >= 5:
                if best_zt_idx == -1 or zt_wins[i] > zt_wins[best_zt_idx]:
                    best_zt_idx = i

        # 找最佳反T梯度
        ft = intraday_result.get("反T", {})
        ft_grads = ft.get("梯度", [])
        ft_wins = ft.get("胜率%", [])
        ft_days = ft.get("触发天数", [])
        ft_returns = ft.get("平均收益%", [])
        best_ft_idx = -1
        for i in range(len(ft_grads)):
            if ft_days[i] >= 5:
                if best_ft_idx == -1 or ft_wins[i] > ft_wins[best_ft_idx]:
                    best_ft_idx = i

        # 也找次优梯度（用于显示范围）


        L("| 策略类型 | 最佳阈值 | 胜率 | 触发次数 | 平均收益 | 执行方式 |")
        L("| --- | --- | --- | --- | --- | --- |")

        if best_zt_idx >= 0:
            zt_th = zt_grads[best_zt_idx]
            zt_win = zt_wins[best_zt_idx]
            zt_day = zt_days[best_zt_idx]
            zt_ret = zt_returns[best_zt_idx]
            fut_price_low = round(price * (1 - zt_th / 100), 2) if isinstance(price, (int, float)) else "?"
            L(f"| **日内正T (先买后卖)** | **{zt_th:.1f}%** | {zt_win:.1f}% | {zt_day}次 | {zt_ret:.2f}% | 开盘下跌超过**{zt_th:.1f}%** 时买入，收盘卖出。 |")

        if best_ft_idx >= 0:
            ft_th = ft_grads[best_ft_idx]
            ft_win = ft_wins[best_ft_idx]
            ft_day = ft_days[best_ft_idx]
            ft_ret = ft_returns[best_ft_idx]
            fut_price_high = round(price * (1 + ft_th / 100), 2) if isinstance(price, (int, float)) else "?"
            L(f"| **日内反T (先卖后买)** | **{ft_th:.1f}%** | {ft_win:.1f}% | {ft_day}次 | {ft_ret:.2f}% | 开盘上涨超过**{ft_th:.1f}%** 时卖出，收盘买回。 |")

        L()
        L("**执行建议：**")
        L()
        if best_zt_idx >= 0:
            fut_price_low = round(price * (1 - zt_th / 100), 2) if isinstance(price, (int, float)) else "?"
            L(f"* 若次日开盘价较今日收盘价 **下跌超过{zt_th:.1f}%（即低于约{fut_price_low}元）** ，可执行日内正T，历史胜率超{zt_win:.0f}%。")
        if best_ft_idx >= 0:
            fut_price_high = round(price * (1 + ft_th / 100), 2) if isinstance(price, (int, float)) else "?"
            L(f"* 若次日开盘价 **上涨超过{ft_th:.1f}%（即高于约{fut_price_high}元）** ，可执行日内反T，历史胜率{ft_win:.1f}%，需注意上涨趋势中可能卖飞。")
    else:
        L("日内做T数据不足，无法提供策略参考。")
    L()

    # 4.2 技术指标共振检验
    L("#### **4.2 技术指标共振检验**")
    L()
    tech = td.get("技术指标", {})
    macd_dir = tech.get("MACD", {}).get("方向", "N/A")
    rsi_val = tech.get("RSI(14)", "N/A")
    rsi_judge = tech.get("RSI判断", "")

    # 计算多少周期发出正T/反T信号
    zt_count = len([s for s in (signal_row.get("信号", []) if signal_row else []) if s.get("信号类型") == "正T"])
    ft_count = len([s for s in (signal_row.get("信号", []) if signal_row else []) if s.get("信号类型") == "反T"])

    has_zts = zt_count >= 2
    has_fts = ft_count >= 2
    macd_bull = "金叉" in str(macd_dir) or "多头" in str(macd_dir) or "上方" in str(macd_dir)
    macd_bear = "死叉" in str(macd_dir) or "空头" in str(macd_dir) or "下方" in str(macd_dir)

    # 共振判断
    if has_zts and macd_bull:
        resonance = "**强共振**"
        resonance_note = "中期趋势（MACD金叉）与多个波段周期超卖信号（正T买入）形成方向一致，增加了买入策略的可靠性。"
    elif has_fts and macd_bear:
        resonance = "**强共振**"
        resonance_note = "中期趋势（MACD死叉）与多个波段周期超买信号（反T卖出）形成方向一致，增加了卖出策略的可靠性。"
    elif has_zts:
        resonance = "**中等共振**"
        resonance_note = "多个波段周期发出正T买入信号，但MACD尚未形成金叉共振，需谨慎。"
    elif has_fts:
        resonance = "**中等共振**"
        resonance_note = "多个波段周期发出反T卖出信号，但MACD尚未形成死叉共振，需谨慎。"
    else:
        resonance = "**弱共振/无共振**"
        resonance_note = "波段信号与趋势指标未形成明显共振，建议观望。"

    L(f"* **趋势共振：** `MACD:{macd_dir}` + `波段信号:正T{zt_count}个/反T{ft_count}个` = {resonance}。{resonance_note}")

    # 风险因素
    risk_parts = []
    if isinstance(rsi_val, (int, float)):
        if rsi_val > 70:
            risk_parts.append(f"RSI={rsi_val}处于超买区域，注意回调风险。")
        elif rsi_val < 30:
            risk_parts.append(f"RSI={rsi_val}处于超卖区域，下跌空间可能有限，但尚未出现明确底部反转信号。")
        else:
            risk_parts.append(f"RSI={rsi_val}并非极端值，市场处于正常波动区间。")

    if risk_parts:
        L(f"* **风险因素：** {' '.join(risk_parts)}")
    L()
    L("---")
    L()

    # ═══════ 五、最终交易建议总结 ═══════
    L("### **五、 最终交易建议总结**")
    L()

    # ════════════════════════════════════════════════════════
    #  新逻辑：以波段T信号为核心，MACD/RSI/量能/背离为辅助确认
    #  结合波段涨跌幅分布判断极端程度
    # ════════════════════════════════════════════════════════

    # ── 1. 波段T信号加权评分 ──
    # 周期权重：越长周期权重越大（5日=1, 10日=2, 20日=3, 30日=4, 90日=5）
    period_weights = {"5日": 1, "10日": 2, "20日": 3, "30日": 4, "90日": 5}
    zt_weighted = 0   # 正T加权得分
    ft_weighted = 0   # 反T加权得分
    total_weight = 0
    period_details = []  # 各周期详细描述

    if signal_row and signal_row.get("信号"):
        sig_list = signal_row["信号"]
        for s in sig_list:
            period = s["周期"]
            sig_type = s.get("信号类型", "无信号")
            ret = s.get("区间涨跌幅%", 0)
            win_rate = s.get("历史胜率%", "-")
            w = period_weights.get(period, 1)
            total_weight += w
            if sig_type == "正T":
                zt_weighted += w
                period_details.append(f"{period}跌{s.get('触发阈值%','?')}%触发正T(胜率{win_rate}%)")
            elif sig_type == "反T":
                ft_weighted += w
                period_details.append(f"{period}涨{ret:.1f}%触发反T(胜率{win_rate}%)")
            else:
                period_details.append(f"{period}涨跌{ret:+.1f}%无信号")

    # 波段倾向净得分：-100~+100，正=偏多，负=偏空
    if total_weight > 0:
        net_score = round((zt_weighted - ft_weighted) / total_weight * 100, 0)
    else:
        net_score = 0

    # ── 2. MACD方向确认 ──
    macd_bull = ("金叉" in str(macd_dir) or "多头" in str(macd_dir) or "上方" in str(macd_dir)) and "下方" not in str(macd_dir) and "空头" not in str(macd_dir)
    macd_bear = "死叉" in str(macd_dir) or "空头" in str(macd_dir) or "下方" in str(macd_dir)

    # ── 3. RSI状态 ──
    rsi_oversold = isinstance(rsi_val, (int, float)) and rsi_val < 30
    rsi_overbought = isinstance(rsi_val, (int, float)) and rsi_val > 70
    rsi_neutral = isinstance(rsi_val, (int, float)) and 30 <= rsi_val <= 70

    # ── 4. 背离检测（取信号中各周期背离的多数情况）──
    has_bottom_div = False  # 底背离
    has_top_div = False     # 顶背离
    if signal_row and signal_row.get("信号"):
        for s in signal_row["信号"]:
            div = s.get("背离", "无")
            if "底背离" in str(div):
                has_bottom_div = True
            if "顶背离" in str(div):
                has_top_div = True

    # ── 5. 判断当前涨跌幅在历史分布中的极端程度 ──
    # 检查各周期当前涨跌幅是否处于历史极端位置（<20%分位或>80%分位）
    extreme_bullish = 0  # 极端看多信号数（大跌到历史低位）
    extreme_bearish = 0  # 极端看空信号数（大涨到历史高位）
    if distribution_result and signal_row and signal_row.get("信号"):
        for s in signal_row["信号"]:
            period = s["周期"]
            ret = s.get("区间涨跌幅%", 0)
            p_str = period.replace("日", "")
            dist_pd = distribution_result.get(p_str, {})
            if dist_pd.get("数据不足", True):
                continue
            if ret < 0:
                # 下跌：对比下跌幅度分布
                down_s = dist_pd.get("下跌幅度", {})
                down_mean = down_s.get("平均值", 0)
                ret_abs = abs(ret)
                if ret_abs > down_mean * 1.5:  # 跌幅超过均值1.5倍 = 极端
                    extreme_bullish += 1  # 超跌 = 潜在看多机会
            elif ret > 0:
                # 上涨：对比上涨幅度分布
                up_s = dist_pd.get("上涨幅度", {})
                up_mean = up_s.get("平均值", 0)
                if ret > up_mean * 1.5:  # 涨幅超过均值1.5倍 = 极端
                    extreme_bearish += 1  # 超涨 = 潜在看空风险

    # ── 6. 综合胜率 ──
    if signal_row and signal_row.get("信号"):
        win_rates = []
        for s in signal_row["信号"]:
            wr = s.get("历史胜率%", "")
            if isinstance(wr, (int, float)):
                win_rates.append(wr)
        combined_win_rate = round(sum(win_rates) / len(win_rates), 1) if win_rates else "-"
    else:
        combined_win_rate = '-'

    # ── 7. 最终决策 ──
    # 构建评分理由
    score_parts = []
    score_parts.append(f"波段信号净得分{net_score:+.0f}")
    if macd_bull:
        score_parts.append("MACD偏多")
    elif macd_bear:
        score_parts.append("MACD偏空")
    if rsi_oversold:
        score_parts.append("RSI超卖")
    elif rsi_overbought:
        score_parts.append("RSI超买")
    if has_bottom_div:
        score_parts.append("底背离")
    if has_top_div:
        score_parts.append("顶背离")
    if extreme_bullish >= 2:
        score_parts.append(f"{extreme_bullish}个周期超跌")
    if extreme_bearish >= 2:
        score_parts.append(f"{extreme_bearish}个周期超涨")

    # 强多条件：净得分>30 + MACD金叉确认 + (RSI不超买或超卖)
    strong_bull = net_score >= 30 and macd_bull and not rsi_overbought
    # 中多条件：净得分>0 + (MACD偏多或超卖或底背离或超跌)
    mild_bull = net_score >= 10 and (macd_bull or rsi_oversold or has_bottom_div or extreme_bullish >= 2)
    # 弱多条件：净得分>0
    weak_bull = net_score > 0

    # 强空条件
    strong_bear = net_score <= -30 and macd_bear and not rsi_oversold
    # 中空条件
    mild_bear = net_score <= -10 and (macd_bear or rsi_overbought or has_top_div or extreme_bearish >= 2)
    # 弱空条件
    weak_bear = net_score < 0

    # 震荡条件
    is_neutral = not weak_bull and not weak_bear

    logic = []

    if strong_bull:
        main_strategy = "**买入/加仓**"
        confidence = "**高**"
        logic.append(f"1. 波段信号强烈偏多（净得分{net_score:+.0f}），{zt_count}个周期触发正T，{ft_count}个周期触发反T。")
        logic.append(f"2. MACD{macd_dir}确认多头趋势。")
        if rsi_oversold:
            logic.append("3. RSI处于超卖区域，短期超跌反弹概率大。")
        if has_bottom_div:
            logic.append("4. 出现底背离信号，反转可靠性增强。")
        if extreme_bullish >= 2:
            logic.append(f"5. {extreme_bullish}个周期跌幅超过历史均值1.5倍，处于极端超跌区域。")
    elif mild_bull:
        main_strategy = "**逢低买入/做多T**"
        confidence = "**中高**"
        logic.append(f"1. 波段信号偏多（净得分{net_score:+.0f}），{zt_count}个周期触发正T。")
        if macd_bull:
            logic.append(f"2. MACD{macd_dir}，中期趋势支持做多。")
        if rsi_oversold:
            logic.append("3. RSI超卖，短期有反弹需求。")
        if has_bottom_div:
            logic.append("4. 出现底背离，增强买入信号可靠性。")
        if extreme_bullish >= 2:
            logic.append(f"5. {extreme_bullish}个周期超跌，历史统计显示此类极端下跌后反弹概率较高。")
        if not macd_bull and not rsi_oversold:
            logic.append("3. MACD尚未形成明确金叉/多头，建议分批低吸而非一次性重仓。")
    elif weak_bull:
        main_strategy = "**偏多持仓+做T**"
        confidence = "**中**"
        logic.append(f"1. 波段信号略偏多（净得分{net_score:+.0f}），正T{zt_count}个/反T{ft_count}个。")
        if macd_bull:
            logic.append(f"2. MACD{macd_dir}，中期偏多，持股为主。")
        else:
            logic.append("2. MACD方向不明确，不宜重仓。")
        logic.append("3. 适宜持股同时以做T降低持仓成本。")
    elif strong_bear:
        main_strategy = "**卖出/减仓**"
        confidence = "**高**"
        logic.append(f"1. 波段信号强烈偏空（净得分{net_score:+.0f}），{ft_count}个周期触发反T。")
        logic.append(f"2. MACD{macd_dir}确认空头趋势。")
        if rsi_overbought:
            logic.append("3. RSI处于超买区域，回调风险较大。")
        if has_top_div:
            logic.append("4. 出现顶背离信号，下跌概率增加。")
        if extreme_bearish >= 2:
            logic.append(f"5. {extreme_bearish}个周期涨幅超过历史均值1.5倍，处于极端超买区域。")
    elif mild_bear:
        main_strategy = "**逢高减仓/做空T**"
        confidence = "**中高**"
        logic.append(f"1. 波段信号偏空（净得分{net_score:+.0f}），{ft_count}个周期触发反T。")
        if macd_bear:
            logic.append(f"2. MACD{macd_dir}，中期趋势偏空。")
        if rsi_overbought:
            logic.append("3. RSI超买，短期回调概率大。")
        if has_top_div:
            logic.append("4. 出现顶背离，增强卖出信号可靠性。")
        if extreme_bearish >= 2:
            logic.append(f"5. {extreme_bearish}个周期超涨，历史统计显示此类极端上涨后回调概率较高。")
        if not macd_bear and not rsi_overbought:
            logic.append("3. MACD尚未形成明确死叉/空头，建议分批减仓而非清仓。")
    elif weak_bear:
        main_strategy = "**偏空持仓+做T**"
        confidence = "**中**"
        logic.append(f"1. 波段信号略偏空（净得分{net_score:+.0f}），反T{ft_count}个/正T{zt_count}个。")
        if macd_bear:
            logic.append(f"2. MACD{macd_dir}，中期偏空，控制仓位。")
        else:
            logic.append("2. MACD方向不明确，以做T降本为主。")
        logic.append("3. 控制仓位，以反T（逢高卖出）操作为主，降低持仓成本。")
    else:
        main_strategy = "**持仓观望**"
        confidence = "**中**"
        logic.append(f"1. 波段信号无明显方向（净得分{net_score:+.0f}），正T{zt_count}个/反T{ft_count}个。")
        logic.append("2. 各周期信号缺乏一致性，多空分歧较大，等待方向明确。")
        if macd_bull:
            logic.append(f"3. MACD{macd_dir}，中期略偏多，可保留部分仓位。")
        elif macd_bear:
            logic.append(f"3. MACD{macd_dir}，中期略偏空，注意控制风险。")
        else:
            logic.append("3. 以观望为主，等待信号明确后再操作。")

    # 追加综合胜率
    if combined_win_rate != '-':
        logic.append(f"{len(logic)+1}. 历史统计显示，触发信号周期的综合平均胜率约为 **{combined_win_rate}%** 。")

    L("| 决策维度 | 结论 | 置信度 |")
    L("| --- | --- | --- |")
    L(f"| **主策略** | {main_strategy} | {confidence} |")
    L(f"| **逻辑依据** | {' '.join(logic)} | |")
    L(f"| **波段评分** | 净得分{net_score:+.0f}（{'偏多' if net_score > 0 else '偏空' if net_score < 0 else '中性'}） | MACD:{'偏多' if macd_bull else '偏空' if macd_bear else '中性'} / RSI:{'超卖' if rsi_oversold else '超买' if rsi_overbought else '中性'} / 背离:{'底背离' if has_bottom_div else '顶背离' if has_top_div else '无'} |")
    L(f"| **周期详情** | {' ｜ '.join(period_details)} | |")

    # 买入区域
    buy_zone = []
    if isinstance(rsi_val, (int, float)) and rsi_val < 30:
        buy_zone.append("RSI已进入超卖区域，可在**集合竞价**或**开盘价附近**直接建仓。")
    elif isinstance(rsi_val, (int, float)) and rsi_val < 40:
        buy_zone.append("RSI接近超卖但未确认反转，建议在**集合竞价**或**开盘后下跌1%-2%** 的区间内分批建仓，避免追高。")
    else:
        buy_zone.append("RSI处于中性区域，建议等待回调至支撑位附近建仓。")
    L(f"| **买入区域** | {' '.join(buy_zone)} | |")

    # 止损设定
    vod_kr = vod.get("关键价位", {})
    stop_loss = []
    recent_low = vod_kr.get("60日最低", "?")
    if recent_low != "?" and isinstance(price, (int, float)):
        stop_pct = round((recent_low - price) / price * 100, 1) if price > 0 else 0
        stop_loss.append(f"建议以**最近60日低点（约{recent_low}元，{stop_pct:+.1f}%）** ")
    else:
        stop_loss.append("建议以**最近10日低点** ")
    stop_loss.append("或 **-5%** 作为硬止损。")
    L(f"| **止损设定** | {''.join(stop_loss)} | |")

    # 仓位建议
    position = valuation.get("verdict", "?")
    L(f"| **仓位建议** | 作为波段交易，建议仓位**不超过总仓位的15%-20%**。估值建议：{position} | |")

    # 日内T机会
    intraday_tt = []
    if best_zt_idx >= 0:
        zt_price = round(price * (1 - zt_th / 100), 2) if isinstance(price, (int, float)) else "?"
        intraday_tt.append(f"若大跌（>-{zt_th:.1f}%即低于{zt_price}元），可额外分配资金做日内正T。")
    if best_ft_idx >= 0:
        ft_price = round(price * (1 + ft_th / 100), 2) if isinstance(price, (int, float)) else "?"
        intraday_tt.append(f"若大涨（>+{ft_th:.1f}%即高于{ft_price}元），可做日内反T。")
    if not intraday_tt:
        intraday_tt.append("关注开盘价，根据实时波动决定日内T操作。")
    L(f"| **日内T机会** | {' '.join(intraday_tt)} | |")

    return "\n".join(lines)


def md_to_image(md_content: str, output_path: str, width: int = 900) -> bool:
    """
    Markdown 内容转图片
    流程：markdown→HTML + CSS → playwright(system chrome) 截图

    Args:
        md_content: Markdown 文本内容
        output_path: 输出图片路径
        width: 图片宽度（像素）

    Returns:
        是否成功
    """
    import markdown as md_lib
    import os

    try:
        # 1. Markdown → HTML（使用标准 markdown 库 + extra 扩展）
        body_html = md_lib.markdown(md_content, extensions=['extra', 'codehilite'])

        # 2. 包装 HTML 页面（漂亮样式）
        html_template = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 14px;
    line-height: 1.7;
    color: #1a1a2e;
    background: #ffffff;
    padding: 30px 40px;
    width: {width}px;
  }}
  h1, h2, h3, h4 {{ color: #1a1a2e; margin-top: 1.2em; margin-bottom: 0.6em; }}
  h3 {{ font-size: 18px; border-left: 4px solid #e94560; padding-left: 12px; }}
  h4 {{ font-size: 15px; color: #16213e; }}
  p {{ margin: 0.5em 0; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 0.8em 0;
    font-size: 13px;
  }}
  th {{
    background: #1a1a2e;
    color: #ffffff;
    padding: 8px 10px;
    text-align: left;
    font-weight: 600;
  }}
  td {{
    padding: 7px 10px;
    border-bottom: 1px solid #e8e8e8;
  }}
  tr:nth-child(even) td {{ background: #f8f9fa; }}
  tr:hover td {{ background: #eef2ff; }}
  strong {{ color: #e94560; }}
  hr {{ border: none; border-top: 2px solid #1a1a2e; margin: 1.5em 0; }}
  blockquote {{
    border-left: 4px solid #e94560;
    background: #fff5f5;
    padding: 10px 15px;
    margin: 0.8em 0;
    color: #555;
    font-size: 13px;
  }}
  code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
  ul, ol {{ padding-left: 20px; margin: 0.4em 0; }}
  li {{ margin: 0.3em 0; }}
  .header {{ text-align: center; margin-bottom: 20px; }}
  .footer {{ text-align: center; margin-top: 20px; font-size: 12px; color: #999; }}
</style>
</head>
<body>
<div class="header">
  <h2>股票交易决策支持报告</h2>
  <hr>
</div>
{body_html}
<div class="footer">
  <hr>
  <p>本报告由 stock_crawler 自动生成 | 仅供参考，不构成投资建议</p>
</div>
</body>
</html>'''

        # 3. playwright 使用系统 Chrome 渲染截图
        from playwright.sync_api import sync_playwright

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": 1})
            page.set_content(html_template, wait_until="networkidle")
            page.wait_for_timeout(500)
            page_height = page.evaluate("document.body.scrollHeight")
            page.set_viewport_size({"width": width, "height": page_height})
            page.wait_for_timeout(200)
            page.screenshot(path=output_path, full_page=True)
            browser.close()

        return True
    except Exception as e:
        print(f"  [警告] 图片生成失败: {e}")
        return False
