#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
数据获取器 —— 通过新浪财经 API 抓取 A 股日 K 线数据

关键说明：
  - 新浪 API 无需特殊请求头，网络连通性好
  - 返回的 OHLC 价格是**不复权**的原始数据
  - 成交额通过新浪实时行情接口补充（最新一日），历史日期按均价估算
"""

import json
import time
from datetime import datetime
from typing import Dict, List, Optional

import httpx


class DataFetchError(Exception):
    """数据抓取异常"""
    pass


# ── API 地址 ──
SINA_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
SINA_REALTIME_URL = "https://hq.sinajs.cn/list"


def _get_sina_symbol(stock_code: str) -> str:
    """
    将股票代码转为新浪格式

    规则：6/9 开头 → 沪市 sh，其余 → 深市 sz

    示例：
      "000933"      → "sz000933"
      "600519.SH"   → "sh600519"
    """
    code = stock_code.replace(".SZ", "").replace(".SH", "").strip()
    if code.startswith("6") or code.startswith("9"):
        return f"sh{code}"
    else:
        return f"sz{code}"


def _to_float(value) -> Optional[float]:
    """安全转换为浮点数，转换失败返回 None"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _fetch_kline_data(sina_symbol: str, datalen: int) -> List[Dict]:
    """
    从新浪获取日 K 线数据（不复权）

    Args:
        sina_symbol: 新浪格式代码，如 "sz000933"
        datalen: 请求的数据条数

    Returns:
        按日期降序排列的数据列表（最新在前）
    """
    params = {
        "symbol": sina_symbol,
        "scale": "240",     # 240分钟 = 日线
        "ma": "no",        # 不需要均线
        "datenum": str(datalen),
        "datalen": str(datalen),
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://finance.sina.com.cn",
    }

    # 失败自动重试 3 次
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=30.0, headers=headers) as client:
                resp = client.get(SINA_KLINE_URL, params=params)
                resp.raise_for_status()
                raw = resp.json()
            break
        except Exception as e:
            if attempt < max_retries:
                wait = attempt * 2
                print(f"[重试] 新浪K线第 {attempt} 次失败，{wait} 秒后重试... ({e})")
                time.sleep(wait)
            else:
                raise DataFetchError(f"新浪K线API请求失败（已重试{max_retries}次）: {e}")

    if not raw:
        raise DataFetchError("新浪K线API返回空数据")

    # 统一字段名
    records = []
    for item in raw:
        records.append({
            "日期": str(item.get("day", "")),
            "开盘价": _to_float(item.get("open")),
            "收盘价": _to_float(item.get("close")),
            "最高价": _to_float(item.get("high")),
            "最低价": _to_float(item.get("low")),
            "成交量_股": _to_float(item.get("volume")),  # 原始单位：股
        })

    # 按日期降序排序（最新在前）
    records.sort(key=lambda x: x["日期"], reverse=True)
    return records


def _fetch_latest_amount(sina_symbol: str) -> Optional[float]:
    """
    从新浪实时行情获取当日成交额（元）

    实时行情中第10个字段（索引9）为成交额，单位元
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://finance.sina.com.cn",
    }

    try:
        with httpx.Client(timeout=10.0, headers=headers) as client:
            resp = client.get(f"{SINA_REALTIME_URL}={sina_symbol}")
            resp.raise_for_status()
            text = resp.text
            # 解析格式: var hq_str_sz000933="...";
            if "=" in text:
                data_part = text.split("=", 1)[1].strip().strip('"').strip(";")
                fields = data_part.split(",")
                if len(fields) > 9:
                    return _to_float(fields[9])  # 第10个字段 = 成交额(元)
    except Exception:
        pass
    return None


def fetch_stock_data(config: Dict) -> List[Dict]:
    """
    根据配置从新浪 API 抓取股票日 K 线数据，并计算衍生字段

    返回的每条记录包含：
      日期、开盘价、收盘价、最高价、最低价、
      成交量(万手)、成交额（亿）、涨跌幅%、振幅%、
      日内波动区间%、上涨幅度%、下跌幅度%

    Args:
        config: 配置字典，需包含 stock_code, start_date, end_date 等

    Returns:
        按日期降序排列的数据列表
    """
    stock_code = config["stock_code"]
    raw_code = stock_code.replace(".SZ", "").replace(".SH", "").strip()
    sina_symbol = _get_sina_symbol(raw_code)

    start_date = config.get("start_date", "")
    end_date = config.get("end_date", "")

    print(f"[抓取] 正在抓取 {config.get('stock_name', stock_code)} ({raw_code}) 的数据...")
    print(f"   接口: 新浪财经")
    print(f"   时间范围: {start_date or '不限'} ~ {end_date or '不限'}")

    # ── 获取 K 线数据 ──
    datalen = 1500  # 多取一些以便后续按日期过滤
    records = _fetch_kline_data(sina_symbol, datalen)
    print(f"[返回] 新浪API返回 {len(records)} 条原始数据")

    # ── 按时间范围过滤 ──
    if start_date:
        records = [r for r in records if r["日期"] >= start_date]
    if end_date:
        records = [r for r in records if r["日期"] <= end_date]

    if not records:
        raise DataFetchError("过滤后无数据，请检查日期范围")

    # ── 获取当日成交额（实时行情补充最新一日） ──
    latest_amount = _fetch_latest_amount(sina_symbol)
    if latest_amount is not None:
        print(f"[补充] 获取到当日实时成交额: {latest_amount/100000000:.2f} 亿元")

    # ── 解析并计算衍生字段 ──
    parsed_data: List[Dict] = []
    for i, rec in enumerate(records):
        date_str = rec["日期"]
        open_price = rec["开盘价"] or 0.0
        close_price = rec["收盘价"] or 0.0
        high = rec["最高价"] or 0.0
        low = rec["最低价"] or 0.0
        volume_shares = rec["成交量_股"] or 0.0  # 原始单位：股

        record: Dict = {}

        # 基础 OHLC
        record["日期"] = date_str
        record["开盘价"] = round(open_price, 2)
        record["收盘价"] = round(close_price, 2)
        record["最高价"] = round(high, 2)
        record["最低价"] = round(low, 2)

        # 成交量：股 → 万手（1万手 = 1,000,000股）
        record["成交量(万手)"] = round(volume_shares / 1000000, 2)

        # 成交额：最新一天用实时行情，其余按均价估算
        if i == 0 and latest_amount is not None:
            amount_yuan = latest_amount
        else:
            avg_price = (open_price + close_price) / 2
            amount_yuan = volume_shares * avg_price

        record["成交额（亿）"] = round(amount_yuan / 100000000, 2)

        # ── 涨跌幅% = (今收 - 昨收) / 昨收 × 100 ──
        if i + 1 < len(records):
            prev_close = records[i + 1]["收盘价"] or 0.0
            if prev_close != 0:
                pct_change = round(((close_price - prev_close) / prev_close) * 100, 2)
            else:
                pct_change = 0.0
        else:
            pct_change = 0.0
        record["涨跌幅%"] = pct_change

        # ── 振幅% = (最高 - 最低) / 昨收 × 100 ──
        if i + 1 < len(records):
            prev_close = records[i + 1]["收盘价"] or 0.0
            if prev_close != 0:
                amplitude = round(((high - low) / prev_close) * 100, 2)
            else:
                amplitude = 0.0
        else:
            amplitude = 0.0
        record["振幅%"] = amplitude

        # ── 日内波动区间% = (最高 - 最低) / 开盘价 × 100 ──
        if open_price != 0:
            record["日内波动区间%"] = round(((high - low) / open_price) * 100, 2)
        else:
            record["日内波动区间%"] = 0.0

        # ── 上涨幅度% = (最高 - 开盘) / 开盘 × 100 ──
        # ── 下跌幅度% = (最低 - 开盘) / 开盘 × 100 ──
        if open_price != 0:
            record["上涨幅度%"] = round(((high - open_price) / open_price) * 100, 2)
            record["下跌幅度%"] = round(((low - open_price) / open_price) * 100, 2)
        else:
            record["上涨幅度%"] = 0.0
            record["下跌幅度%"] = 0.0

        parsed_data.append(record)

    print(f"[成功] 成功解析 {len(parsed_data)} 条数据")
    return parsed_data
