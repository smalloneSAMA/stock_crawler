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
