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
    加载并验证 JSON 配置文件

    必填字段：stock_code, output_file
    可选字段：stock_name, start_date, end_date, analysis

    Args:
        config_path: JSON 配置文件路径

    Returns:
        标准化后的股票配置列表

    Raises:
        ConfigError: 文件不存在、JSON 格式错误、缺少必填字段
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

        # 自动补全交易所后缀（6/9开头→沪市，其余→深市）
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
            "analysis": item.get("analysis"),  # 分析参数，可为 None
        })

    return validated


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
