#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
缓存管理器 —— 将抓取并填充好的股票数据缓存到本地 JSON 文件

功能：
  1. 缓存已获取的 K 线数据（含财务指标衍生字段），避免重复 API 调用
  2. 检查缓存是否覆盖完整日期范围
  3. 合并新旧数据（按日期去重，新数据优先）
  4. 快速获取缓存数据的日期范围

缓存文件独立存储在 cache/ 目录下，与 Excel 输出文件同名但扩展名为 .cache.json
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple


class CacheError(Exception):
    """缓存操作异常"""
    pass


def get_cache_path(output_path: str, cache_dir: str) -> str:
    """
    获取缓存文件路径

    缓存文件独立存储在 cache/ 目录下，与 Excel 输出文件同名但扩展名为 .cache.json

    Args:
        output_path: Excel 输出文件路径（如 data/神火股份抓取数据.xlsx）
        cache_dir: 缓存文件存放目录（如 cache/）

    Returns:
        缓存文件路径（如 cache/神火股份抓取数据.cache.json）
    """
    base_name = os.path.basename(output_path)
    name, _ = os.path.splitext(base_name)
    return os.path.join(cache_dir, name + ".cache.json")


def load_from_cache(cache_path: str) -> Optional[List[Dict]]:
    """
    从缓存文件加载股票数据

    Args:
        cache_path: 缓存文件路径

    Returns:
        数据列表（按日期降序，最新在前），缓存不存在或损坏时返回 None
    """
    if not os.path.exists(cache_path):
        return None

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            print(f"  [警告] 缓存文件格式错误（非数组），将重新抓取")
            return None
        print(f"  [缓存] 从本地加载 {len(data)} 条缓存数据")
        return data
    except (json.JSONDecodeError, IOError) as e:
        print(f"  [警告] 缓存文件读取失败: {e}，将重新抓取")
        return None


def save_to_cache(cache_path: str, data: List[Dict]) -> None:
    """
    将股票数据保存到缓存文件

    Args:
        cache_path: 缓存文件路径
        data: 数据列表（按日期降序，最新在前）
    """
    try:
        # 先写入临时文件，再重命名，防止写入中断导致缓存损坏
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, cache_path)
    except IOError as e:
        print(f"  [警告] 缓存写入失败: {e}")


def get_date_range(data: List[Dict]) -> Tuple[Optional[str], Optional[str]]:
    """
    获取缓存数据的日期范围

    Args:
        data: 数据列表

    Returns:
        (最早日期, 最晚日期)，data 为空时返回 (None, None)
    """
    if not data:
        return None, None

    dates = [r.get("日期", "") for r in data if r.get("日期")]
    if not dates:
        return None, None

    return min(dates), max(dates)


def needs_fetch(
    cached_data: Optional[List[Dict]],
    start_date: str,
    end_date: str,
) -> bool:
    """
    判断是否需要从 API 抓取新数据

    Args:
        cached_data: 已有的缓存数据（可能为 None）
        start_date: 配置文件中的起始日期
        end_date: 配置文件中的截止日期

    Returns:
        True 表示需要从 API 抓取，False 表示缓存已完整覆盖
    """
    if not cached_data:
        return True

    min_date, max_date = get_date_range(cached_data)
    if min_date is None or max_date is None:
        return True

    # 如果起始日期早于缓存最早数据 → 需要补充历史数据
    if start_date and start_date < min_date:
        print(f"  [缓存] 起始日期 {start_date} 早于缓存最早 {min_date}，需要补充历史数据")
        return True

    # 如果截止日期晚于缓存最新数据 → 需要获取最新数据
    if end_date and end_date > max_date:
        print(f"  [缓存] 截止日期 {end_date} 晚于缓存最新 {max_date}，需要获取新数据")
        return True

    return False


def needs_fetch_start(
    cached_data: Optional[List[Dict]],
    start_date: str,
) -> bool:
    """
    判断是否需要补充历史数据（比缓存更早的数据）

    Args:
        cached_data: 已有缓存
        start_date: 配置的起始日期

    Returns:
        True 表示需要获取更早的历史数据
    """
    if not cached_data or not start_date:
        return bool(not cached_data)  # 无缓存则需要

    min_date, _ = get_date_range(cached_data)
    if min_date is None:
        return True

    return start_date < min_date


def merge_data(cached_data: List[Dict], new_data: List[Dict]) -> List[Dict]:
    """
    合并缓存数据和新数据，按日期去重

    规则：
      - 相同日期：新数据优先（覆盖缓存中的旧数据）
      - 不同日期：合并，统一按日期降序排列

    Args:
        cached_data: 旧缓存数据（按日期降序）
        new_data: 新抓取的数据（按日期降序）

    Returns:
        合并后的数据（按日期降序，最新在前）
    """
    # 用字典按日期索引，新数据覆盖旧数据
    date_map: Dict[str, Dict] = {}

    for record in cached_data:
        date = record.get("日期", "")
        if date:
            date_map[date] = record

    for record in new_data:
        date = record.get("日期", "")
        if date:
            date_map[date] = record

    if not date_map:
        return []

    # 按日期降序排序
    merged = sorted(date_map.values(), key=lambda x: x.get("日期", ""), reverse=True)
    return merged
