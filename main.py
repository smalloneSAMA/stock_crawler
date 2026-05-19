#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
A股股票日K线批量数据抓取工具

支持在配置文件中配置多只股票，一次运行生成多个 Excel 文件。

用法：
    python main.py                              # 默认 config.json
    python main.py -c my_config.json            # 自定义配置
    python main.py -c my_config.json -o 前缀    # 统一输出目录
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_manager import ConfigError, load_config, print_config_list
from data_fetcher import DataFetchError, fetch_stock_data
from excel_generator import ExcelGenerateError, generate_excel
from financial_fetcher import FinancialFetchError, fetch_financial_indicators


def main():
    parser = argparse.ArgumentParser(
        description="A股股票日K线批量数据抓取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py
  python main.py -c my_config.json
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
    args = parser.parse_args()

    # ── 1. 加载配置列表 ──
    try:
        configs = load_config(args.config)
    except ConfigError as e:
        print(f"[错误] 配置错误: {e}")
        sys.exit(1)

    print_config_list(configs)

    # ── 确定输出目录 ──
    output_dir = args.output_dir or os.path.dirname(os.path.abspath(args.config))
    os.makedirs(output_dir, exist_ok=True)

    # ── 2. 逐只股票抓取 ──
    total_ok = 0
    total_fail = 0

    for idx, config in enumerate(configs, 1):
        stock_label = f"{config.get('stock_name', '未知')} ({config['stock_code']})"

        print(f"\n{'=' * 50}")
        print(f"[进度] 第 {idx}/{len(configs)} 只: {stock_label}")
        print(f"{'=' * 50}")

        # ── 2a. 抓取数据 ──
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

        # ── 预览 ──
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

        # ── 2b. 获取财务指标 ──
        financial_data = None
        if data:
            latest_close = data[0].get("收盘价") or 0
            if latest_close:
                try:
                    financial_data = fetch_financial_indicators(
                        stock_code=config["stock_code"],
                        latest_close=latest_close,
                    )
                except FinancialFetchError as e:
                    print(f"  [警告] 财务指标获取失败: {e}")

        if financial_data:
            print(f"[财务] 市值:{financial_data.get('当前市值(亿)', '-')}亿  "
                  f"PE:{financial_data.get('市盈率TTM', '-')}  "
                  f"PB:{financial_data.get('市净率', '-')}  "
                  f"ROE:{financial_data.get('净资产收益率%', '-')}%  "
                  f"EPS:{financial_data.get('每股收益', '-')}")
        else:
            print("[财务] 无法获取财务指标")

        # ── 2c. 生成 Excel ──
        output_file = os.path.join(output_dir, config["output_file"])
        print(f"\n[生成] 正在生成: {output_file}")

        try:
            generate_excel(
                stock_name=config.get("stock_name", ""),
                stock_code=config["stock_code"],
                start_date=config.get("start_date", ""),
                end_date=config.get("end_date", ""),
                data=data,
                financial_data=financial_data,
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

        total_ok += 1

    # ── 3. 最终汇总 ──
    print(f"\n{'=' * 50}")
    print(f"[汇总] 全部完成！成功 {total_ok} 只，失败 {total_fail} 只")
    if total_ok > 0:
        print(f"       输出目录: {os.path.abspath(output_dir)}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
