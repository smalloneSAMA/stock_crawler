"""
财务指标获取器 —— 从东方财富数据中心获取股票基本面数据

配合 Sina K-line 数据使用，提供：
  当前市值、市盈率TTM、股息率、市净率、市销率、净资产收益率、每股收益
"""

import json
import time
from typing import Dict, Optional

import httpx


class FinancialFetchError(Exception):
    """财务数据获取异常"""
    pass


# ── 东方财富数据中心API ──
FINANCE_API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# ── 腾讯行情API（用于补充股息率等实时指标）──
TENCENT_QT_URL = "https://qt.gtimg.cn/q"


def _get_eastmoney_secid(stock_code: str) -> str:
    """转为东方财富格式：000933.SZ"""
    code = stock_code.replace(".SZ", "").replace(".SH", "").strip()
    if code.startswith("6") or code.startswith("9"):
        return f"{code}.SH"
    else:
        return f"{code}.SZ"


def _get_tencent_symbol(stock_code: str) -> str:
    """转为腾讯行情格式：sz000933"""
    code = stock_code.replace(".SZ", "").replace(".SH", "").strip()
    if code.startswith("6") or code.startswith("9"):
        return f"sh{code}"
    else:
        return f"sz{code}"


def fetch_financial_indicators(
    stock_code: str,
    latest_close: float,
) -> Dict[str, Optional[float]]:
    """
    获取股票的各项财务指标

    Args:
        stock_code: 股票代码（如 000933.SZ）
        latest_close: 最新收盘价（用于计算PE/PB等）

    Returns:
        {
            "当前市值(亿)": float,
            "市盈率TTM": float,
            "股息率%": float,
            "市净率": float,
            "市销率": float,
            "净资产收益率%": float,
            "每股收益": float,
        }
    """
    secucode = _get_eastmoney_secid(stock_code)
    tencent_symbol = _get_tencent_symbol(stock_code)

    result = {
        "当前市值(亿)": None,
        "市盈率TTM": None,
        "股息率%": None,
        "市净率": None,
        "市销率": None,
        "净资产收益率%": None,
        "每股收益": None,
    }

    # ── 1. 从东方财富获取财务报表数据 ──
    financial_data = _fetch_financial_report(secucode)
    if financial_data:
        result.update(financial_data)

    # ── 2. 从腾讯行情获取股息率等实时指标 ──
    tencent_data = _fetch_tencent_indicators(tencent_symbol)
    if tencent_data:
        # 股息率用腾讯的实时数据
        if tencent_data.get("股息率%") is not None:
            result["股息率%"] = tencent_data["股息率%"]

    # ── 3. 基于最新收盘价计算比率的指标 ──
    if latest_close and latest_close > 0:
        eps = result.get("每股收益")
        if eps and eps > 0:
            result["市盈率TTM"] = round(latest_close / eps, 2)

        bps = result.get("每股净资产")
        if bps and bps > 0:
            result["市净率"] = round(latest_close / bps, 2)

        total_shares = result.get("_总股本")
        if total_shares and total_shares > 0:
            market_cap = round(total_shares * latest_close / 100000000, 2)
            result["当前市值(亿)"] = market_cap

            # 市销率 = 市值 / 营业收入
            revenue = result.get("_营业收入")
            if revenue and revenue > 0:
                result["市销率"] = round(market_cap / (revenue / 100000000), 2)

    # 清理内部字段
    for key in list(result.keys()):
        if key.startswith("_"):
            del result[key]

    return result


def _fetch_financial_report(secucode: str) -> Optional[Dict]:
    """
    从东方财富数据中心获取财务报表数据
    """
    params = {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": (
            "SECUCODE,SECURITY_NAME_ABBR,"
            "EPSJB,BPS,ROEJQ,"
            "TOTAL_SHARE,TOTALOPERATEREVE,"
            "MGWFPLR"
        ),
        "filter": f"(SECUCODE=\"{secucode}\")",
        "pageNumber": 1,
        "pageSize": 1,
        "sortTypes": "-REPORT_DATE",
        "sortColumns": "",
        "source": "HSFGO",
        "client": "PC",
        "v": "0.1",
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://data.eastmoney.com",
    }

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=15.0, headers=headers) as client:
                resp = client.get(FINANCE_API_URL, params=params)
                resp.raise_for_status()
                raw = resp.json()
            break
        except Exception as e:
            if attempt < max_retries:
                time.sleep(attempt * 2)
            else:
                print(f"  [警告] 财务数据获取失败: {e}")
                return None

    if not raw.get("result") or not raw["result"].get("data"):
        print("  [警告] 财务数据为空")
        return None

    item = raw["result"]["data"][0]
    data = {}

    # 每股收益
    eps = item.get("EPSJB")
    if eps is not None:
        data["每股收益"] = round(float(eps), 4)

    # 每股净资产
    bps = item.get("BPS")
    if bps is not None:
        data["每股净资产"] = round(float(bps), 4)

    # 净资产收益率 (%)
    roe = item.get("ROEJQ")
    if roe is not None:
        data["净资产收益率%"] = round(float(roe), 2)

    # 总股本
    total_share = item.get("TOTAL_SHARE")
    if total_share is not None:
        data["_总股本"] = float(total_share)

    # 营业收入
    revenue = item.get("TOTALOPERATEREVE")
    if revenue is not None:
        data["_营业收入"] = float(revenue)

    return data


def _fetch_tencent_indicators(tencent_symbol: str) -> Optional[Dict]:
    """
    从腾讯行情获取股息率等实时指标
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }

    try:
        with httpx.Client(timeout=10.0, headers=headers) as client:
            resp = client.get(f"{TENCENT_QT_URL}={tencent_symbol}")
            resp.raise_for_status()
            text = resp.text

        if "=\"" not in text:
            return None

        data_part = text.split("=\"", 1)[1].rstrip(";\";\n\r ")
        fields = data_part.split("~")

        data = {}

        # [64] = 股息率 (%)
        if len(fields) > 64 and fields[64]:
            try:
                val = float(fields[64])
                if val > 0:
                    data["股息率%"] = val
            except ValueError:
                pass

        # [62] = 市盈率(动)
        if len(fields) > 62 and fields[62]:
            try:
                val = float(fields[62])
                if val > 0:
                    data["市盈率(动)"] = val
            except ValueError:
                pass

        return data

    except Exception as e:
        print(f"  [警告] 腾讯行情获取失败: {e}")
        return None
