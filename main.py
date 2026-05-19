#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
A股股票日K线批量数据抓取 + 分析决策工具

功能：
  1. 从新浪财经批量抓取 A 股日 K 线数据
  2. 从东方财富获取财务指标（PE/PB/ROE/每股收益/股息等），逐行填充
  3. 生成格式化 Excel 文件（原生 XML，无需 openpyxl）
  4. 自动分析：趋势选时 + 估值定仓 + 波动降本（网格/做T）
  5. 智能缓存：已有数据从本地读取，缺失部分才爬取更新

用法：
    python main.py                              # 默认 config.json
    python main.py -c my_config.json            # 自定义配置文件
    python main.py -c my_config.json -o 目录    # 自定义输出目录
    python main.py --no-cache                   # 忽略缓存，强制重新抓取
"""

import argparse
import os
import time
import sys
from datetime import datetime

# 将当前目录加入模块搜索路径，方便导入同级 .py 文件
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_manager import ConfigError, load_config, print_config_list
from data_fetcher import DataFetchError, fetch_stock_data
from excel_generator import ExcelGenerateError, generate_excel
from financial_fetcher import FinancialFetchError, enrich_kline_with_financials
from stock_analyzer import analyze_and_print
from cache_manager import (
    get_cache_path,
    load_from_cache,
    save_to_cache,
    get_date_range,
    needs_fetch,
    merge_data,
)


def main():
    # ── 解析命令行参数 ──
    parser = argparse.ArgumentParser(
        description="A股股票日K线批量数据抓取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py
  python main.py -c my_config.json
  python main.py --no-cache
        """,
    )
    parser.add_argument(
        "-c", "--config",
        default="config.json",
        help="配置文件路径（默认: config.json）",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="输出目录（默认与配置文件同目录）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="忽略本地缓存，强制重新抓取所有数据",
    )
    args = parser.parse_args()

    # ── 1. 加载配置列表 ──
    try:
        configs = load_config(args.config)
    except ConfigError as e:
        print(f"[错误] 配置错误: {e}")
        sys.exit(1)

    print_config_list(configs)

    # ── 确定输出目录 ──
    base_dir = args.output_dir or os.path.dirname(os.path.abspath(args.config))
    data_dir = os.path.join(base_dir, "data")      # Excel 数据文件存放目录
    cache_dir = os.path.join(base_dir, "cache")     # 缓存数据存放目录
    report_dir = os.path.join(base_dir, "output")   # 分析报告存放目录
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(report_dir, exist_ok=True)

    # ── 2. 逐只股票抓取 + 分析 ──
    total_ok = 0    # 成功处理的股票数
    total_fail = 0  # 失败的股票数

    for idx, config in enumerate(configs, 1):
        stock_label = f"{config.get('stock_name', '未知')} ({config['stock_code']})"

        print(f"\n{'=' * 50}")
        print(f"[进度] 第 {idx}/{len(configs)} 只: {stock_label}")
        print(f"{'=' * 50}")

        start_date = config.get("start_date", "")
        end_date = config.get("end_date", "")

        # ── 2a. 确定缓存路径，尝试加载本地缓存 ──
        base_output = config["output_file"]
        output_file = os.path.join(data_dir, base_output)
        cache_path = get_cache_path(output_file, cache_dir)

        cached_data = None
        use_cache = not args.no_cache
        fetched_new = False  # 标记是否从 API 获取了新数据

        if use_cache:
            cached_data = load_from_cache(cache_path)
            if cached_data:
                min_date, max_date = get_date_range(cached_data)
                print(f"  [缓存] 本地数据范围: {min_date or 'N/A'} ~ {max_date or 'N/A'} ({len(cached_data)} 条)")

        # ── 判断是否需要抓取 ──
        if use_cache and cached_data and not needs_fetch(cached_data, start_date, end_date):
            # 缓存数据完整，直接使用
            data = cached_data
            print(f"  [缓存] [OK] 本地缓存已覆盖全部日期范围，跳过 API 抓取")
        else:
            # 需要从 API 抓取
            if use_cache and cached_data:
                min_date, max_date = get_date_range(cached_data)
                print(f"  [缓存] 缓存不完整，将从 API 补充缺失数据...")
            else:
                print(f"  [缓存] 无本地缓存，将从 API 全新抓取...")

            try:
                data = fetch_stock_data(config)
            except DataFetchError as e:
                print(f"[错误] {stock_label} 数据抓取失败: {e}")
                total_fail += 1
                continue

            if not data:
                print(f"[警告] {stock_label} 未获取到任何数据，跳过")
                total_fail += 1
                continue

            print(f"[数据] 共获取 {len(data)} 条记录")

            # ── 2b. 从东方财富获取财务数据，逐行填充 PE/PB/ROE/股息等 ──
            try:
                data = enrich_kline_with_financials(
                    kline_data=data,
                    stock_code=config["stock_code"],
                )
            except FinancialFetchError as e:
                print(f"  [警告] 财务指标填充失败: {e}")

            # ── 与本地缓存合并（新数据覆盖同日期旧数据）──
            if use_cache and cached_data:
                before_count = len(data)
                data = merge_data(cached_data, data)
                merged_new = len(data) - len(cached_data)
                if merged_new > 0:
                    print(f"  [缓存] 合并完成: 新增 {merged_new} 条，共 {len(data)} 条")
                else:
                    print(f"  [缓存] 合并完成: 共 {len(data)} 条（数据未变化）")

                # ── 保存更新后的数据到缓存 ──
                save_to_cache(cache_path, data)
            elif use_cache:
                # 全新数据，写入缓存
                save_to_cache(cache_path, data)

            fetched_new = True

        # ── 预览数据 ──
        print("\n[预览] 前 3 条:")
        for i, row in enumerate(data[:3], 1):
            print(f"  {i}. {row.get('日期','')}  "
                  f"开:{row.get('开盘价','')}  "
                  f"高:{row.get('最高价','')}  "
                  f"低:{row.get('最低价','')}  "
                  f"收:{row.get('收盘价','')}  "
                  f"涨跌:{row.get('涨跌幅%','')}%")
        if len(data) > 3:
            print(f"  ... 共 {len(data)} 条")
            print("  后 2 条:")
            for i, row in enumerate(data[-2:], len(data) - 1):
                print(f"  {i}. {row.get('日期','')}  "
                      f"开:{row.get('开盘价','')}  "
                      f"高:{row.get('最高价','')}  "
                      f"低:{row.get('最低价','')}  "
                      f"收:{row.get('收盘价','')}  "
                      f"涨跌:{row.get('涨跌幅%','')}%")

        # ── 预览最新一行的财务指标 ──
        if data:
            latest = data[0]
            mc = latest.get("当前市值(亿)", "-")
            pe = latest.get("市盈率TTM", "-")
            pb = latest.get("市净率", "-")
            roe = latest.get("净资产收益率%", "-")
            eps = latest.get("每股收益", "-")
            dps = latest.get("每股股息TTM", "-")
            dy = latest.get("股息率TTM", "-")
            pr = latest.get("分红率", "-")
            print(f"[财务] 市值:{mc}亿  PE:{pe}  PB:{pb}  "
                  f"ROE:{roe}%  EPS:{eps}")
            if dps is not None and dps != "-":
                print(f"[分红] 每股股息:{dps}  股息率:{dy}%  分红率:{pr}%")

        # ── 2c. 运行分析决策（趋势 + 估值 + 波动降本）──
        print(f"\n{'─' * 50}")
        print("[分析] 正在生成决策清单...")
        try:
            report = analyze_and_print(
                stock_name=config.get("stock_name", ""),
                stock_code=config["stock_code"],
                data=data,
                analysis_config=config.get("analysis"),
            )
            # 分析报告保存到 output 文件夹
            report_file = os.path.join(
                report_dir,
                config.get("output_file", "report").replace(".xlsx", "_分析报告.txt"),
            )
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"[报告] 已保存: {os.path.abspath(report_file)}")
        except Exception as e:
            print(f"  [警告] 分析生成失败: {e}")
            import traceback
            traceback.print_exc()

        # ── 2d. 生成 Excel 文件（原生 XML，无需第三方库）──
        # 若文件被占用则加时间戳后缀，避免写入失败
        if os.path.exists(output_file):
            try:
                os.remove(output_file)
            except PermissionError:
                name, ext = os.path.splitext(base_output)
                output_file = os.path.join(data_dir, f"{name}_{int(time.time())}{ext}")
                print(f"  [注意] 原文件被占用，另存为: {os.path.basename(output_file)}")
        print(f"\n[生成] 正在生成: {output_file}")

        try:
            generate_excel(
                stock_name=config.get("stock_name", ""),
                stock_code=config["stock_code"],
                start_date=start_date,
                end_date=end_date,
                data=data,
                output_path=output_file,
            )
        except ExcelGenerateError as e:
            print(f"[错误] Excel 生成失败: {e}")
            total_fail += 1
            continue
        except PermissionError:
            print(f"[错误] 无法写入 {output_file}，请检查文件是否被占用")
            total_fail += 1
            continue

        abs_path = os.path.abspath(output_file)
        print(f"[完成] 已保存: {abs_path}")
        print(f"       数据范围: {data[0].get('日期','?')} ~ {data[-1].get('日期','?')}")
        print(f"       交易日数: {len(data)}")
        print(f"       缓存文件: {os.path.abspath(cache_path)}")

        total_ok += 1

    # ── 3. 最终汇总 ──
    print(f"\n{'=' * 50}")
    print(f"[汇总] 全部完成！成功 {total_ok} 只，失败 {total_fail} 只")
    if total_ok > 0:
        print(f"       Excel数据: {os.path.abspath(data_dir)}")
        print(f"       缓存数据: {os.path.abspath(cache_dir)}")
        print(f"       分析报告: {os.path.abspath(report_dir)}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
