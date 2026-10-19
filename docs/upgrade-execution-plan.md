# efinance 升级执行文档

日期：2026-10-18，验证更新：2026-10-19。目标版本：0.6.0。状态：实现已交付；Windows 上 Python 3.6/3.9/3.12 与移动视口验证通过，Docker、远程 CI 和上游稳定性验收未完成。详见 upgrade-validation.md。

## 约束与基线

- 不引入数据库、ORM、Redis、数据仓库、定时入库、交易下单或回测。
- 保持 JSON 缓存路径、格式、TTL 和加载机制，只修复确认的缺陷。
- 核心库和 CLI 兼容 Python 3.6；可选网页服务使用 Python 3.12。
- 保持既有接口参数、中文字段、默认返回类型和复权语义。
- 只交付源码、测试、文档和部署模板，不发布、不部署到线上。
- 初始版本 0.5.8，已有 0.5.9 修复记录；基线离线回归 10 项通过，联网未全量验证。

## 阶段与验收

1. **0.5.9 修复收尾**：核对行情 ID 解析、缓存迁移、并发锁、过期回退、pandas 兼容；修复告警天数和测试说明。原有 10 项离线检查必须通过。
2. **请求、限流、异常**：新增 configure/get_config；默认连接 10 秒、读取 30 秒、总尝试 3 次、并发 8、每秒 2 次、校验 warn。请求层独占重试，指数退避与抖动，尊重 Retry-After；覆盖连接失败、超时、429/500/502/503/504。参数、标的和格式错误不重试。显式 timeout 优先，配置按请求快照。异常保留原因，参数异常兼容 ValueError。
3. **批量与数据规范**：batch.fetch 返回 successes/failures/cancelled，失败记录输入、类型、消息和尝试次数；retry_failed 只重试失败项，支持进度与取消，输入去重并保持顺序。旧批量入口使用有界执行器，错误传回调用线程。校验必需字段、重复时间、顺序和 OHLC；warn 不改数据，strict 报错，off 关闭质量检查。代码保留前导零，正常空表保留列。
4. **测试 CI**：保留离线回归，新增响应样本与异常测试；普通 CI 禁止外网，联网仅手动执行。核心覆盖 Python 3.6/3.9/3.12，pandas 3 与网页使用 3.12。验证语法、构建和干净安装，不全库格式化。
5. **CLI 与导出**：argparse 提供 quote/history/doctor/web；doctor 默认离线。CSV 使用 UTF-8 BOM，支持 JSON，可选 Excel/Parquet。Excel 代码写文本；默认禁止覆盖，--overwrite 明确覆盖。批量按标的导出并写失败清单。成功/参数错误/失败退出码为 0/2/1。
6. **网页与部署模板**：FastAPI + 原生 HTML/CSS/JS + Plotly.js，静态资源本地提供。四类资产查询、图表、表格、错误反馈、CSV 下载。GET /api/health 不查询上游；POST /api/quote、/api/history、/api/export。响应含列、记录、查询时间、口径，非有限数值转 null。错误映射 422/404/429/502/504。默认 127.0.0.1 单进程；单次最多 10 标的，浏览器单任务。同步查询在有界执行器执行。导出重新查询并流式返回，不留结果文件。非本机绑定要求环境变量 Basic 认证。Docker 从当前源码构建；提供 Compose、反代与 HTTPS 说明，不实际部署。

## 公共接口与默认值

- ef.configure(connect_timeout=10, read_timeout=30, max_attempts=3, max_concurrency=8, requests_per_second=2, validation="warn")；ef.get_config() 返回独立配置快照。
- ef.batch.fetch(function, items, kwargs=None, max_workers=None, progress=None, cancel_event=None, tries=None)。BatchResult.retry_failed() 合并原成功项和重试结果。
- EfinanceError 派生 ParameterError、NetworkError、RateLimitError、SymbolNotFoundError、ResponseError、DataQualityError；TimeoutError 属于 NetworkError。
- 旧 tries 参数含义统一为单请求总尝试次数，不叠加任务重试。

## 完成判定

各阶段交付源码、测试和说明，最后同步版本到 0.6.0。所有离线测试、构建和安装验证通过；联网、旧 Python 实机、Docker 和浏览器验证逐项记录真实结果，未执行不得标为通过。版本 0.5.9 作为历史修复节点，不自动发布。实施结果和限制记录在 upgrade-validation.md。
