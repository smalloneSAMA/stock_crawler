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
  6. T策略分析：日内做T + 波段做T 胜率统计（含技术指标），输出独立Excel

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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from config_manager import ConfigError, load_config, load_config_full, print_config_list
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
from t_strategy_analyzer import (
    analyze_intraday_t,
    analyze_swing_t,
    analyze_current_swing_signal,
    generate_t_excel,
    generate_current_signal_excel,
    generate_consolidated_signal_excel,
    analyze_swing_amplitude_distribution,
    generate_amplitude_distribution_excel,
    SWING_PERIODS,
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
        configs, global_config = load_config_full(args.config)
    except ConfigError as e:
        print(f"[错误] 配置错误: {e}")
        sys.exit(1)

    t_analysis_dates = global_config.get("_t_dates", [])
    if t_analysis_dates:
        if len(t_analysis_dates) == 1:
            print(f"[配置] 波段T分析日期: {t_analysis_dates[0]}")
        else:
            print(f"[配置] 波段T分析日期: {t_analysis_dates[0]} ~ {t_analysis_dates[-1]} (共{len(t_analysis_dates)}天)")

    print_config_list(configs)

    # ── 确定输出目录 ──
    base_dir = args.output_dir or os.path.dirname(os.path.abspath(args.config))
    data_dir = os.path.join(base_dir, "data")      # Excel 数据文件存放目录
    cache_dir = os.path.join(base_dir, "cache")     # 缓存数据存放目录
    report_dir = os.path.join(base_dir, "output")   # 分析报告存放目录
    t_analysis_dir = os.path.join(base_dir, "t_analysis")  # T策略分析报告存放目录
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(report_dir, exist_ok=True)
    os.makedirs(t_analysis_dir, exist_ok=True)

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

        # ── 2e. T策略分析（日内做T + 波段做T）──
        print(f"\n{'─' * 50}")
        print("[T策略] 正在分析日内做T及波段做T胜率...")
        try:
            raw_code = config["stock_code"].replace(".SZ", "").replace(".SH", "")
            t_output = os.path.join(
                t_analysis_dir,
                config.get("output_file", "report").replace(".xlsx", "_T策略分析.xlsx"),
            )

            # 日内做T分析
            intraday_result = analyze_intraday_t(data, max_gradient=10.0, step=0.5)
            if not intraday_result.get("数据不足", True):
                bs = intraday_result["基础统计"]
                print(f"  [日内T] {bs['总交易日']}个交易日, "
                      f"上涨日{bs['上涨日(收盘>开盘)']}个({bs['上涨日占比%']}%)")
                # 反T - 找胜率最高且触发天数>=5的梯度
                ft = intraday_result["反T"]
                best_ft_idx = -1
                for i in range(len(ft["梯度"])):
                    if ft["触发天数"][i] >= 5:
                        if best_ft_idx == -1 or ft["胜率%"][i] > ft["胜率%"][best_ft_idx]:
                            best_ft_idx = i
                if best_ft_idx >= 0:
                    print(f"  [反T] 最优: {ft['梯度'][best_ft_idx]}%梯度 "
                          f"(触发{ft['触发天数'][best_ft_idx]}天, "
                          f"胜率{ft['胜率%'][best_ft_idx]}%, "
                          f"均收益{ft['平均收益%'][best_ft_idx]}%)")
                # 正T - 找胜率最高且触发天数>=5的梯度
                zt = intraday_result["正T"]
                best_zt_idx = -1
                for i in range(len(zt["梯度"])):
                    if zt["触发天数"][i] >= 5:
                        if best_zt_idx == -1 or zt["胜率%"][i] > zt["胜率%"][best_zt_idx]:
                            best_zt_idx = i
                if best_zt_idx >= 0:
                    print(f"  [正T] 最优: {zt['梯度'][best_zt_idx]}%梯度 "
                          f"(触发{zt['触发天数'][best_zt_idx]}天, "
                          f"胜率{zt['胜率%'][best_zt_idx]}%, "
                          f"均收益{zt['平均收益%'][best_zt_idx]}%)")
            else:
                print(f"  [日内T] 数据不足")

            # 波段做T分析
            swing_result = analyze_swing_t(data)
            if not swing_result.get("数据不足", True):
                summary = swing_result.get("汇总", [])
                print(f"  [波段T] 统计 {swing_result['总交易日']} 个交易日")
                for s in summary:
                    print(f"    {s['周期']}日周期: 正T@{s['最佳正T阈值%']}%({s['正T胜率%']}%)  "
                          f"反T@{s['最佳反T阈值%']}%({s['反T胜率%']}%)")

                # ── 量能/背离辅助分析提示 ──
                # 取每个周期最佳阈值的量能和背离分布做示例
                for period in SWING_PERIODS:
                    pd = swing_result.get(period, {})
                    for s_item in summary:
                        if s_item["周期"] == period:
                            best_zt_th = s_item["最佳正T阈值%"]
                            best_ft_th = s_item["最佳反T阈值%"]
                            break
                    # 正T量能分析
                    if best_zt_th > 0:
                        zt_td = pd.get(f"threshold_{best_zt_th}", {})
                        zt_vol = zt_td.get("正T", {}).get("量能分布", {})
                        zt_div = zt_td.get("正T", {}).get("背离分布", {})
                        vol_parts = []
                        for v, info in sorted(zt_vol.items()):
                            vol_parts.append(f"{v}:{info['胜率%']}%({info['次数']}次)")
                        div_parts = []
                        for dv, info in sorted(zt_div.items()):
                            if dv != "无":
                                div_parts.append(f"{dv}:{info['胜率%']}%({info['次数']}次)")
                        if vol_parts:
                            print(f"    {period}日正T量能: {' | '.join(vol_parts)}")
                        if div_parts:
                            print(f"    {period}日正T背离: {' | '.join(div_parts)}")
                    # 反T量能分析
                    if best_ft_th > 0:
                        ft_td = pd.get(f"threshold_{best_ft_th}", {})
                        ft_vol = ft_td.get("反T", {}).get("量能分布", {})
                        ft_div = ft_td.get("反T", {}).get("背离分布", {})
                        vol_parts = []
                        for v, info in sorted(ft_vol.items()):
                            vol_parts.append(f"{v}:{info['胜率%']}%({info['次数']}次)")
                        div_parts = []
                        for dv, info in sorted(ft_div.items()):
                            if dv != "无":
                                div_parts.append(f"{dv}:{info['胜率%']}%({info['次数']}次)")
                        if vol_parts:
                            print(f"    {period}日反T量能: {' | '.join(vol_parts)}")
                        if div_parts:
                            print(f"    {period}日反T背离: {' | '.join(div_parts)}")
            else:
                print(f"  [波段T] 数据不足(需≥60条)")

            # 生成Excel
            generate_t_excel(intraday_result, swing_result,
                             config.get("stock_name", ""), config["stock_code"],
                             t_output)
            print(f"  [T策略] 已保存: {os.path.abspath(t_output)}")

            # ── 当前波段T信号分析（多日期支持）──
            if t_analysis_dates and not swing_result.get("数据不足", True):
                from t_strategy_analyzer import _build_tech_comment
                all_signals = []
                try:
                    # 只处理实际交易日（过滤周末/节假日）
                    actual_dates = sorted(set(r["日期"] for r in data if r.get("日期")))
                    valid_dates = [d for d in t_analysis_dates if d in actual_dates]
                    skipped = len(t_analysis_dates) - len(valid_dates)
                    if skipped:
                        print(f"  [过滤] 跳过{skipped}个非交易日(周末/节假日)")
                    if not valid_dates:
                        print(f"  [警告] 无有效交易日可分析")
                    else:
                        for dt in valid_dates:
                            sig = analyze_current_swing_signal(data, dt, swing_result)
                            if sig:
                                all_signals.append(sig)

                    if all_signals:
                        if len(all_signals) == 1:
                            # 单日期: 生成单个Excel
                            signal_output = os.path.join(
                                t_analysis_dir,
                                config.get("output_file", "report")
                                .replace(".xlsx", f"_波段T信号_{t_analysis_dates[0]}.xlsx"),
                            )
                            generate_current_signal_excel(
                                all_signals[0],
                                config.get("stock_name", ""),
                                config["stock_code"],
                                signal_output,
                            )
                            print(f"  [波段信号] 已保存: {os.path.abspath(signal_output)}")
                            # 打印技术指标
                            tech = all_signals[0].get("技术指标", {})
                            print(f"  [技术指标] {_build_tech_comment(tech)}")
                            for sig in all_signals[0].get("信号", []):
                                print(f"    {sig['周期']}日: {sig['方向']} {sig['区间涨跌幅%']:+.1f}% "
                                      f"→ {sig['建议操作']} (胜率{sig['历史胜率%']}% "
                                      f"| MACD:{sig['MACD']} RSI:{sig['RSI']})")
                        else:
                            # 多日期: 生成合并Excel（仅交易日）
                            date_label = f"{valid_dates[0]}_{valid_dates[-1]}"
                            consolidated_output = os.path.join(
                                t_analysis_dir,
                                config.get("output_file", "report")
                                .replace(".xlsx", f"_波段T信号_{date_label}.xlsx"),
                            )
                            generate_consolidated_signal_excel(
                                all_signals, valid_dates,
                                config.get("stock_name", ""),
                                config["stock_code"],
                                consolidated_output,
                            )
                            print(f"  [波段信号] 合并{len(all_signals)}天分析: "
                                  f"{os.path.abspath(consolidated_output)}")
                            # 打印首尾对比
                            first, last = all_signals[0], all_signals[-1]
                            ft = first.get("技术指标", {})
                            lt = last.get("技术指标", {})
                            print(f"  [首日] {_build_tech_comment(ft)}")
                            print(f"  [末日] {_build_tech_comment(lt)}")
                            for i in [0, -1]:
                                day_sig = all_signals[i]
                                date_str = t_analysis_dates[i]
                                actions = [s['建议操作'] for s in day_sig.get('信号', [])]
                                print(f"  {date_str}: {', '.join(actions)}")
                except Exception as e2:
                    print(f"  [警告] 当前波段信号分析失败: {e2}")
                    import traceback
                    traceback.print_exc()

        except Exception as e:
            print(f"  [警告] T策略分析失败: {e}")
            import traceback
            traceback.print_exc()

        # ── 2f. 波段涨跌幅历史分布分析（5/10/20/30/90日）──
        print(f"\n{'─' * 50}")
        print("[分布] 正在分析波段涨跌幅历史分布...")
        try:
            dist_result = analyze_swing_amplitude_distribution(
                data, periods=[5, 10, 20, 30, 90], bin_width=5.0
            )
            # 打印统计结果
            for p_str in ['5', '10', '20', '30', '90']:
                pd = dist_result.get(p_str, {})
                if pd.get('数据不足', True):
                    continue
                print(f"  [{p_str}日] 样本:{pd['样本数']}  "
                      f"范围:{pd['range']}%  "
                      f"时间:{pd.get('时间范围', '?')}")
                for side, label in [('上涨幅度', '上涨'), ('下跌幅度', '下跌')]:
                    sd = pd.get(side, {})
                    print(f"    {label}: 最大={sd['最大值']}% "
                          f"平均={sd['平均值']}% "
                          f"加权平均={sd['加权平均']}% "
                          f"中位数={sd['中位数']}% "
                          f"样本数={sd['样本数']}")
                    # 分布详情（取前5个密度最高的区间）
                    dist_d = sd.get('分布', {})
                    top5 = sorted(dist_d.items(), key=lambda x: x[1]['次数'], reverse=True)[:5]
                    if top5:
                        parts = [f"{k}:{v['次数']}次({v['加权次数']:.0f}w)" for k, v in top5]
                        print(f"      密集区间: {' | '.join(parts)}")

            # 生成分布分析Excel
            dist_output = os.path.join(
                t_analysis_dir,
                config.get("output_file", "report").replace(".xlsx", "_波段涨跌幅分布.xlsx"),
            )
            # 获取数据时间范围（data 按日期降序，最新在前）
            data_dates = [r.get('日期', '') for r in data if r.get('日期')]
            if data_dates:
                data_time_range = f"{data_dates[0]} ~ {data_dates[-1]}"
            else:
                data_time_range = "未知"

            generate_amplitude_distribution_excel(
                dist_result,
                config.get("stock_name", ""),
                config["stock_code"],
                dist_output,
                data_time_range=data_time_range,
            )
            print(f"  [分布] 已保存: {os.path.abspath(dist_output)}")
        except Exception as e_dist:
            print(f"  [警告] 波段涨跌幅分布分析失败: {e_dist}")
            import traceback
            traceback.print_exc()

        total_ok += 1

    # ── 3. 最终汇总 ──
    print(f"\n{'=' * 50}")
    print(f"[汇总] 全部完成！成功 {total_ok} 只，失败 {total_fail} 只")
    if total_ok > 0:
        print(f"       Excel数据: {os.path.abspath(data_dir)}")
        print(f"       缓存数据: {os.path.abspath(cache_dir)}")
        print(f"       分析报告: {os.path.abspath(report_dir)}")
        print(f"       T策略分析: {os.path.abspath(t_analysis_dir)}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
