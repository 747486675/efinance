import json
import re
import threading
import time
import warnings
from collections import OrderedDict, namedtuple
from functools import wraps
from typing import Any, Callable, Dict, List, TypeVar, Union

import pandas as pd
import rich
from retry.api import retry

from ..common.config import (
    FS_DICT,
    MARKET_NUMBER_DICT,
    QUOTE_INDEX_EXPIRE,
    QUOTE_INDEX_GROUPS,
    MagicConfig,
    MarketType,
)
from ..config import QUOTE_INDEX_CACHE_PATH, SEARCH_RESULT_CACHE_PATH
from ..shared import SEARCH_RESULT_DICT, session

# 函数变量
F = TypeVar("F")


def to_numeric(func: F) -> F:
    """
    将 DataFrame 或者 Series 尽可能地转为数字的装饰器

    Parameters
    ----------
    func : Callable
        返回结果为 DataFrame 或者 Series 的函数

    Returns
    -------
    Union[DataFrame, Series]

    """

    ignore = [
        "股票代码",
        "基金代码",
        "代码",
        "市场类型",
        "市场编号",
        "债券代码",
        "行情ID",
        "正股代码",
    ]

    @wraps(func)
    def run(*args, **kwargs):
        values = func(*args, **kwargs)
        if isinstance(values, pd.DataFrame):
            for column in values.columns:
                if column not in ignore:

                    values[column] = values[column].apply(convert)
        elif isinstance(values, pd.Series):
            # NOTE pandas 3 起字符串列由 Arrow 承载，逐项写入 float 会抛
            # TypeError，需先退化为 object 再回写
            values = values.astype(object)
            for index in values.index:
                if index not in ignore:

                    values[index] = convert(values[index])
        return values

    def convert(o: Union[str, int, float]) -> Union[str, float, int]:
        if not re.findall(r"\d", str(o)):
            return o
        try:
            if str(o).isalnum():
                o = int(o)
            else:
                o = float(o)
        except:
            pass
        return o

    return run


# 存储证券代码的实体
Quote = namedtuple(
    "Quote",
    [
        "code",
        "name",
        "pinyin",
        "id",
        "jys",
        "classify",
        "market_type",
        "security_typeName",
        "security_type",
        "mkt_num",
        "type_us",
        "quote_id",
        "unified_code",
        "inner_code",
    ],
)

# 行情ID本地索引：代码 -> 候选列表，按 QUOTE_INDEX_GROUPS 顺序排列，
# 每项为 {"q": 行情ID, "n": 名称, "t": security_typeName, "c": classify}
QUOTE_INDEX: Dict[str, List[Dict[str, str]]] = dict()
# 名称 -> 代码 的反向索引
QUOTE_INDEX_BY_NAME: Dict[str, str] = dict()
# 缓存文件格式版本，低于此版本的缓存会被丢弃重建
QUOTE_INDEX_VERSION = 2
# NOTE 多只股票的历史行情会并发调用解析，无锁时每个线程各建一轮索引，
# 会把 7 个分组请求放大成 7×线程数
QUOTE_INDEX_LOCK = threading.Lock()


def _blank_quote(**kwargs) -> Quote:
    """构造只填充了部分字段的 Quote"""
    fields = Quote._fields
    return Quote(**{field: kwargs.get(field, "") for field in fields})


def _index_names() -> None:
    # NOTE 证券名称在接口里常带空格填充（如 '五 粮 液'），索引时去掉
    # setdefault 保证靠前的分组优先，避免同名标的（如 宁德时代 A股/港股）被覆盖
    QUOTE_INDEX_BY_NAME.clear()
    for code, entries in QUOTE_INDEX.items():
        for entry in entries:
            name = re.sub(r"\s+", "", entry.get("n", ""))
            if name:
                QUOTE_INDEX_BY_NAME.setdefault(name, code)


def _entry_to_quote(code: str, entry: Dict[str, str]) -> Quote:
    quote_id = entry["q"]
    market_number = quote_id.split(".")[0]
    return _blank_quote(
        code=code,
        name=entry.get("n", ""),
        quote_id=quote_id,
        security_typeName=entry.get("t", ""),
        classify=entry.get("c", ""),
        mkt_num=market_number,
        jys=MARKET_NUMBER_DICT.get(market_number, ""),
    )


def _read_quote_index_file(allow_expired: bool = False) -> bool:
    """读取磁盘上的行情ID索引，返回是否读取成功

    allow_expired : 是否允许读取已过期索引，供重建失败时兜底使用
    """
    try:
        with open(QUOTE_INDEX_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return False
    expired = time.time() - cache.get("last_time", 0) > QUOTE_INDEX_EXPIRE
    if expired and not allow_expired:
        return False
    data = cache.get("data")
    if not data:
        return False
    QUOTE_INDEX.clear()
    if cache.get("version", 1) < QUOTE_INDEX_VERSION:
        # NOTE 旧版本每代码只存单个候选，包成单元素列表即可继续使用，
        # 避免因升级强制重建而让无网络的环境失去解析能力
        QUOTE_INDEX.update({code: [entry] for code, entry in data.items()})
    else:
        QUOTE_INDEX.update(data)
    _index_names()
    if expired:
        warnings.warn(
            f"行情ID索引重建失败，暂继续使用已过期 {time.time() - cache.get('last_time', 0) / 86400:.1f} 天的旧索引："
            "新上市或改码的标的可能解析不到，网络恢复后会自动重建。",
            RuntimeWarning,
        )
    return True


def _build_quote_index() -> None:
    # 延迟导入以打破 efinance.stock -> efinance.utils 的循环依赖
    from concurrent.futures import ThreadPoolExecutor

    from ..stock.getter import get_realtime_quotes

    def fetch(group):
        market, type_name, classify = group
        try:
            return market, type_name, classify, get_realtime_quotes(fs=market)
        except Exception as e:
            warnings.warn(f"获取 {market} 行情列表失败，已跳过: {e}", RuntimeWarning)
            return market, type_name, classify, None

    index: Dict[str, List[Dict[str, str]]] = dict()
    failed: List[str] = []
    with ThreadPoolExecutor(max_workers=len(QUOTE_INDEX_GROUPS)) as pool:
        # NOTE 必须用有序 map：分组顺序即代码冲突时的优先级
        for market, type_name, classify, df in pool.map(fetch, QUOTE_INDEX_GROUPS):
            if df is None:
                failed.append(market)
                continue
            columns = list(df.columns)
            # NOTE 无论哪个市场，前两列始终是代码和名称
            codes = df[columns[0]].astype(str)
            names = df[columns[1]].astype(str)
            quote_ids = df["行情ID"].astype(str)
            for code, name, quote_id in zip(codes, names, quote_ids):
                if "." not in quote_id:
                    continue
                entries = index.setdefault(code, [])
                # NOTE 同一分组内也可能重复出现同一行情ID，只留一次
                if quote_id not in [entry["q"] for entry in entries]:
                    entries.append(
                        {"q": quote_id, "n": name, "t": type_name, "c": classify}
                    )
    if not index:
        raise Exception("行情ID索引构建失败，请检查网络连接")
    QUOTE_INDEX.clear()
    QUOTE_INDEX.update(index)
    _index_names()
    if failed:
        # NOTE 残缺索引不落盘：否则整个过期周期内都解析不到缺失市场的标的，
        # 且下次运行会直接复用这份坏缓存。仅本次进程内存可用
        warnings.warn(
            f"以下市场获取失败，索引不完整且未写入缓存: {failed}",
            RuntimeWarning,
        )
        return
    with open(QUOTE_INDEX_CACHE_PATH, "w", encoding="utf-8") as f:
        cache = {
            "version": QUOTE_INDEX_VERSION,
            "last_time": time.time(),
            "data": QUOTE_INDEX,
        }
        json.dump(cache, f, ensure_ascii=False)


def load_quote_index(force_reload: bool = False) -> Dict[str, List[Dict[str, str]]]:
    """
    加载代码到行情ID的本地索引，必要时从行情列表重建

    Parameters
    ----------
    force_reload : bool, optional
        是否忽略缓存强制重建, 默认为 ``False``

    Returns
    -------
    Dict[str, List[Dict[str, str]]]
        代码到行情ID等信息的映射，同一代码的多个候选按市场分组顺序排列
    """
    if QUOTE_INDEX and not force_reload:
        return QUOTE_INDEX
    with QUOTE_INDEX_LOCK:
        # 双重检查：等锁期间可能已被其它线程建好
        if QUOTE_INDEX and not force_reload:
            return QUOTE_INDEX
        if not force_reload and _read_quote_index_file():
            return QUOTE_INDEX
        try:
            _build_quote_index()
        except Exception:
            # NOTE 上游按 IP 限流时重建必然失败。此时若缓存文件里还有旧索引，
            # 宁可带着过期告警继续用它，也不要让所有解析退化成完全不可用 ——
            # 缓存一旦过 TTL 就再没有兜底，故障会在某个凌晨静默发生。
            if not _read_quote_index_file(allow_expired=True):
                raise
    return QUOTE_INDEX


def search_quote_from_index(
    keyword: str, market_type: Union[MarketType, None] = None
) -> List[Quote]:
    """
    在本地行情ID索引中按代码或名称精确查找

    Parameters
    ----------
    keyword : str
        证券代码、证券名称或完整行情ID
    market_type : MarketType, optional
        市场类型，索引中无归属信息时不参与筛选

    Returns
    -------
    List[Quote]
        命中的候选，按市场分组顺序排列；未命中时为空列表

    """
    keyword = str(keyword)
    # NOTE 入参可以本身就是行情ID，例如期货主力合约 "115.ZCM"
    market_number, _, bare_code = keyword.partition(".")
    if bare_code and market_number in MARKET_NUMBER_DICT:
        return [
            _blank_quote(
                code=bare_code,
                quote_id=keyword,
                mkt_num=market_number,
                jys=MARKET_NUMBER_DICT[market_number],
            )
        ]
    index = load_quote_index()
    code = QUOTE_INDEX_BY_NAME.get(re.sub(r"\s+", "", keyword))
    if keyword in index:
        code = keyword
    if code is None:
        return []
    entries = index.get(code, [])
    if code != keyword:
        # NOTE 按名称命中时，名称完全匹配的那一条要排在前面，
        # 否则同代码多候选会按分组顺序把 A股 排在被搜的指数之前
        normalized = re.sub(r"\s+", "", keyword)
        entries = sorted(
            entries, key=lambda e: re.sub(r"\s+", "", e.get("n", "")) != normalized
        )
    quotes = []
    for entry in entries:
        classify = entry.get("c", "")
        if market_type is not None and classify and classify != market_type.value:
            continue
        quotes.append(_entry_to_quote(code, entry))
    return quotes


@retry(tries=3, delay=1)
def get_quote_id(
    stock_code: str,
    market_type: Union[MarketType, None] = None,
    use_local=True,
    suppress_error=False,
    **kwargs,
) -> str:
    """
    生成东方财富股票专用的行情ID

    Parameters
    ----------
    stock_code : str
        证券代码或者证券名称
    market_type : MarketType, optional
        市场类型，目前可筛选A股，港股，美股和英股。默认不筛选
    use_local : bool, optional
        是否使用本地缓存
    suppress_error : bool, optional
        遇到错误的股票代码，是否不报错，返回空字符串

    Returns
    -------
    str
        东方财富股票专用的 secid
    """
    if len(str(stock_code).strip()) == 0:
        if suppress_error:
            return ""
        raise Exception("证券代码应为长度不应为 0")
    quote = search_quote(
        stock_code, market_type=market_type, use_local=use_local, **kwargs
    )
    if isinstance(quote, Quote):
        return quote.quote_id
    if quote is None:
        if not suppress_error:
            rich.print(f'证券代码 "{stock_code}" 可能有误')
        return ""


def search_quote(
    keyword: str,
    market_type: Union[MarketType, None] = None,
    count: int = 1,
    use_local: bool = True,
    **kwargs,
) -> Union[Quote, None, List[Quote]]:
    """
    根据关键词搜索以获取证券信息

    Parameters
    ----------
    keyword : str
        搜索词(股票代码、债券代码甚至证券名称都可以)
    market_type : MarketType, optional
        市场类型，目前可筛选A股，港股，美股和英股。默认不筛选
    count : int, optional
        最多搜索结果数, 默认为 `1`
    use_local : bool, optional
        是否使用本地缓存

    Returns
    -------
    Union[Quote, None, List[Quote]]

    """
    # NOTE 本地仅存储第一个搜索结果
    if use_local and count == 1:
        quote = search_quote_locally(keyword, market_type=market_type)
        if quote:
            return quote
    if use_local:
        quotes = search_quote_from_index(keyword, market_type=market_type)
        if quotes:
            return quotes[0] if count == 1 else quotes[:count]
    url = "https://searchapi.eastmoney.com/api/suggest/get"
    params = (
        ("input", f"{keyword}"),
        ("type", "14"),
        ("token", "D43BF722C8E33BDC906FB84D85E326E8"),
        ("count", f"{max(count, 5)}"),
    )
    try:
        json_response = session.get(url, params=params).json()
        items = json_response["QuotationCodeTable"]["Data"]
    except (ValueError, KeyError):
        # NOTE 接口限流或改版时返回的不是 JSON，也可能是 JSON 但缺字段
        warnings.warn(
            "unable to parse search quote result, consider if you are blocked",
            RuntimeWarning,
        )
        return None

    if items is not None and items:
        quotes = [
            Quote(*item.values())
            for item in items
            # 支持精确查找股票代码
            if (
                not kwargs.get(MagicConfig.QUOTE_SYMBOL_MODE, False)
                or (keyword == item["Code"])
            )
            # 支持筛选股票市场
            and (market_type is None or (market_type.value == item["Classify"]))
        ]
        # NOTE 暂时仅存储第一个搜索结果
        save_search_result(keyword, quotes[:1])
        if count == 1:
            return quotes[0] if len(quotes) >= 1 else None

        return quotes[:count]

    return None


def search_quote_locally(
    keyword: str, market_type: Union[MarketType, None] = None
) -> Union[Quote, None]:
    """
    在本地里面使用搜索记录进行关键词搜索

    Parameters
    ----------
    keyword : str
        搜索词
    market_type : MarketType, optional
        市场类型，目前可筛选A股，港股，美股和英股。默认不筛选

    Returns
    -------
    Union[Quote,None]

    """
    q = SEARCH_RESULT_DICT.get(keyword)
    # NOTE 兼容旧版本 给缓存加上最后修改时间
    if (
        q is None
        or not q.get("last_time")
        or (
            isinstance(market_type, MarketType)
            and (q.get("classify")) != (market_type.value)
        )
    ):
        return None

    last_time: float = q["last_time"]
    # 缓存过期秒数
    max_ts = 3600 * 24 * 3
    now = time.time()
    # 缓存过期，在线搜索
    if (now - last_time) > max_ts:
        return None
    # NOTE 一定要拷贝 否则改变源对象
    _q = q.copy()
    # NOTE 一定要删除它 否则会构造错误
    del _q["last_time"]
    quote = Quote(**_q)
    return quote


def save_search_result(keyword: str, quotes: List[Quote]):
    """
    存储搜索结果到文件中

    Parameters
    ----------
    keyword : str
        搜索词
    quotes : List[Quote]
        搜索结果
    """
    with open(SEARCH_RESULT_CACHE_PATH, "w", encoding="utf-8") as f:
        # TODO考虑如何存储多个搜索结果
        for quote in quotes:
            now = time.time()
            d = dict(quote._asdict())
            d["last_time"] = now
            SEARCH_RESULT_DICT[keyword] = d
            break
        json.dump(SEARCH_RESULT_DICT.copy(), f)


def rename_dataframe_and_series(
    fields: dict, to_be_removed: List[str] = [], keep_all: bool = True
):
    """
    重命名 DataFrame 和 Series 的列名的装饰器

    Parameters
    ----------
    fields : dict
        新的表头
    to_be_removed : List[str], optional
        要移除的列, by default []
    keep_all : bool, optional
        是否保存全部列(包含未重命名的列), by default True
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            values = func(*args, **kwargs)
            if isinstance(values, pd.DataFrame):
                columns = list(fields.values())
                if keep_all:
                    for column in values.columns:
                        if column not in columns:
                            columns.append(column)
                    values = values.rename(columns=fields)[columns]
                else:
                    values = values.rename(columns=fields)[columns]
                for column in values:
                    if column in to_be_removed:
                        del values[column]
            elif isinstance(values, pd.Series):
                values = values.rename(fields)

            return values

        return wrapper

    return decorator


def process_dataframe_and_series(
    function_fields: Dict[str, Callable] = dict(),
    remove_columns_and_indexes: List[str] = list(),
):
    """
    对 DataFrame 和 Series 进一步操作

    Parameters
    ----------
    function_fields : Dict[str, Callable], optional
        函数字典
    remove_columns_and_indexes : List[str], optional
        需要删除的行或者列, by default list()
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            values = func(*args, **kwargs)
            if isinstance(values, pd.DataFrame):
                for column, function_name in function_fields.items():
                    if column not in values.columns:
                        continue
                    values[column] = values[column].apply(function_name)
                for column in remove_columns_and_indexes:
                    if column in values.columns:
                        del values[column]
            elif isinstance(values, pd.Series):
                for index in remove_columns_and_indexes:
                    values = values.drop(index)
            return values

        return wrapper

    return decorator


T = TypeVar("T")


def to_type(f: Callable[[str], T], value: Any, default: T = None) -> T:
    """
    类型转换

    Parameters
    ----------
    f : Callable[[str], T]
        转换函数
    value : Any
        待转换的值

    default : T, optional
        转换失败时的返回值, 默认为  ``None`` 表示原样返回

    Returns
    -------
    T
        转换结果
    """
    try:
        value = f(value)
        return value
    except:
        if default is None:
            return value
        return default


def add_market(
    category: str, market_number: str, market_name: str, drop_duplicate: bool = True
) -> None:
    """
    添加市场

    Parameters
    ----------
    category : str
        市场类别
    market_number : str
        市场编号
    market_name : str
        市场名称
    drop_duplicate : bool, optional
        是否去重, 默认为 ``True``
    """
    MARKET_NUMBER_DICT[market_number] = market_name
    old = FS_DICT.get(category, "")
    new = f"{old},m:{market_number}"
    if drop_duplicate:
        FS_DICT[category] = ",".join(OrderedDict.fromkeys(new.split(",")))
    else:
        FS_DICT[category] = new


__all__ = []
