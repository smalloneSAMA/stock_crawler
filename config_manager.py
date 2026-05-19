#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
配置管理器 —— 加载并验证 JSON 格式的股票配置

配置文件 (config.json) 各字段说明：
{
    "stocks": [
        {
            "stock_name": "神火股份",         // 股票名称，仅用于显示
            "stock_code": "000933",           // 股票代码，自动匹配深市/沪市
            "start_date": "2022-04-19",       // 数据起始日期（含当天）
            "end_date":   "2026-05-18",       // 数据截止日期（含当天）
            "output_file": "神火股份.xlsx",    // 生成的 Excel 文件名

            "analysis": {                     // （可选）分析参数
                "grid_levels": 4,             // 网格交易档位数
                "atr_multiplier": 0.5,        // 网格间距 = ATR × 此系数
                "t_single_qty": 500           // 每笔做T股数，不设则自动估算
            }
        }
    ]
}
"""

import json
import os
from typing import Dict, List


class ConfigError(Exception):
    """配置加载或验证错误"""
    pass


def load_config(config_path: str) -> List[Dict]:
    """
    加载并验证 JSON 配置文件（仅股票配置列表）

    Returns:
        标准化后的股票配置列表
    """
    stocks, _ = load_config_full(config_path)
    return stocks


def load_config_full(config_path: str):
    """
    加载完整配置，返回 (股票配置列表, 全局配置字典)

    全局配置字段（可选）：
      - t_analysis_date: 做T分析日期 (如 "2026-05-19")
    """
    if not os.path.exists(config_path):
        raise ConfigError(f"配置文件不存在: {config_path}")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"JSON 解析失败: {e}")

    # 根节点必须包含 stocks 数组
    if not isinstance(raw, dict) or "stocks" not in raw:
        raise ConfigError("配置文件根节点必须包含 'stocks' 数组")

    stocks = raw["stocks"]
    if not isinstance(stocks, list) or len(stocks) == 0:
        raise ConfigError("'stocks' 必须是非空数组")

    validated = []
    required_fields = ["stock_code", "output_file"]

    for idx, item in enumerate(stocks, 1):
        for field in required_fields:
            if field not in item or not item[field]:
                raise ConfigError(f"第 {idx} 只股票缺少必填字段: {field}")

        code = str(item["stock_code"]).strip()
        if not code.endswith(".SZ") and not code.endswith(".SH"):
            if code.startswith(("6", "9")):
                code = f"{code}.SH"
            else:
                code = f"{code}.SZ"

        validated.append({
            "stock_name": item.get("stock_name", ""),
            "stock_code": code,
            "start_date": item.get("start_date", ""),
            "end_date": item.get("end_date", ""),
            "output_file": item["output_file"],
            "analysis": item.get("analysis"),
        })

    # 提取全局配置
    global_config = {}
    for key in ["t_analysis_date"]:
        if key in raw:
            global_config[key] = raw[key]
            # 解析为日期列表
            global_config["_t_dates"] = parse_analysis_dates(raw[key])

    return validated, global_config


def parse_analysis_dates(raw_value) -> List[str]:
    """
    解析 t_analysis_date 配置，返回日期字符串列表

    支持格式：
      - 单个日期: "2026-05-19"
      - 日期范围: "2026-01-01~2026-01-31" 或 "2026-01-01 ~ 2026-01-31"
      - 日期列表: ["2026-01-01", "2026-01-15"]
    """
    if raw_value is None:
        return []

    # 列表格式
    if isinstance(raw_value, list):
        return [str(d).strip() for d in raw_value if str(d).strip()]

    # 字符串格式
    s = str(raw_value).strip()
    if not s:
        return []

    # 范围格式: "2026-01-01~2026-01-31"
    from datetime import datetime, timedelta
    for sep in ["~", "～"]:
        if sep in s:
            parts = s.split(sep)
            if len(parts) == 2:
                a, b = parts[0].strip(), parts[1].strip()
                if a and b:
                    try:
                        sd = datetime.strptime(a, "%Y-%m-%d")
                        ed = datetime.strptime(b, "%Y-%m-%d")
                        if sd > ed:
                            sd, ed = ed, sd
                        dates = []
                        cur = sd
                        while cur <= ed:
                            dates.append(cur.strftime("%Y-%m-%d"))
                            cur += timedelta(days=1)
                        return dates
                    except ValueError:
                        pass
            break

    # 单个日期
    return [s]


def print_config_list(configs: List[Dict]) -> None:
    """
    打印配置列表摘要到控制台

    Args:
        configs: load_config() 返回的配置列表
    """
    print(f"\n{'=' * 50}")
    print(f"[配置] 共 {len(configs)} 只股票待抓取")
    print(f"{'=' * 50}")

    for idx, cfg in enumerate(configs, 1):
        name = cfg.get("stock_name", "未知")
        code = cfg["stock_code"]
        start = cfg.get("start_date") or "不限"
        end = cfg.get("end_date") or "不限"
        out = cfg["output_file"]
        print(f"  [{idx}] {name} ({code})")
        print(f"      时间: {start} ~ {end}")
        print(f"      输出: {out}")

    print(f"{'=' * 50}")
