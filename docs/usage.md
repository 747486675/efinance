# 使用文档

`efinance` 是一个 Python 金融数据获取库，主要基于东方财富、天天基金等公开接口，提供股票、基金、期货、债券等数据查询能力。返回结果通常是 `pandas.DataFrame` 或 `pandas.Series`，适合用于数据分析、量化研究和行情查看。

它是一个**库**，不是服务：没有需要启动的进程，没有端口，装好之后在 Python 里 `import` 即可使用。

## 安装与环境要求

见 [Installation](install.md) 页 —— 环境与虚拟环境准备、Windows 上 `python` 命令失效的说明、以及安装命令都在那里。

本文档余下部分默认你已在仓库根目录以开发模式装好本仓库，并且**装的是本仓库源码而非 PyPI 上的发布版**（两者行为不一致）。

## 快速上手

```python
import efinance as ef

# 贵州茅台 2024 年日线
df = ef.stock.get_quote_history("600519", beg="20240101", end="20241231")
print(df.head())
print(df.tail())
```

第一次调用会花几秒钟建立本地索引（见下文「行情ID与代码解析」），之后同一个代码是毫秒级返回。

确认装好了：

```python
import efinance as ef

print(len(ef.stock.get_realtime_quotes()))   # 五千多行，说明网络与依赖正常
```

## 行情ID与代码解析

东方财富的行情接口不使用 6 位证券代码，而是使用「行情ID」，格式为 `市场编号.代码`：

```text
0    深A                     113  上期所
1    沪A                     114  大商所
8    中金所                  115  郑商所
90   板块                    142  上海能源期货交易所
105  美股                    128  港股
155  英股                    225  广期所
```

库会在首次调用时抓取各市场的行情列表，在本地建立「代码、名称 → 行情ID」索引，因此下面三种写法都可以：

```python
import efinance as ef

ef.stock.get_quote_history("600519")      # 按代码
ef.stock.get_quote_history("贵州茅台")     # 按名称（需完全匹配）
ef.stock.get_quote_history("1.600519")    # 按行情ID
```

索引的基本情况：

```text
缓存位置   efinance/data/quote-index.json（已被 .gitignore 忽略）
过期时间   7 天
覆盖市场   沪深京A股、ETF、LOF、可转债、沪深系列指数、期货、港股
首次构建   数秒（各市场并发抓取）；之后读取缓存约 0.1 秒
```

### 几点需要注意的

- **名称是精确匹配，不是模糊搜索。** `"贵州茅台"` 可以，`"茅台"` 不行。接口返回的中文名称里常带空格（如 `"五 粮 液"`），两种写法都能识别。
- **同一代码可能属于多个市场。** 例如 `000001` 既是平安银行（`0.000001`）也是上证指数（`1.000001`），按代码查询时返回优先级更高的那个（A股在前）。要指定后者，直接传完整行情ID。
- **未纳入索引的市场（美股、英股、板块等）必须传完整行情ID**，例如 `ef.stock.get_quote_history("105.AAPL")`。
- **索引重建失败时会自动退回过期缓存**，并给出 `RuntimeWarning`：「暂继续使用已过期 N 天的旧索引」。这不是坏了 —— 上游按 IP 限流可以持续数天，而没有这条退路时，缓存一过 7 天有效期所有解析会直接抛错。此期间拿不到的只是这期间新上市或改码的标的；等窗口恢复后重建一次即可。
- **刚上市的标的可能查不到**，因为本地索引还没刷新：

```python
from efinance.utils import load_quote_index

load_quote_index(force_reload=True)
```

```{warning}
**不要频繁重建索引。** 一次重建会向 `push2.eastmoney.com` 发约 7 个全市场列表请求。反复重建会让上游按出口 IP 限流，表现为 `JSONDecodeError`、`ProxyError` 或 HTTP 503，且**每次重试都会延长限流窗口**。遇到这类报错请停止请求等待冷却，而不是加重试。
```

### 跳过解析

已经确定行情ID时，可以加 `quote_id_mode=True` 完全跳过解析这一步：

```python
df = ef.stock.get_quote_history("1.600519", quote_id_mode=True)
```

行情ID 可以在各市场的实时行情表里查到（无论哪个市场，第一列都是代码）：

```python
df = ef.stock.get_realtime_quotes("港股")
print(df[["股票代码", "股票名称", "行情ID"]])
```

## 功能一览

四个模块共 39 个函数，返回值除标注外均为 `pandas.DataFrame`。

### 股票 `ef.stock`（16 个）

```text
get_realtime_quotes              全市场实时行情
get_quote_history                历史 K 线（单只返回 DataFrame，多只返回 dict）
get_latest_quote                 指定代码的最新报价
get_quote_snapshot               单只盘口快照（返回 Series）
get_base_info                    基本信息（返回 Series / DataFrame）
get_belong_board                 所属板块
get_members                      指数成分股及权重
get_deal_detail                  成交明细
get_today_bill                   当日资金流
get_history_bill                 历史资金流
get_top10_stock_holder_info      十大股东
get_latest_holder_number         股东户数
get_daily_billboard              龙虎榜
get_latest_ipo_info              新股信息
get_all_company_performance      全市场业绩预告
get_all_report_dates             财报披露日期
```

### 基金 `ef.fund`（12 个）

```text
get_fund_codes                   全部基金代码列表
get_quote_history                历史净值
get_quote_history_multi          多只历史净值（返回 dict）
get_realtime_increase_rate       实时估算涨跌幅
get_base_info                    基本信息（返回 Series / DataFrame）
get_invest_position              持仓股票
get_industry_distribution        行业分布
get_types_percentage             资产配置比例
get_period_change                阶段涨幅
get_public_dates                 净值披露日期
get_fund_manager                 基金经理信息
get_pdf_reports                  下载定期报告 PDF（会写文件到本地）
```

### 债券 `ef.bond`（7 个）

```text
get_realtime_quotes              可转债实时行情
get_all_base_info                全部债券基本信息
get_base_info                    单只债券基本信息（返回 Series）
get_quote_history                历史行情
get_deal_detail                  成交明细
get_today_bill                   当日资金流
get_history_bill                 历史资金流
```

### 期货 `ef.futures`（4 个）

```text
get_realtime_quotes              期货实时行情
get_futures_base_info            期货品种基础信息
get_quote_history                历史 K 线（参数是行情ID）
get_deal_detail                  成交明细（参数是行情ID）
```

## 股票数据

```python
import efinance as ef

# 历史日线
df = ef.stock.get_quote_history("600519")

# 指定区间
df = ef.stock.get_quote_history("600519", beg="20240101", end="20241231")

# 多只：返回 dict，键是你传入的代码
data = ef.stock.get_quote_history(["600519", "300750"])
print(data["600519"])
print(data["300750"])

# 全市场实时行情
df = ef.stock.get_realtime_quotes()

# 指定市场的实时行情，可传列表
df = ef.stock.get_realtime_quotes("创业板")
df = ef.stock.get_realtime_quotes(["创业板", "港股"])

# 单只基本信息与盘口
info = ef.stock.get_base_info("600519")
snap = ef.stock.get_quote_snapshot("600519")

# 最新报价
df = ef.stock.get_latest_quote(["600519", "300750"])

# 所属板块
df = ef.stock.get_belong_board("600519")

# 指数成分股：需要传精确的指数名称或代码
df = ef.stock.get_members("沪深300")

# 资金流
df = ef.stock.get_today_bill("600519")
df = ef.stock.get_history_bill("600519")

# 成交明细（默认量大，用 max_count 控制）
df = ef.stock.get_deal_detail("600519", max_count=100)

# 龙虎榜、新股、股东户数、十大股东
df = ef.stock.get_daily_billboard()
df = ef.stock.get_latest_ipo_info()
df = ef.stock.get_latest_holder_number()
df = ef.stock.get_top10_stock_holder_info("600519")

# 业绩预告与财报日期
df = ef.stock.get_all_company_performance()
df = ef.stock.get_all_report_dates()
```

## 基金数据

```python
import efinance as ef

df = ef.fund.get_quote_history("161725")                    # 历史净值
df = ef.fund.get_quote_history_multi(["161725", "005827"])  # 返回 dict
df = ef.fund.get_realtime_increase_rate(["161725", "005827"])
df = ef.fund.get_fund_codes()                               # 全部基金代码
info = ef.fund.get_base_info("161725")
df = ef.fund.get_invest_position("161725")                  # 持仓
df = ef.fund.get_industry_distribution("161725")            # 行业分布
df = ef.fund.get_types_percentage("161725")                 # 资产配置比例
df = ef.fund.get_period_change("161725")                    # 阶段涨幅
dates = ef.fund.get_public_dates("161725")                  # 返回 list
df = ef.fund.get_fund_manager("161725")                     # 参数是基金代码
```

下载定期报告 PDF（会在当前目录创建 `pdf/` 并写入文件）：

```python
ef.fund.get_pdf_reports("161725", max_count=3, save_dir="pdf")
```

## 期货数据

期货的行情ID 形如 `113.fu2610`（上期所 燃油2610）、`115.ZCM`（郑商所某品种），需要从实时行情表里取：

```python
import efinance as ef

df = ef.futures.get_realtime_quotes()
print(df[["期货代码", "期货名称", "行情ID"]].head())

df = ef.futures.get_futures_base_info()     # 全部品种

quote_id = "115.ZCM"
df = ef.futures.get_quote_history(quote_id)
df = ef.futures.get_deal_detail(quote_id, max_count=100)
```

注意期货表的列名是 `期货代码` / `期货名称`，与 `ef.stock.get_realtime_quotes()` 的 `股票代码` / `股票名称` 不同。

## 债券与可转债数据

可转债会到期或强赎退市，**代码不要写死**，先从实时行情表里取了再用：

```python
import efinance as ef

df = ef.bond.get_realtime_quotes()
bond_code = df.iloc[0]["债券代码"]          # 当前在市的一只转债
print(df[["债券代码", "债券名称", "行情ID"]].head())

info = ef.bond.get_base_info(bond_code)     # 返回 Series
df = ef.bond.get_all_base_info()            # 全部债券
df = ef.bond.get_quote_history(bond_code)
df = ef.bond.get_today_bill(bond_code)
df = ef.bond.get_history_bill(bond_code)
df = ef.bond.get_deal_detail(bond_code, max_count=100)
```

## 常用参数

```python
ef.stock.get_quote_history(
    stock_codes,
    beg="20240101",
    end="20241231",
    klt=101,
    fqt=1,
)
```

```text
beg             开始日期，格式 YYYYMMDD
end             结束日期，格式 YYYYMMDD
klt             K 线周期间隔
fqt             复权方式
market_type     筛选市场类型，见下
suppress_error  代码有误时不报错，返回空结果
use_id_cache    是否使用本地代码索引缓存
quote_id_mode   参数已是完整行情ID，跳过解析
```

`klt` 取值：

```text
1    1 分钟      15   15 分钟     60   60 分钟     103  月线
5    5 分钟      30   30 分钟     101  日线        102  周线
```

`fqt` 取值：

```text
0  不复权    1  前复权（默认）    2  后复权
```

按市场类型筛选：

```python
from efinance.common.config import MarketType

ef.stock.get_quote_history("600519", market_type=MarketType.A_stock)
```

## 常见问题排查

**报 `JSONDecodeError` 或 `Expecting value: line 1 column 1`**
上游把连接断了（多为限流），返回的不是 JSON。停止请求等待冷却，见「不要频繁重建索引」。

**返回空 DataFrame，行数 0**
两种可能：代码已退市 / 不在市场列表里；或者代码解析成了错误的标的。先用实时行情表确认代码还在市：

```python
df = ef.stock.get_realtime_quotes()
print(df[df["股票代码"].astype(str) == "600519"])
```

**报 `证券代码 "xxx" 可能有误`**
索引里没有这个代码或名称。检查是否拼错、是否是刚上市的标的（`load_quote_index(force_reload=True)`）、或者名称没写全（只支持精确匹配）。

**导入时报缺少依赖**

```powershell
python -m pip install -e .
```

**中文列名在终端显示乱码**
Windows 终端编码问题，数据本身正常。设置 `LANG=C.UTF-8` 或改用 UTF-8 代码页。

## 回归检查（开发用）

仓库自带一个不依赖测试框架的检查脚本，改动行情ID解析或数据接口后跑一次：

```powershell
python tests/regression.py              # 全量，需要联网
python tests/regression.py --offline    # 只跑免网检查，不发任何请求
```

- 覆盖 39 个公开函数中的 38 个（`fund.get_pdf_reports` 会往磁盘写 PDF，有意排除）
- 样本代码动态取自实时行情表，因此不会因某只标的退市而误报
- 输出 `通过 / 失败 / 跳过` 三类计数；**任一失败或被跳过都以非零码退出**，避免接口不通时给出假绿灯
- 其中 6 项是离线检查（并发不放大请求、旧缓存迁移、部分失败不落盘等），用 `--offline` 可单独运行 —— 上游按 IP 限流期间，这是唯一可用的验证路径

```{note}
**全量检查的通过数跨运行不可比**，它衡量的是"当前网络能验证多少项"，不是代码健康度。判断有没有退化要看**错误类型**：`ProxyError` / `JSONDecodeError` / `Timeout` 属于网络问题，改代码没用；`ImportError` / `AttributeError` / `TypeError` / `KeyError` 才是代码坏了。
```

## 本地文档构建

文档用 Sphinx + MyST 构建，输出目录 `docs/build/` 已被 gitignore。

```powershell
cd docs
python -m pip install -r requirements.txt --upgrade
python -m sphinx -b html . ./build
```

构建中文文档：

```powershell
python -m sphinx -b html . ./build -D language=zh
```

生成结果在 `docs/build/index.html`。

## 注意事项

数据来自第三方公开接口，可能遇到网络超时、接口限流、字段变更、返回空数据或数据延迟。**本库只能保证拿到接口返回的内容，不能保证数值本身的准确性**，用于实盘交易前请自行与券商或官方数据核对。

本项目仅作学习交流之用，不构成任何投资建议。
