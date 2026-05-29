#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
数据获取器 —— 多源级联抓取 A 股日 K 线数据

数据源优先级（自动降级）：
  1. mootdx    (TCP)    — 主力，通达信服务器，多周期支持，不封IP
  2. 新浪财经  (HTTP)   — 替补，返回 ~1500 条，OHLC 不复权
  3. 腾讯财经  (HTTP)   — 保底，至少获取最新一日实时行情

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
import pandas as pd


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


def _fetch_kline_data_mootdx(stock_code: str, datalen: int) -> List[Dict]:
    """
    从 mootdx (通达信 TCP 协议) 获取日K线数据（支持分页，获取全量历史）

    mootdx 连接通达信行情服务器(7709)，不封 IP。
    vol 单位: 手 (1手=100股)，amount 单位: 元。

    通过 start/offset 分页参数获取全量历史，不受单次 800 条上限限制。
    直到获取条数达到 datalen 或已无更多数据为止。

    Returns:
        按日期降序排列的数据列表，字段同新浪格式 (含 成交量_股)
    """
    raw_code = stock_code.replace(".SZ", "").replace(".SH", "").strip()
    print(f"   替补: 尝试 mootdx (通达信, 分页获取)...")
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std')
    except Exception as e:
        raise DataFetchError(f"mootdx 连接失败: {e}")

    page_size = 800  # mootdx 单次最大返回数
    all_records = []
    seen_dates = set()
    start = 0

    while len(all_records) < datalen:
        try:
            klines = client.bars(symbol=raw_code, frequency=4, start=start, offset=page_size)
        except Exception as e:
            if all_records:
                break  # 已有数据，跳过剩余分页
            raise DataFetchError(f"mootdx K线获取失败: {e}")

        if klines is None or len(klines) == 0:
            break

        batch_size = len(klines)
        for _, row in klines.iterrows():
            dt = row.get('datetime')
            if isinstance(dt, pd.Timestamp):
                date_str = dt.strftime("%Y-%m-%d")
            else:
                date_str = str(dt)[:10]

            if date_str in seen_dates:
                continue  # 去重（相邻批次边界可能有重复）
            seen_dates.add(date_str)

            all_records.append({
                "日期": date_str,
                "开盘价": float(row.get('open', 0)),
                "收盘价": float(row.get('close', 0)),
                "最高价": float(row.get('high', 0)),
                "最低价": float(row.get('low', 0)),
                # mootdx vol 单位是 手 (1手=100股)，转为 股 以统一格式
                "成交量_股": float(row.get('vol', 0)) * 100,
            })

        start += batch_size

        if batch_size < page_size:
            break  # 最后一批

        if len(all_records) % 2000 == 0:
            print(f"     [mootdx] 已获取 {len(all_records)} 条...")

    # 按日期降序（最新在前）
    all_records.sort(key=lambda x: x["日期"], reverse=True)

    # 截取需要的数量
    if len(all_records) > datalen:
        all_records = all_records[:datalen]

    print(f"   [mootdx] 返回 {len(all_records)} 条数据")
    return all_records


def _fetch_kline_data_tencent(stock_code: str) -> List[Dict]:
    """
    从腾讯财经 API 获取最新一日行情数据（二级替补）

    腾讯 qt.gtimg.cn 只提供实时行情，不提供历史K线。
    此函数作为最后保底，至少返回一条当日数据。

    Returns:
        含一条记录的列表（最新一日）, 字段同新浪格式
    """
    raw_code = stock_code.replace(".SZ", "").replace(".SH", "").strip()
    prefix = "sh" if raw_code.startswith(("6", "9")) else "sz"
    print(f"   替补: 尝试腾讯财经 (实时行情)...")

    try:
        import urllib.request
        url = f"https://qt.gtimg.cn/q={prefix}{raw_code}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ))
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
        vals = data.split('"')[1].split('~')
        if len(vals) < 50:
            raise DataFetchError(f"腾讯财经返回字段不足({len(vals)})")

        # 腾讯字段索引
        name = vals[1]
        price = float(vals[3]) if vals[3] else 0
        last_close = float(vals[4]) if vals[4] else 0
        open_price = float(vals[5]) if vals[5] else 0
        high = float(vals[33]) if vals[33] else 0
        low = float(vals[34]) if vals[34] else 0
        volume_wan = float(vals[36]) if vals[36] else 0  # 成交量(手)
        amount_wan = float(vals[37]) if vals[37] else 0  # 成交额(万)

        today_str = datetime.now().strftime("%Y-%m-%d")
        records = [{
            "日期": today_str,
            "开盘价": open_price,
            "收盘价": price,
            "最高价": high,
            "最低价": low,
            # 腾讯成交量单位是手，转为 股
            "成交量_股": volume_wan * 100,
        }]
        print(f"   [腾讯财经] 获取到 {name} 当日行情: 收{price} 开{open_price} 高{high} 低{low}")
        return records
    except Exception as e:
        raise DataFetchError(f"腾讯财经实时行情获取失败: {e}")


def _try_fetch_with_fallback(stock_code: str, sina_symbol: str, datalen: int) -> List[Dict]:
    """
    级联获取 K 线数据：mootdx(通达信) → 新浪财经 → 腾讯财经

    按优先级依次尝试，前一个失败则自动降级到下一个。
    """
    sources = [
        ("mootdx(通达信)", lambda: _fetch_kline_data_mootdx(stock_code, datalen)),
        ("新浪财经", lambda: _fetch_kline_data(sina_symbol, datalen)),
        ("腾讯财经(实时)", lambda: _fetch_kline_data_tencent(stock_code)),
    ]

    last_error = None
    for name, fetcher in sources:
        try:
            records = fetcher()
            if records:
                if last_error:
                    print(f"  [降级] {name} 替补成功，替代失败的数据源")
                return records
        except DataFetchError as e:
            last_error = e
            print(f"  [失败] {name}: {e}")
            continue

    raise DataFetchError(f"所有数据源均失败，最后错误: {last_error}")


def fetch_stock_data(config: Dict, max_records: Optional[int] = None) -> List[Dict]:
    """
    根据配置从多源级联抓取股票日 K 线数据，并计算衍生字段

    数据源优先级: mootdx(通达信) → 新浪财经 → 腾讯财经
    前一个失败自动降级到下一个。

    返回的每条记录包含：
      日期、开盘价、收盘价、最高价、最低价、
      成交量(万手)、成交额（亿）、涨跌幅%、振幅%、
      日内波动区间%、上涨幅度%、下跌幅度%

    Args:
        config: 配置字典，需包含 stock_code, start_date, end_date 等
        max_records: 限制只获取最近 N 条记录（用于缓存刷新模式）
                     设置后忽略 start_date/end_date 过滤，只取最新数据

    Returns:
        按日期降序排列的数据列表
    """
    stock_code = config["stock_code"]
    raw_code = stock_code.replace(".SZ", "").replace(".SH", "").strip()
    sina_symbol = _get_sina_symbol(raw_code)

    start_date = config.get("start_date", "")
    end_date = config.get("end_date", "")

    # 刷新模式：只获取最近 max_records 条，忽略日期范围
    if max_records is not None:
        print(f"[抓取] 正在刷新 {config.get('stock_name', stock_code)} ({raw_code}) 最近 {max_records} 条数据...")
        print(f"   数据源: mootdx(通达信) → 新浪财经 → 腾讯财经(保底)")
    else:
        print(f"[抓取] 正在抓取 {config.get('stock_name', stock_code)} ({raw_code}) 的数据...")
        print(f"   数据源: mootdx(通达信) → 新浪财经 → 腾讯财经(保底)")
        print(f"   时间范围: {start_date or '不限'} ~ {end_date or '不限'}")

    # ── 级联获取 K 线数据 ──
    # 根据起始日期估算所需数据量（约250个交易日/年），最大5000
    if max_records is not None:
        datalen = max_records
    elif start_date:
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d")
            ed = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.today()
            years = (ed - sd).days / 365.0
            datalen = min(5000, max(800, int(years * 250) + 200))  # 缓冲200条
        except Exception:
            datalen = 3000
    else:
        datalen = 3000
    records = _try_fetch_with_fallback(stock_code, sina_symbol, datalen)
    print(f"[返回] 共 {len(records)} 条原始数据")

    # ── 按时间范围过滤（刷新模式跳过日期过滤，保留最新 N 条）──
    if max_records is None:
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
