"""Shared query contract for CLI and web; existing Python APIs stay intact."""
from datetime import datetime
import json

import pandas as pd

from .exceptions import ParameterError, SymbolNotFoundError
from .validation import history_parameters

ASSETS = ("stock", "fund", "bond", "futures")


def normalize_query(asset, codes, kind="history", beg=None, end=None, klt=None, fqt=None):
    if asset not in ASSETS or kind not in ("quote", "history"):
        raise ParameterError("unsupported asset or query kind")
    if not isinstance(codes, (list, tuple)) or not codes or any(not isinstance(c, str) or not c.strip() for c in codes):
        raise ParameterError("codes must be a non-empty list of strings")
    codes = list(dict.fromkeys(c.strip() for c in codes))
    if any(len(c) > 100 for c in codes):
        raise ParameterError("code is too long")
    if asset == "fund" and any(len(c) != 6 or not c.isdigit() for c in codes):
        raise ParameterError("fund codes must contain six digits")
    if kind == "quote":
        if any(v is not None for v in (beg, end, klt, fqt)):
            raise ParameterError("quote does not accept dates, intervals or adjustment")
    else:
        if asset == "fund" and (klt is not None or fqt is not None):
            raise ParameterError("fund NAV does not support klt or fqt")
        beg = "19000101" if beg is None else beg
        end = "20500101" if end is None else end
        klt, fqt = (101 if klt is None else klt), (1 if fqt is None else fqt)
        history_parameters(codes[0], beg, end, klt, fqt)
    return dict(asset=asset, codes=codes, kind=kind, beg=beg, end=end, klt=klt, fqt=fqt)


def query_one(code, asset, kind, beg=None, end=None, klt=None, fqt=None):
    from . import bond, fund, futures, stock
    from .common.getter import get_latest_quote
    from .utils import get_quote_id
    if kind == "quote":
        if asset == "fund":
            return fund.get_realtime_increase_rate(code)
        if asset == "stock":
            return stock.get_latest_quote(code)
        quote_id = get_quote_id(code)
        if not quote_id:
            raise SymbolNotFoundError("No quote ID for {}".format(code))
        return get_latest_quote(quote_id)
    if asset == "fund":
        df = fund.get_quote_history(code)
        dates = df["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
        return df.loc[(dates >= beg) & (dates <= end)].reset_index(drop=True)
    if asset == "futures":
        quote_id = get_quote_id(code)
        if not quote_id:
            raise SymbolNotFoundError("No quote ID for {}".format(code))
        code = quote_id
    module = dict(stock=stock, bond=bond, futures=futures)[asset]
    return module.get_quote_history(code, beg=beg, end=end, klt=klt, fqt=fqt)


def metadata(query):
    if query["asset"] == "fund":
        return dict(label="基金估算涨幅（非成交价格）" if query["kind"] == "quote" else "基金历史净值",
                    timezone="Asia/Shanghai", adjustment="不适用", units="净值与涨幅沿用上游口径；估值可能缺失或滞后")
    return dict(label="行情", timezone="交易所当地时间；未额外转换",
                adjustment={0: "不复权", 1: "前复权", 2: "后复权"}.get(query["fqt"], "最新行情"),
                units="成交量、成交额保持上游单位，不跨市场换算；分钟线可获取范围由上游决定")


def table_payload(df, query):
    # pandas handles NaN/Infinity/NaT and numpy scalar types; code columns stay strings.
    frame = df.copy()
    for column in frame.columns:
        if "代码" in str(column) or column == "行情ID":
            frame[column] = frame[column].map(lambda v: None if pd.isna(v) else str(v))
    return dict(columns=list(frame.columns), records=json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False)),
                queried_at=datetime.now().astimezone().isoformat(), metadata=metadata(query))
