"""行情ID解析与历史行情接口的回归检查。

    python tests/regression.py              # 全量，需要联网
    python tests/regression.py --offline    # 只跑 6 项免网检查

全量用例会读取实时行情表以获得在用的转债、期货和基金代码，
因此不依赖任何可能退市的具体标的。上游按 IP 限流时先跑 ``--offline``：
它把解析逻辑自己打桩，不发任何请求，因此断网也能给出真实的代码健康度。

退出码：任一用例失败、或因依赖缺失被跳过时为 1。

覆盖 39 个公开函数中的 38 个，未纳入的一个：

- ``fund.get_pdf_reports``：会向磁盘下载 PDF，有副作用
"""

import json
import sys
import time
from pathlib import Path
from tempfile import mkdtemp
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import efinance as ef
from efinance import utils
from efinance.common.config import QUOTE_INDEX_GROUPS


class Checker:
    def __init__(self):
        self.failures = []
        # 依赖项失败导致未执行的检查：不能算通过，否则断网时会给出假绿灯
        self.skipped = []
        self.passed = 0

    def rows(self, name, function, min_rows=1):
        try:
            value = function()
        except Exception as e:
            self._fail(name, f"{type(e).__name__}: {e}")
            return None
        count = len(value) if hasattr(value, "__len__") else 0
        if count >= min_rows:
            self.passed += 1
            print(f"PASS  {name}: {count}")
        else:
            self._fail(name, f"返回 {count} 行，期望 >= {min_rows}")
        return value

    def equals(self, name, function, expected):
        try:
            value = function()
        except Exception as e:
            self._fail(name, f"{type(e).__name__}: {e}")
            return None
        if value == expected:
            self.passed += 1
            print(f"PASS  {name}: {value!r}")
        else:
            self._fail(name, f"得到 {value!r}，期望 {expected!r}")
        return value

    def skip(self, name, reason):
        """前置依赖失败，本项未执行"""
        self.skipped.append(name)
        print(f"SKIP  {name}: {reason}")

    def not_applicable(self, name, reason):
        """按设计可缺省的检查，不计入失败也不阻止绿灯"""
        print(f"N/A   {name}: {reason}")

    def _fail(self, name, detail):
        self.failures.append(name)
        print(f"FAIL  {name}: {detail}")
        return None


def first_column_value(dataframe, *names):
    columns = [name for name in names if name in dataframe.columns]
    return str(dataframe[columns[0]].iloc[0]) if columns else str(dataframe.iloc[0, 0])


def resolve_quote_id(keyword):
    quotes = utils.search_quote_from_index(keyword)
    return quotes[0].quote_id if quotes else None


def check_build_is_serialized(c: "Checker") -> None:
    """并发解析只应抓取一轮分组，不联网即可验证。

    无锁时每个工作线程各建一轮索引，请求量按线程数放大，
    足以把出口 IP 打进东方财富的 WAF 限流。
    """
    import efinance.stock.getter as stock_getter

    groups = len(QUOTE_INDEX_GROUPS)
    calls = []

    def fake(fs=None, **kwargs):
        calls.append(fs)
        time.sleep(0.05)
        return pd.DataFrame(
            {"股票代码": ["600519"], "股票名称": ["贵州茅台"], "行情ID": ["1.600519"]}
        )

    original_quotes = stock_getter.get_realtime_quotes
    original_path = utils.QUOTE_INDEX_CACHE_PATH
    original_index = dict(utils.QUOTE_INDEX)
    try:
        stock_getter.get_realtime_quotes = fake
        utils.QUOTE_INDEX_CACHE_PATH = str(Path(mkdtemp()) / "index.json")
        utils.QUOTE_INDEX.clear()
        threads = [Thread(target=utils.load_quote_index) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        c.equals(
            f"8 线程并发只抓取一轮分组（实际 {len(calls)} 次）",
            lambda: len(calls) // groups,
            1,
        )
    finally:
        stock_getter.get_realtime_quotes = original_quotes
        utils.QUOTE_INDEX_CACHE_PATH = original_path
        utils.QUOTE_INDEX.clear()
        utils.QUOTE_INDEX.update(original_index)


def check_legacy_cache_migrates(c: "Checker") -> None:
    """旧版本缓存（每代码单个候选、无 version 字段）必须能迁移使用。

    升级缓存格式时若直接丢弃旧缓存，会强制触发一次全市场重建，
    让没有网络的环境失去解析能力。
    """
    legacy = {
        "last_time": time.time(),
        "data": {"600519": {"q": "1.600519", "n": "贵州茅台", "t": "", "c": "AStock"}},
    }
    path = Path(mkdtemp()) / "legacy.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    original_path = utils.QUOTE_INDEX_CACHE_PATH
    original_index = dict(utils.QUOTE_INDEX)
    try:
        utils.QUOTE_INDEX_CACHE_PATH = str(path)
        utils.QUOTE_INDEX.clear()
        c.equals("旧版缓存可迁移读取", utils._read_quote_index_file, True)
        c.rows("迁移后值为候选列表", lambda: utils.QUOTE_INDEX.get("600519"), 1)
        c.equals(
            "迁移后解析结果不变",
            lambda: utils.search_quote_from_index("600519")[0].quote_id,
            "1.600519",
        )
    finally:
        utils.QUOTE_INDEX_CACHE_PATH = original_path
        utils.QUOTE_INDEX.clear()
        utils.QUOTE_INDEX.update(original_index)


def check_partial_build_not_cached(c: "Checker") -> None:
    """分组部分失败时不得写磁盘缓存。

    残缺索引一旦落盘，会在整个过期周期内被复用，缺失市场的标的静默解析不到。
    """
    import warnings as _warnings

    import efinance.stock.getter as stock_getter

    ok_market = QUOTE_INDEX_GROUPS[0][0]

    def fake(fs=None, **kwargs):
        if fs != ok_market:
            raise RuntimeError("模拟该市场获取失败")
        return pd.DataFrame(
            {"股票代码": ["600519"], "股票名称": ["贵州茅台"], "行情ID": ["1.600519"]}
        )

    path = str(Path(mkdtemp()) / "index.json")
    original_quotes = stock_getter.get_realtime_quotes
    original_path = utils.QUOTE_INDEX_CACHE_PATH
    original_index = dict(utils.QUOTE_INDEX)
    try:
        stock_getter.get_realtime_quotes = fake
        utils.QUOTE_INDEX_CACHE_PATH = path
        utils.QUOTE_INDEX.clear()
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            utils.load_quote_index(force_reload=True)
        c.equals(
            "部分失败时本进程仍可解析", lambda: resolve_quote_id("600519"), "1.600519"
        )
        c.equals("部分失败时不写磁盘缓存", lambda: Path(path).exists(), False)
    finally:
        stock_getter.get_realtime_quotes = original_quotes
        utils.QUOTE_INDEX_CACHE_PATH = original_path
        utils.QUOTE_INDEX.clear()
        utils.QUOTE_INDEX.update(original_index)


def check_stale_cache_backstops_build_failure(c: "Checker") -> None:
    """重建全部失败时必须退回过期索引，而不是让解析彻底不可用。

    上游按 IP 限流可以连续数天，而缓存一旦过了 TTL 就没有任何兜底：
    没有这条退路，故障会在某个与代码无关的时刻静默发生。
    """
    import warnings as _warnings

    import efinance.stock.getter as stock_getter

    def failing(fs=None, **kwargs):
        raise RuntimeError("模拟各市场全部获取失败（IP 限流）")

    stale_dir = mkdtemp()
    stale_path = str(Path(stale_dir) / "index.json")
    Path(stale_path).write_text(
        json.dumps(
            {
                "version": utils.QUOTE_INDEX_VERSION,
                # 故意写到 TTL 之外，验证"过期也仍能用"这条退路
                "last_time": time.time() - 99 * 86400,
                "data": {
                    "600519": [
                        {"q": "1.600519", "n": "贵州茅台", "t": "", "c": "AStock"}
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    original_quotes = stock_getter.get_realtime_quotes
    original_path = utils.QUOTE_INDEX_CACHE_PATH
    original_index = dict(utils.QUOTE_INDEX)
    try:
        stock_getter.get_realtime_quotes = failing
        utils.QUOTE_INDEX_CACHE_PATH = stale_path
        utils.QUOTE_INDEX.clear()
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            loaded = utils.load_quote_index()
        c.equals(
            "重建失败时退回过期索引",
            lambda: (loaded or {}).get("600519"),
            [{"q": "1.600519", "n": "贵州茅台", "t": "", "c": "AStock"}],
        )
        c.equals(
            "退回过期索引时给出 RuntimeWarning",
            lambda: any(
                issubclass(w.category, RuntimeWarning) and "过期" in str(w.message)
                for w in caught
            ),
            True,
        )
        c.equals("过期兜底后仍可解析", lambda: resolve_quote_id("600519"), "1.600519")

        # 没有缓存可退时不能假装成功，否则会把坏状态吞掉
        utils.QUOTE_INDEX.clear()
        utils.QUOTE_INDEX_CACHE_PATH = str(Path(mkdtemp()) / "absent.json")
        raised = False
        try:
            with _warnings.catch_warnings():
                _warnings.simplefilter("ignore")
                utils.load_quote_index()
        except Exception:
            raised = True
        c.equals("无缓存可退时仍然抛错", lambda: raised, True)
    finally:
        stock_getter.get_realtime_quotes = original_quotes
        utils.QUOTE_INDEX_CACHE_PATH = original_path
        utils.QUOTE_INDEX.clear()
        utils.QUOTE_INDEX.update(original_index)


OFFLINE_CHECKS = (
    check_build_is_serialized,
    check_legacy_cache_migrates,
    check_partial_build_not_cached,
    check_stale_cache_backstops_build_failure,
)


def run_offline_checks(c: "Checker") -> None:
    print("\n=== 离线检查 ===")
    for check in OFFLINE_CHECKS:
        check(c)


def finish(c: "Checker") -> int:
    print(f"\n通过 {c.passed} 项，失败 {len(c.failures)} 项，跳过 {len(c.skipped)} 项")
    if c.failures:
        print(f"失败明细：{c.failures}")
    if c.skipped:
        print(f"未执行明细：{c.skipped}")
    # 跳过同样不算通过，否则接口不通时会给出假绿灯
    return 1 if (c.failures or c.skipped) else 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    c = Checker()

    # NOTE 上游限流期间唯一可跑的验证路径：断网也能给出真实的代码健康度
    if "--offline" in sys.argv:
        run_offline_checks(c)
        return finish(c)

    print("\n=== 代码与名称解析 ===")
    index = c.rows("索引可构建", utils.load_quote_index, min_rows=10000)
    c.equals("按代码 600519", lambda: resolve_quote_id("600519"), "1.600519")
    c.equals("按名称 贵州茅台", lambda: resolve_quote_id("贵州茅台"), "1.600519")
    # NOTE 接口里的名称常带空格填充，取索引里任意一个带空格的名称验证归一化
    spaced = next(
        (
            entry["n"]
            for entries in (index or {}).values()
            for entry in entries
            if " " in entry["n"]
        ),
        None,
    )
    if spaced:
        c.rows(
            f"带空格名称 {spaced!r} 可解析",
            lambda: utils.search_quote_from_index(spaced),
        )
    else:
        c.not_applicable("带空格名称解析", "索引中没有带空格的名称")
    c.equals("同名标的取A股 宁德时代", lambda: resolve_quote_id("宁德时代"), "0.300750")
    c.equals(
        "完整行情ID直接放行 105.AAPL", lambda: resolve_quote_id("105.AAPL"), "105.AAPL"
    )
    # 同一代码跨市场重复（如 000001 既是平安银行又是上证指数）必须保留全部候选，
    # 否则 get_members 拿不到指数那一条
    duplicated = next(
        (code for code, entries in (index or {}).items() if len(entries) > 1), None
    )
    if duplicated:
        c.rows(
            f"同代码多候选 {duplicated}",
            lambda: utils.search_quote_from_index(duplicated),
            min_rows=2,
        )
        secondary = index[duplicated][1]
        c.equals(
            f"按次候选名称查询优先命中该候选 {secondary['n']}",
            lambda: resolve_quote_id(secondary["n"]),
            secondary["q"],
        )
    else:
        # 迁移自旧版缓存的索引只有单候选，多候选分支要到下次完整重建才生效
        c.skip("同代码多候选 2 项", "索引无重复代码，需一次完整重建才会生成多候选")

    print("\n=== 股票（曾因搜索接口失效而崩溃）===")
    c.rows(
        "get_quote_history 单只",
        lambda: ef.stock.get_quote_history("600519", beg="20240101", end="20241231"),
        100,
    )
    c.rows(
        "get_quote_history 按名称",
        lambda: ef.stock.get_quote_history("贵州茅台", beg="20240101", end="20240601"),
    )
    c.rows(
        "get_quote_history 多只",
        lambda: ef.stock.get_quote_history(
            ["600519", "300750"], beg="20240101", end="20240301"
        ),
        2,
    )
    c.rows(
        "get_latest_quote", lambda: ef.stock.get_latest_quote(["600519", "300750"]), 2
    )
    c.rows("get_base_info", lambda: ef.stock.get_base_info("600519"))
    c.rows("get_belong_board", lambda: ef.stock.get_belong_board("600519"))
    c.rows("get_history_bill", lambda: ef.stock.get_history_bill("600519"))
    c.rows("get_members 沪深300", lambda: ef.stock.get_members("沪深300"), 100)
    c.rows("get_quote_snapshot", lambda: ef.stock.get_quote_snapshot("600519"))
    c.rows("get_today_bill", lambda: ef.stock.get_today_bill("600519"))
    c.rows("get_deal_detail", lambda: ef.stock.get_deal_detail("600519", max_count=30))
    c.rows(
        "get_top10_stock_holder_info",
        lambda: ef.stock.get_top10_stock_holder_info("600519"),
    )
    c.rows("get_latest_ipo_info", lambda: ef.stock.get_latest_ipo_info())
    c.rows("get_latest_holder_number", lambda: ef.stock.get_latest_holder_number())
    c.rows("get_daily_billboard", lambda: ef.stock.get_daily_billboard())
    c.rows("get_all_report_dates", lambda: ef.stock.get_all_report_dates())
    c.rows(
        "get_all_company_performance", lambda: ef.stock.get_all_company_performance()
    )

    print("\n=== 债券 / 期货 / 基金 ===")
    # 样本代码取自实时行情表，避免写死可能退市的具体标的
    bonds = c.rows("bond.get_realtime_quotes", lambda: ef.bond.get_realtime_quotes(), 1)
    if bonds is not None:
        bond_code = first_column_value(bonds, "债券代码", "代码")
        c.rows(
            f"bond.get_quote_history {bond_code}",
            lambda: ef.bond.get_quote_history(bond_code),
        )
        c.rows(
            f"bond.get_base_info {bond_code}", lambda: ef.bond.get_base_info(bond_code)
        )
        c.rows(
            f"bond.get_today_bill {bond_code}",
            lambda: ef.bond.get_today_bill(bond_code),
        )
        c.rows(
            f"bond.get_history_bill {bond_code}",
            lambda: ef.bond.get_history_bill(bond_code),
        )
        c.rows(
            f"bond.get_deal_detail {bond_code}",
            lambda: ef.bond.get_deal_detail(bond_code, max_count=30),
        )
    else:
        c.skip("bond 个股接口 5 项", "bond.get_realtime_quotes 未取得样本")
    futures = c.rows(
        "futures.get_realtime_quotes", lambda: ef.futures.get_realtime_quotes(), 1
    )
    if futures is not None:
        futures_id = first_column_value(futures, "行情ID")
        c.rows(
            f"futures.get_quote_history {futures_id}",
            lambda: ef.futures.get_quote_history(futures_id),
        )
        c.rows(
            f"futures.get_deal_detail {futures_id}",
            lambda: ef.futures.get_deal_detail(futures_id, max_count=30),
        )
    else:
        c.skip("futures 个股接口 2 项", "futures.get_realtime_quotes 未取得样本")
    funds = c.rows("fund.get_fund_codes", lambda: ef.fund.get_fund_codes(), 1)
    if funds is not None:
        fund_code = first_column_value(funds, "基金代码", "代码")
        c.rows(
            f"fund.get_quote_history {fund_code}",
            lambda: ef.fund.get_quote_history(fund_code),
        )
        # NOTE fund.get_pdf_reports 未纳入：会往磁盘下载 PDF，有副作用
        c.rows(
            f"fund.get_base_info {fund_code}", lambda: ef.fund.get_base_info(fund_code)
        )
        c.rows(
            f"fund.get_quote_history_multi {fund_code}",
            lambda: ef.fund.get_quote_history_multi([fund_code, "005827"]),
            2,
        )
        c.rows(
            f"fund.get_realtime_increase_rate {fund_code}",
            lambda: ef.fund.get_realtime_increase_rate([fund_code, "005827"]),
            2,
        )
        c.rows(
            f"fund.get_invest_position {fund_code}",
            lambda: ef.fund.get_invest_position(fund_code),
        )
        c.rows(
            f"fund.get_industry_distribution {fund_code}",
            lambda: ef.fund.get_industry_distribution(fund_code),
        )
        c.rows(
            f"fund.get_types_percentage {fund_code}",
            lambda: ef.fund.get_types_percentage(fund_code),
        )
        c.rows(
            f"fund.get_period_change {fund_code}",
            lambda: ef.fund.get_period_change(fund_code),
        )
        c.rows(
            f"fund.get_public_dates {fund_code}",
            lambda: ef.fund.get_public_dates(fund_code),
        )
        c.rows(
            f"fund.get_fund_manager {fund_code}",
            lambda: ef.fund.get_fund_manager(fund_code),
        )
    else:
        c.skip("fund 个股接口 9 项", "fund.get_fund_codes 未取得样本")

    print("\n=== 无需解析的批量接口（防回归）===")
    c.rows("stock.get_realtime_quotes", lambda: ef.stock.get_realtime_quotes(), 1000)
    c.rows(
        "futures.get_futures_base_info", lambda: ef.futures.get_futures_base_info(), 100
    )
    c.rows("bond.get_all_base_info", lambda: ef.bond.get_all_base_info(), 100)

    print("\n=== 降级行为 ===")
    # 美股未纳入索引，绕过索引走远端搜索接口时必须返回空串而不是抛异常
    c.equals(
        "use_local=False 时降级为空串",
        lambda: utils.get_quote_id("AAPL", use_local=False),
        "",
    )

    run_offline_checks(c)

    return finish(c)


if __name__ == "__main__":
    sys.exit(main())
