# Changelog

## v0.5.9(未发布)

### Fixed

- 东方财富 `searchapi` 搜索接口不再返回行情数据，导致 `stock/bond/futures.get_quote_history`、`get_latest_quote` 等所有需要「代码 → 行情ID」解析的接口抛出 `JSONDecodeError`
- `search_quote` 的异常兜底从未生效：环境装有 `simplejson` 时 requests 抛出的 `JSONDecodeError` 不属于标准库 `json.JSONDecodeError`，改为捕获 `ValueError` 与 `KeyError`
- `search_quote` 兜底分支中 `RuntimeWarning(...)` 只构造未发出，改为 `warnings.warn`
- `to_numeric` 装饰器在 pandas 3 下对 Series 逐项回写会抛 `TypeError`（字符串列改为 Arrow 后端承载，不接受写入 float），导致 `fund.get_base_info` 不可用；改为先转 object 再回写
- 索引重建全部失败时不再直接抛错，改为带 `RuntimeWarning` 退回磁盘上的过期缓存：`push2` 按出口 IP 限流可连续数天，而缓存一过 7 天 TTL，原实现会让所有代码→行情ID解析退化成修复前的不可用状态，且故障时刻由缓存时间戳决定、与代码无关。无缓存可退时仍然抛错，不吞掉坏状态

### Added

- 本地行情ID索引：由 `stock.get_realtime_quotes` 按市场抓取行情列表建立「代码、名称 → 行情ID」映射，缓存于 `efinance/data/quote-index.json`，7 天过期
- 索引按市场分组并发抓取（有序，分组顺序即优先级），并在构建时加锁：无锁时多只股票并发解析会按线程数放大请求量（实测 8 线程放大 12 倍，足以触发东方财富 WAF 对出口 IP 限流）
- 同一代码的跨市场多候选全部保留（如 `000001` 既是平安银行也是上证指数），`search_quote(count>1)` 返回候选列表；按名称查询时名称完全匹配的候选排在最前。缓存格式升级为带 `version` 字段，旧缓存自动迁移继续可用，不会因升级强制重建；部分市场获取失败时只在当前进程内使用，不写入缓存，避免残缺索引被复用整个过期周期
- 解析入参支持直接传入完整行情ID（如 `105.AAPL`），未纳入索引的市场可据此访问
- `tests/regression.py` 回归检查脚本，覆盖 39 个公开函数中的 38 个，含不依赖网络的并发、优先级、缓存迁移与过期兜底检查；因依赖缺失而未执行的检查会计入 SKIP 并给出非零退出码。`--offline` 只跑这 10 项自带打桩的检查，一条请求都不发，用于上游限流期间验证代码健康度

---

## v0.5.8(2026-03-18)

### Changed

- 更新正则表达式写法

---

## v0.5.5(2025-03-15)

### Fixed

- `get_realtime_quotes` 盘中数据存在重复数据

### Changed

- `get_all_company_performance` 和 `get_latest_holder_number`  速度优化

---

## v0.5.4(2025-03-10)

### Fixed

- `get_realtime_quotes` 分页更加智能

---

## v0.5.3(2025-02-17)

### Fixed

- `get_realtime_quotes` 自动分页以获取完整数据

---

## v0.5.2(2024-04-24)

### Fixed

- 补充 setup.py 缺失的依赖

---

## v0.5.1(2024-02-22)

### Added

- 增加用于检索股票市场的参数

---

## v0.5.0(2023-01-08)

### Added

- 增加获取 广期所 期货行情的功能
- 添加扩展市场类型的函数

---

## v0.4.9(2022-07-29)

### Added

- 为 `get_quote_history` 添加更多选项

---

## v0.4.8(2022-06-30)

### Fixed

- 修复 `stock` 模块的 `get_base_info` 函数的 bug

---

## v0.4.7(2022-06-24)

### Added

- 添加获取近 n 日 1 分钟 K 线的功能

### Fixed

- 修复部分函数命名拼写错误

---

## v0.4.6(2022-06-19)

### Changed

- 从 `stock` 模块中抽离出获取证券最新行情的函数
- 增强 `common` 模块中的配置以支持自定义获取更多属性

---

## v0.4.5(2022-06-05)

### Fixed

- 修复 `bond` 模块的 `get_base_info` 进行正股代码转换时出现的问题

## Added

为 `futures` 和 `bond` 模块添加获取最新交易日成交明细的功能

---

## v0.4.4(2022-04-29)

### Added

- 增加对 python3.10 及其以上版本的适配

### Changed

- 将 `stock` 模块里面的 `get_belong_plate` 重命名为 `get_belong_board`

---

## v0.4.3(2022-04-28)

### Added

- 为 `stock` 模块添加 `get_belong_plate` 函数，以支持获取股票所属板块
- 为 `stock` 模块添加 `get_deal_detail` 函数，以支持获取股票最新交易日的成交明细

---

## v0.4.2(2022-03-07)

### Changed

- 扩展 `get_quote_history` 函数，支持通过设定 `kwargs` 来指定返回类型

### Added

- 为 `stock` 模块添加函数 `get_quote_snapshot` 以支持获取沪深市场 3 秒行情快照

---

## v0.4.0(2021-12-02)

### Fiexd

- 修复 fund 模块的命名错误以及其他 bug

### Added

- 添加对获取北证 A 股市场行情的支持

---

## v0.3.9(2021-10-23)

### Changed

- 优化 `fund` 模块的获取基金列表的函数
- 更新 `common` 模块的行情配置文件以支持获取更多板块的行情数据

### Added

- 添加获取指数成分股的函数 `efnance.stock.get_members`
- 增强函数 `efnance.stock.get_realtime_quotes`
- 添加获取企业 IPO 审核情况的函数 `efnance.stock.get_latest_ipo_info`
- 为搜索缓存添加过期时间
- 增强获取股东户数变动情况的函数 `efnance.stock.get_latest_holder_number`

---

## v0.3.8(2021-09-18)

### Changed

- 为 `fund` 模块的获取实时基金涨幅估计函数添加更多字段

### Added

- 添加获取多个板块成分股实时行情以及板块历史行情的功能
- 添加获取 ETF、LOF 基金实时行情的功能

---

## v0.3.7(2021-08-30)

### Added

- 添加使用 `docker` 安装的方法说明
- 添加获取沪深 A 股股东数量的功能
- 为 `stock` 模块添加龙虎榜数据获取功能

---

### Fixed

- 修复 `fund` 模块获取基金代码的函数产生的 bug
- 修复 `fund` 模块中获取基金涨幅估计函数产生的 bug

### Changed

- 为函数 `efinance.stock.get_all_company_performance` 添加更多字段

## v0.3.6 (2021-08-21)

### Fixed

- 修复创建缓存文件夹出错的问题

### Changed

- 简化部分代码

### Added

- 为 `futures` 模块添加实时行情获取功能

---

## v0.3.5 (2021-08-15)

### Added

- 添加沪深市场股票季度业绩表现数据获取功能
- 为股票实时行情数据添加更多字段
- 新增 `common` 模块，抽取出 `bond` 与 `stock` 的共同点放到其中
- 为 `bond` 模块添加获取历史单子流入数据获取的功能
- 添加更多的函数装饰器

---

## v0.3.4 (2021-08-08)

### Changed

- 修改部分注释文档缩进等级，优化其在 `vscode` 中的显示效果

---

## v0.3.3 (2021-08-08)

### Changed

- 修改项目文档主页链接

---

## v0.3.2 (2021-08-08)

### Added

- 使用 `sphinx` 构建文档
- 使用 read the docs 托管文档

---

## v0.3.1 (2021-08-05)

### Added

- 添加 session 机制

### Fixed

- 修复十大流通股东数据接口

---

## v0.3.0 (2021-08-03)

### Added

- 添加搜索结果缓存机制
- 引用 `jsonpath` 来解析数据

### Fixed

- 修复股票、债券实时行情接口表头错误（误把最高价写成最新价格）
