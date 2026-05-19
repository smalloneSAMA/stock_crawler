# A股股票日K线数据抓取工具

从配置文件中读取股票名称、代码、时间范围，通过 **AKShare** 接口抓取日K线数据，生成与模板 Excel 格式一致的 `.xlsx` 文件。

## 📁 文件结构

```
code/
├── main.py              # 主入口，启动程序
├── config_manager.py    # 配置读取与验证
├── data_fetcher.py      # AKShare 数据抓取
├── excel_generator.py   # Excel 文件生成
├── config.json          # 配置文件（可修改）
├── requirements.txt     # Python 依赖
└── README.md            # 本文件
```

## 🚀 快速开始

### 1️⃣ 安装依赖

```bash
cd code
pip install -r requirements.txt
```

### 2️⃣ 修改配置

编辑 `config.json`，填入需要的股票信息：

```json
{
    "stock_name": "神火股份",
    "stock_code": "000933",
    "start_date": "2024-04-19",
    "end_date": "2026-05-18",
    "output_file": "神火股份抓取数据.xlsx"
}
```

| 字段 | 说明 | 示例 |
|------|------|------|
| `stock_name` | 股票名称（仅用于展示） | `"神火股份"` |
| `stock_code` | 股票代码（可带或不带交易所后缀） | `"000933"` 或 `"000933.SZ"` |
| `start_date` | 开始日期 `YYYY-MM-DD` | `"2024-04-19"` |
| `end_date` | 结束日期 `YYYY-MM-DD` | `"2026-05-18"` |
| `output_file` | 输出文件名 | `"神火股份抓取数据.xlsx"` |

> **补充说明**：股票代码后缀 `.SZ`=深交所，`.SH`=上交所。不写后缀时默认深圳。

### 3️⃣ 运行程序

```bash
python main.py
```

也可指定自定义配置和输出：

```bash
python main.py -c my_config.json -o 我的数据.xlsx
```

## 📊 输出格式

生成的 Excel 文件包含以下 12 列，与模板格式一致：

| 列名 | 单位 | 说明 |
|------|------|------|
| 日期 | YYYY-MM-DD | 交易日 |
| 开盘价 | 元 | 开盘价格 |
| 最高价 | 元 | 当日最高 |
| 最低价 | 元 | 当日最低 |
| 收盘价 | 元 | 收盘价格（前复权） |
| 成交量(万手) | 万手 | 1手=100股 |
| 成交额（亿） | 亿元 | 成交金额 |
| 涨跌幅% | % | 当日涨跌幅 |
| 振幅% | % | 当日振幅 |
| 日内波动区间 | - | (最高-最低)/开盘价 |
| 上涨幅度 | - | 上涨日(收盘-开盘)/开盘价，下跌日为0 |
| 下跌幅度 | - | 下跌日(收盘-开盘)/开盘价，上涨日为0 |

## 📝 注意事项

1. **依赖安装**：首次使用需要 `pip install akshare openpyxl`
2. **数据源**：基于 [AKShare](https://github.com/akfamily/akshare) 开源接口，数据来自东方财富
3. **复权方式**：默认使用前复权（`qfq`），与大部分看盘软件一致
4. **网络要求**：需要联网获取数据
