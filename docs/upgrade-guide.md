# 0.6.0 升级与使用指南

## 安装与兼容

核心库和 CLI 保留 Python 3.6 语法兼容；Python 3.6 可使用 `tests/constraints-py36.txt` 约束依赖。较新 Python 环境安装当前源码：

```powershell
py -3.12 -m pip install -e .
py -3.12 -m efinance doctor
```

基础安装不依赖 FastAPI、Plotly、Excel 或 Parquet 引擎。网页服务使用 Python 3.12：

```powershell
py -3.12 -m pip install -e ".[web]"
py -3.12 -m efinance web
```

浏览器访问 `http://127.0.0.1:8000`。网页静态资源和 Plotly.js 均从本机安装包提供，不访问 CDN。网页支持股票、基金、可转债、期货；基金最新查询展示估算涨幅，历史查询展示净值。

## 请求配置与错误处理

```python
import efinance as ef

ef.configure(connect_timeout=10, read_timeout=30, max_attempts=3,
             max_concurrency=8, requests_per_second=2, validation="warn")
print(ef.get_config())

try:
    frame = ef.stock.get_quote_history("1.600519", beg="20260101", end="20261016")
except ef.exceptions.RateLimitError as error:
    print("上游限流", error.attempts)
except ef.exceptions.NetworkError as error:
    print("连接或超时错误", error)
except ef.exceptions.ResponseError as error:
    print("上游格式发生变化", error)
```

配置更新是原子的，返回独立快照。请求开始后使用当时的配置。共享 Session 的显式 `timeout` 覆盖默认值。全局限速覆盖所有市场和重试请求，每次启动间隔至少 `1 / requests_per_second` 秒，不积累突发额度。

单请求默认总尝试 3 次，仅对连接失败、超时、429/500/502/503/504 重试，使用指数退避、抖动及 `Retry-After`；参数、HTTP 其他错误和格式错误不重试。旧历史批量接口的 `tries=3` 保留，指定时表示每次请求的总尝试次数。没有任务层重复重试。

显式重试预算会传递到分页线程及嵌套批量线程。线程复用后会恢复之前的上下文，避免前一批的设置影响后续任务。

异常均继承 `EfinanceError`，包括 `ParameterError`（同时是 `ValueError`）、`NetworkError`、`TimeoutError`、`RateLimitError`、`SymbolNotFoundError`、`ResponseError`、`DataQualityError`。请求异常保留底层原因与 `attempts`。

缺少参数、未知参数和已校验的错误输入在请求前报 `ParameterError`。响应数据类型、字段或记录宽度错误在解析区域报 `ResponseError`；本地实现的普通 TypeError/ValueError 不再统一伪装成上游异常。

## 批量结果、重试与取消

```python
import threading
import efinance as ef

cancel = threading.Event()
result = ef.batch.fetch(
    ef.stock.get_quote_history,
    ["1.600519", "0.000001", "1.600519"],
    kwargs={"beg": "20260101", "end": "20261016"},
    max_workers=4, tries=2, cancel_event=cancel,
    progress=lambda completed, total, item: print(completed, total, item),
)
print(result.successes)
print(result.failures)
print(result.cancelled)
result.retry_failed()  # 只补跑失败项，原成功项保留
```

输入必须为可哈希标的序列，按首次出现去重。结果按原输入顺序组织；回调在调用线程执行，参数为完成数量、总数量、标的。设置取消事件后停止调度；已经启动的请求会执行完或超时。重试前需要自行清除仍被设置的取消事件。若失败项的重试被取消，保留上一轮的失败记录，以便下一次 `retry_failed()` 再试；不会同时标记为失败和已取消。成功、失败和取消三类最终状态互斥。批次内总工作线程数不超过配置上限。

旧批量函数返回类型保持不变；发生失败时在调用线程抛出 `EfinanceError`，其 `result` 属性保存部分成功结果与失败清单。空输入在需要合并结果时返回空表。

## 数据口径与校验

- 股票、债券和期货历史周期支持 `1/5/15/30/60/101/102/103`，分别表示分钟、5/15/30/60 分钟、日、周、月；实际历史范围由上游决定。
- `fqt=0/1/2` 分别表示不复权、前复权、后复权；保持原有默认前复权。前复权历史价格可能为负，不把负值本身当成坏数据。
- 基金净值不接受周期和复权设置，CLI/网页按请求日期过滤现有净值接口返回值。
- 行情日期保持交易所当地时间；基金日期使用上游中国市场日期。网页的查询时间为含时区的服务端时间，不替代行情时间。
- 成交量、成交额、资金流、涨跌幅保留现有中文列和上游单位；不同市场/接口可能采用不同数量单位，不进行跨市场换算。金额与百分比单位参见对应原接口文档。
- 代码用字符串传递，保留前导零。CSV 导入 Excel 时应将代码列设置为文本；Excel 导出直接写文本列。
- 必需响应字段缺失始终报 `ResponseError`。正常无数据返回既定列结构的空表。
- 质量校验覆盖重复日期、非法日期、非单调时间和 OHLC 关系；基金历史允许倒序。默认 `warn`，不删除、不排序、不修正原数据；`strict` 报错，`off` 仅关闭质量提示。
- 基金历史的单位净值必须能解析为有限数值；累计净值允许缺失或为空串、`-`、`--`，但提供的非空值必须是有限数值。涨跌幅允许首日缺省。净值异常在 warn 模式告警、strict 模式报错、off 模式不检查；缺失必需响应字段不受 off 影响。

## 命令行与文件导出

```powershell
efinance --help
efinance quote 1.600519 --format json
efinance history 1.600519 --beg 20260101 --end 20261016 --klt 101 --fqt 1 --format csv --output maotai.csv
efinance history 000001 --asset fund --beg 20260101 --end 20261016
efinance quote 1.600519 0.000001 --format csv --output exports
efinance doctor
efinance doctor --online
```

单标的 `--output` 为文件，多标的为目录。批量导出包含各标的文件与 `failures.json`（全部成功时是空数组）。默认拒绝覆盖，使用 `--overwrite` 显式覆盖。CSV 使用 UTF-8 BOM，JSON 使用 UTF-8；不指定输出文件时只允许 CSV/JSON，JSON 多标的按行输出独立对象，CSV 多标的合成一张表并增加“查询标的”列。退出码：成功 0，参数错误 2，查询失败或部分失败 1。错误与原接口调试输出写 stderr。

单个文件写入或序列化失败时，批量导出继续处理其余标的。此类失败也写入 `failures.json`，包含 `stage: "export"`、输出 `path` 和 `attempts: 0`（本地导出不进行网络重试）；查询失败仍保留原有错误类型与尝试次数。失败清单按输入顺序排列；若清单文件本身无法写入，错误详情仍输出到 stderr，退出码为 1。

批量输出会在查询前按当前系统的路径大小写规则检查冲突，包含保留的 `failures.json` 文件名；`--overwrite` 仅允许覆盖之前存在的文件，不允许同批结果互相覆盖。CSV 与 JSON 重定向始终输出 UTF-8 字节，不受 Windows 控制台或管道的 GBK 编码影响；CSV 带 BOM，JSON 不带 BOM。

CLI 和网页的期货历史、最新查询均支持普通代码、名称和完整行情 ID，例如 `rbm` 或 `113.rbm`；普通输入先通过现有解析器解析。Python 原有 `ef.futures.get_quote_history` 接口仍按原约定接收完整行情 ID。

可选格式：

```powershell
py -3.12 -m pip install -e ".[excel,parquet]"
efinance history 1.600519 --format xlsx --output quotes.xlsx
efinance history 1.600519 --format parquet --output quotes.parquet
```

Python 也可调用 `efinance.export.export_table(frame, path, format=None, overwrite=False)`；不会创建行情数据库或保留服务端查询结果。

## 网页 API

`GET /api/health` 仅检查服务存活。`POST /api/quote`、`POST /api/history` 接受 JSON：

```json
{"asset":"stock","codes":["1.600519"],"beg":"20260101","end":"20261016","klt":101,"fqt":1}
```

最新行情查询省略日期、周期和复权参数；基金历史也省略周期和复权。单次最多 10 个标的，不支持任意函数执行或自定义上游地址。

响应 `results` 数组中的每项包含 `code/columns/records/queried_at/metadata`；NaN/Infinity/NaT 转为 null。错误包含 `error.type/message/attempts`，状态码分别为参数 422、未知标的 404、限流 429、上游错误 502、超时 504。

`POST /api/export` 使用相同参数并带 `kind: "history"` 或 `"quote"`，重新查询后流式下载 CSV；因此数值可能与之前的实时查询略有差异。导出不在服务器写结果文件。多标的查询任一失败会返回错误，Python/CLI 的批量接口用于部分成功结果处理。

## 验证命令

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py" -v
py -3.12 tests/offline_regression.py
py -3.12 -m compileall -q efinance
py -3.12 -m build
```

普通 CI 使用打桩数据并禁止外网；网页测试只允许 Windows asyncio 内部所需的本机 socketpair。手动联网回归使用 `python tests/regression.py`，可在 GitHub Actions 手动启动 `Manual upstream checks` 并下载结果。缺失可选网页依赖时核心环境跳过网页测试，专门的网页 CI 必须安装依赖后执行。

四类资产的受限联网验收可运行 `python tests/online_acceptance.py`，逐项输出通过、失败及依赖缺失而跳过的结果，并保存 JSON。市场列表暂时不可用时，可用 `--futures-id 113.rbm` 指定期货行情 ID 验证单标的接口；列表失败仍保留为失败，不自动计为通过。此脚本仅由手动联网 CI 执行。

完整接口回归可使用 `python tests/regression.py --connect-timeout 5 --read-timeout 10 --max-attempts 1 --report docs/full-online-regression-results.json`。这些参数设置全局请求默认值，接口自身显式指定的旧 `tries` 参数仍优先。报告记录每项通过、失败、跳过及不适用状态；前置接口返回空表时不会尝试从中选取标的，也不会把依赖检查计为通过。

浏览器回归需要 Node.js 20+ 和测试依赖 Playwright，不影响产品的无 Node 构建方式。另开终端启动 `python -m uvicorn efinance.web:create_app --factory --host 127.0.0.1 --port 8876`，安装 Playwright 及其 Chromium 浏览器后运行 `node tests/web_browser.cjs`。Windows 默认使用已安装的 Edge；可用 `EFINANCE_BROWSER_EXECUTABLE` 指定浏览器路径，用 `EFINANCE_PLAYWRIGHT_MODULE` 指向独立安装的 Playwright 模块。脚本验证三种视口，禁止外部资源请求，API 使用固定样本，结果与截图默认写入 docs；CI 则写入临时目录并上传工件。

安装 Playwright WebKit 后，可设置 `EFINANCE_BROWSER_ENGINE=webkit` 和独立的 `EFINANCE_BROWSER_OUTPUT` 目录运行同一套检查。`node tests/web_live_browser.cjs` 则直接访问真实上游，通过四类资产的历史查询、图表与下载验证浏览器闭环，不注入 API 样本。

其他手动验收入口（普通离线 CI 不运行）：

```powershell
py -3.12 tests/end_to_end_acceptance.py
py -3.12 tests/upstream_followup.py --bond-id 0.128142 --futures-id 113.rbm
py -3.12 tests/pdf_download_acceptance.py --code 000001
py -3.12 tests/connectivity_acceptance.py --rounds 2
py -3.12 tests/index_rebuild_acceptance.py
```

端到端脚本需要安装网页、Excel、Parquet 可选依赖及 httpx；PDF 验收只下载一份到临时目录，核对最新选择后自动清理。实际接口失败、空结果或前置接口不可用会如实记录，不返回模拟数据作为联网成功结果。

索引专项使用临时路径强制从真实七个市场构建，不读取旧缓存作为回退；记录原缓存文件前后校验值是否一致。任一市场失败或为空，均不将其认定为完整重建通过，依赖完整缓存的检查会明确跳过。
