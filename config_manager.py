"""
配置管理器 —— 负责读取和验证配置文件 (config.json)

支持两种格式：
  1. 新格式（推荐）：{ "stocks": [ { "stock_name": "...", "stock_code": "...", ... }, ... ] }
  2. 旧格式（兼容）：{ "stock_name": "...", "stock_code": "...", ... }
"""

import json
import os
from datetime import datetime
from typing import Dict, List


class ConfigError(Exception):
    """配置相关异常"""
    pass


def _normalize_stock_code(config: Dict) -> Dict:
    """格式化股票代码，加上交易所后缀"""
    stock_code = str(config["stock_code"]).strip()
    if stock_code.endswith(".SZ") or stock_code.endswith(".SH"):
        config["stock_code"] = stock_code
    else:
        exchange = config.get("exchange", "SZ").upper()
        config["stock_code"] = f"{stock_code}.{exchange}"
    return config


def _validate_single(config: Dict) -> Dict:
    """验证单只股票的配置"""
    if "stock_code" not in config or not config["stock_code"]:
        raise ConfigError("缺少必要配置字段: stock_code")

    config = _normalize_stock_code(config)

    # 验证日期格式
    for field in ("start_date", "end_date"):
        date_str = config.get(field, "")
        if date_str:
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                raise ConfigError(
                    f"{field} 日期格式错误: {date_str}，应为 YYYY-MM-DD"
                )

    # 默认值填充
    config.setdefault("stock_name", "未命名")
    config.setdefault("output_file", "stock_data.xlsx")

    return config


def load_config(config_path: str = "config.json") -> List[Dict]:
    """
    加载并验证配置文件，返回配置列表

    Args:
        config_path: 配置文件路径

    Returns:
        配置字典列表，每只股票一个

    Raises:
        ConfigError: 当配置无效或文件不存在时
    """
    if not os.path.exists(config_path):
        parent_config = os.path.join(os.path.dirname(__file__), config_path)
        if os.path.exists(parent_config):
            config_path = parent_config
        else:
            raise ConfigError(f"配置文件 {config_path} 不存在，请检查路径")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置文件解析失败: {e}")

    # ── 判断格式 ──
    if "stocks" in raw and isinstance(raw["stocks"], list):
        # 新格式：{ "stocks": [ {...}, {...} ] }
        stocks = raw["stocks"]
        if not stocks:
            raise ConfigError("配置文件中 stocks 列表为空")
        return [_validate_single(s) for s in stocks]

    elif "stock_code" in raw:
        # 旧格式（单个股票字典）
        return [_validate_single(dict(raw))]

    else:
        raise ConfigError(
            "无法识别的配置格式。请使用:\n"
            '  新格式: { "stocks": [ { "stock_code": "...", ... }, ... ] }\n'
            '  旧格式: { "stock_code": "...", ... }'
        )


def print_config_list(configs: List[Dict]) -> None:
    """打印配置列表信息"""
    print("=" * 50)
    print(f"[配置] 共 {len(configs)} 只股票待抓取")
    print("=" * 50)
    for i, cfg in enumerate(configs, 1):
        print(f"  [{i}] {cfg.get('stock_name', '未知')} ({cfg['stock_code']})")
        print(f"      时间: {cfg.get('start_date', '不限')} ~ {cfg.get('end_date', '不限')}")
        print(f"      输出: {cfg['output_file']}")
    print("=" * 50)
