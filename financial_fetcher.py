"""
财务指标获取器 —— 从东方财富数据中心获取股票基本面数据

提供功能：
  1. enrich_kline_with_financials()   - 逐行填充历史财务指标（含分红）

财务数据累加规则：
  东方财富报告的 EPS/营收 是「年初至报告日」的累计值 (year-to-date)。
  因此 TTM = 最新累计值 - 去年同期累计值 + 去年全年值。

每股股息推算规则：
  对于已有年报的财年：每股股息 ≈ EPS(全年) - (MGWFPLR(年末) - MGWFPLR(上年末))
  其中 MGWFPLR = 每股未分配利润。

分红率规则：
  分红率 = 该财年每股股息 / 该财年EPS × 100%（整年统一值）
  对于最新未完成财年（只有季报），使用最近完整财年的分红率来预估。
"""

import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx


class FinancialFetchError(Exception):
    """财务数据获取异常"""
    pass


# ── 东方财富数据中心API ──
FINANCE_API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _get_eastmoney_secid(stock_code: str) -> str:
    code = stock_code.replace(".SZ", "").replace(".SH", "").strip()
    return f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.SZ"


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_report_date(date_str: str) -> Tuple[int, int, int]:
    dt = datetime.strptime(date_str.split(" ")[0], "%Y-%m-%d")
    return dt.year, dt.month, dt.day


def _is_full_year(month: int) -> bool:
    return month == 12


# ============================================================
#  获取多期财报
# ============================================================

def _fetch_financial_report_list(secucode: str, page_size: int = 30) -> List[dict]:
    """获取多期财报数据（按报告日期降序，最新在前）"""
    params = {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": (
            "SECUCODE,SECURITY_NAME_ABBR,"
            "REPORT_DATE,"
            "EPSJB,BPS,ROEJQ,"
            "TOTAL_SHARE,TOTALOPERATEREVE,"
            "MGWFPLR"
        ),
        "filter": f'(SECUCODE="{secucode}")',
        "pageNumber": 1,
        "pageSize": page_size,
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
    for attempt in range(1, 4):
        try:
            with httpx.Client(timeout=15.0, headers=headers) as client:
                resp = client.get(FINANCE_API_URL, params=params)
                resp.raise_for_status()
                raw = resp.json()
            break
        except Exception as e:
            if attempt < 3:
                time.sleep(attempt * 2)
            else:
                print(f"  [警告] 财务数据获取失败: {e}")
                return []
    if not raw.get("result") or not raw["result"].get("data"):
        print("  [警告] 财务数据为空")
        return []
    return raw["result"]["data"]


# ============================================================
#  关键：预计算每个财年的 每股股息 和 分红率
# ============================================================

def _precompute_yearly_dividend_data(
    reports: List[dict],
) -> Dict[str, Dict[str, Optional[float]]]:
    """
    对所有完整的财政年度（12-31报告），计算：
      - 每股股息
      - 分红率（= 每股股息 / 该年EPS）

    返回 { '2025-12-31': {"_每股股息": ..., "_分红率": ...}, ... }
    """
    # 收集所有年报（按日期降序）
    fy_reports = {}
    for r in reports:
        dt = r["REPORT_DATE"].split(" ")[0]
        if dt.endswith("-12-31"):
            fy_reports[dt] = r

    sorted_fy = sorted(fy_reports.items(), reverse=True)  # 最新在前

    result = {}
    for i, (dt, r) in enumerate(sorted_fy):
        eps = _to_float(r.get("EPSJB")) or 0
        mgwf = _to_float(r.get("MGWFPLR")) or 0

        # 找上一年年报的 MGWFPLR
        prev_mgwf = mgwf  # 默认 = 自己
        if i + 1 < len(sorted_fy):
            prev_r = sorted_fy[i + 1][1]
            prev_mgwf = _to_float(prev_r.get("MGWFPLR")) or mgwf

        # 每股股息 = EPS - (当年MGWFPLR - 上年MGWFPLR)
        div = round(eps - (mgwf - prev_mgwf), 4)
        if div < 0:
            div = 0.0

        # 分红率 = 每股股息 / 该年EPS
        pay_ratio = round(div / eps * 100, 2) if eps > 0 else 0.0

        result[dt] = {
            "_每股股息": div,
            "_分红率": pay_ratio,
        }

    return result


def _get_applicable_dividend_data(
    kline_date: str,
    report_dates_sorted: List[str],
    yearly_div_data: Dict[str, Dict],
) -> Tuple[Optional[float], Optional[float]]:
    """
    根据K线日期，找到适用的 每股股息 和 分红率。

    规则：
      - 找到该日期最新的一个已知年报
      - 使用该年报的每股股息
      - 使用该年报的分红率
      - 如果该日期还没有任何年报（最早的数据之前），返回 None

    Returns:
        (每股股息, 分红率) 或 (None, None)
    """
    kd = datetime.strptime(kline_date, "%Y-%m-%d")

    # 在所有年报日期中，找 <= kline_date 的最新年报
    best_fy_date = None
    best_fy_dt = None
    for fy_date_str in sorted(yearly_div_data.keys(), reverse=True):
        fy_dt = datetime.strptime(fy_date_str, "%Y-%m-%d")
        if fy_dt <= kd:
            if best_fy_date is None or fy_dt > best_fy_dt:
                best_fy_date = fy_date_str
                best_fy_dt = fy_dt

    if best_fy_date is None:
        return None, None

    data = yearly_div_data[best_fy_date]
    return data.get("_每股股息"), data.get("_分红率")


# ============================================================
#  TTM 计算（现有逻辑）
# ============================================================

def _build_report_date_map(reports: List[dict]) -> Dict[str, dict]:
    result = {}
    for r in reports:
        result[r["REPORT_DATE"].split(" ")[0]] = r
    return result


def _compute_ttm_for_period(
    reports_by_date: Dict[str, dict],
    report_date: str,
) -> Dict[str, Optional[float]]:
    """计算指定报告期的 TTM 值（不含分红）"""
    report = reports_by_date.get(report_date)
    if not report:
        return {}

    y, m, d = _parse_report_date(report_date)
    eps = _to_float(report.get("EPSJB"))
    rev = _to_float(report.get("TOTALOPERATEREVE"))
    bps = _to_float(report.get("BPS"))
    roe = _to_float(report.get("ROEJQ"))
    shares = _to_float(report.get("TOTAL_SHARE"))

    result = {}
    if bps is not None:
        result["每股净资产"] = round(bps, 4)
    if shares is not None and shares > 0:
        result["_总股本"] = shares

    if _is_full_year(m):
        if eps is not None:
            result["每股收益"] = round(eps, 4)
        if rev is not None:
            result["_营业收入"] = rev
        if roe is not None:
            result["净资产收益率%"] = round(roe, 2)
        return result

    # TTM 计算
    same_q_key = f"{y - 1}-{m:02d}-{d:02d}"
    same_q_report = reports_by_date.get(same_q_key)

    fy_report = None
    for candidate_key in sorted(reports_by_date.keys(), reverse=True):
        cy, cm, _ = _parse_report_date(candidate_key)
        if cm == 12 and cy < y:
            fy_report = reports_by_date[candidate_key]
            break

    # TTM EPS
    if eps is not None and fy_report is not None:
        eps_fy = _to_float(fy_report.get("EPSJB"))
        if same_q_report is not None:
            eps_same = _to_float(same_q_report.get("EPSJB"))
            if eps_same is not None and eps_fy is not None:
                result["每股收益"] = round(eps - eps_same + eps_fy, 4)
            else:
                result["每股收益"] = round(eps, 4)
        elif eps_fy is not None:
            result["每股收益"] = round(eps, 4)
    elif eps is not None:
        result["每股收益"] = round(eps, 4)

    # TTM 营收
    if rev is not None and fy_report is not None:
        rev_fy = _to_float(fy_report.get("TOTALOPERATEREVE"))
        if same_q_report is not None:
            rev_same = _to_float(same_q_report.get("TOTALOPERATEREVE"))
            if rev_same is not None and rev_fy is not None:
                result["_营业收入"] = rev - rev_same + rev_fy
            else:
                result["_营业收入"] = rev
        elif rev_fy is not None:
            result["_营业收入"] = rev
    elif rev is not None:
        result["_营业收入"] = rev

    # ROE
    if fy_report is not None:
        roe_fy = _to_float(fy_report.get("ROEJQ"))
        if roe_fy is not None:
            result["净资产收益率%"] = round(roe_fy, 2)
    elif roe is not None:
        result["净资产收益率%"] = round(roe, 2)

    return result


def _precompute_ttm_map(reports: List[dict]) -> Dict[str, dict]:
    by_date = _build_report_date_map(reports)
    ttm_map = {}
    for r in reports:
        dt_str = r["REPORT_DATE"].split(" ")[0]
        ttm_map[dt_str] = _compute_ttm_for_period(by_date, dt_str)
    return ttm_map


def _find_applicable_report_date(
    kline_date_str: str, report_dates: List[str],
) -> Optional[str]:
    kd = datetime.strptime(kline_date_str, "%Y-%m-%d")
    best, best_dt = None, None
    for rd_str in report_dates:
        rd = datetime.strptime(rd_str, "%Y-%m-%d")
        if rd <= kd:
            if best_dt is None or rd > best_dt:
                best, best_dt = rd_str, rd
    return best


# ============================================================
#  主入口：逐行填充财务指标
# ============================================================

def enrich_kline_with_financials(
    kline_data: List[dict],
    stock_code: str,
) -> List[dict]:
    """
    对 K 线数据的每一行，根据对应日期当时已知的财报计算财务指标。

    为每一行添加字段:
        当前市值(亿)  市盈率TTM  市净率  市销率
        净资产收益率%  每股收益
        每股股息TTM  股息率TTM  分红率
    """
    if not kline_data:
        return kline_data

    secucode = _get_eastmoney_secid(stock_code)

    # ── 1. 获取所有财报数据 ──
    reports = _fetch_financial_report_list(secucode, page_size=30)
    report_dates = sorted([r["REPORT_DATE"].split(" ")[0] for r in reports])

    # ── 2. 预计算TTM ──
    ttm_map = _precompute_ttm_map(reports)

    # ── 3. 预计算每年的股息和分红率 ──
    yearly_div_data = _precompute_yearly_dividend_data(reports)

    # ── 4. 逐行填充 ──
    for row in kline_data:
        kline_date = row.get("日期", "")
        if not kline_date:
            continue

        # 找到适用的报告期
        applicable_rd = _find_applicable_report_date(kline_date, report_dates)
        if applicable_rd is None or applicable_rd not in ttm_map:
            continue

        fin = ttm_map[applicable_rd]
        close_price = _to_float(row.get("收盘价")) or 0.0
        eps = fin.get("每股收益")
        bps = fin.get("每股净资产")
        shares = fin.get("_总股本")
        revenue = fin.get("_营业收入")
        roe = fin.get("净资产收益率%")

        # ── 基础指标 ──
        if shares is not None and shares > 0 and close_price > 0:
            row["当前市值(亿)"] = round(shares * close_price / 100000000, 2)
        else:
            row["当前市值(亿)"] = None

        row["每股收益"] = round(eps, 4) if eps is not None else None

        if eps is not None and eps > 0 and close_price > 0:
            row["市盈率TTM"] = round(close_price / eps, 2)
        else:
            row["市盈率TTM"] = None

        if bps is not None and bps > 0 and close_price > 0:
            row["市净率"] = round(close_price / bps, 2)
        else:
            row["市净率"] = None

        mc = row.get("当前市值(亿)")
        if mc is not None and revenue is not None and revenue > 0:
            row["市销率"] = round(mc / (revenue / 100000000), 2)
        else:
            row["市销率"] = None

        row["净资产收益率%"] = round(roe, 2) if roe is not None else None

        # ── 分红指标（基于整年财年数据，非TTM混合）──
        dps, pay_ratio = _get_applicable_dividend_data(
            kline_date, report_dates, yearly_div_data
        )

        row["每股股息TTM"] = dps

        if dps is not None and dps > 0 and close_price > 0:
            row["股息率TTM"] = round((dps / close_price) * 100, 2)
        else:
            row["股息率TTM"] = None

        row["分红率"] = pay_ratio

    return kline_data
