"""Non-destructive checks for public tabular results."""
import functools
import contextlib
import inspect
import math
import warnings
from datetime import datetime

import pandas as pd

from .exceptions import DataQualityError, ParameterError, ResponseError, EfinanceError
from .settings import get_config


def history_parameters(code, beg, end, klt, fqt):
    if not isinstance(code, str) or not code.strip():
        raise ParameterError("code must be a non-empty string (preserve leading zeroes)")
    for value in (beg, end):
        try:
            if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
                raise ValueError()
            datetime.strptime(value, "%Y%m%d")
        except (TypeError, ValueError):
            raise ParameterError("dates must use YYYYMMDD")
    if beg > end:
        raise ParameterError("beg must not be after end")
    if not isinstance(klt, int) or isinstance(klt, bool) or klt not in (1, 5, 15, 30, 60, 101, 102, 103):
        raise ParameterError("unsupported K-line interval")
    if not isinstance(fqt, int) or isinstance(fqt, bool) or fqt not in (0, 1, 2):
        raise ParameterError("fqt must be 0, 1 or 2")


def validate_table(value, required=()):
    if not isinstance(value, (pd.DataFrame, pd.Series)):
        return value
    labels = value.columns if isinstance(value, pd.DataFrame) else value.index
    missing = set(required) - set(labels)
    if missing:
        raise ResponseError("Missing required fields: {}".format(", ".join(sorted(missing))))
    mode = get_config()["validation"]
    if mode == "off" or not isinstance(value, pd.DataFrame) or value.empty:
        return value
    issues = []
    if "日期" in value:
        identity = next((c for c in ("股票代码", "基金代码", "债券代码", "期货代码", "代码") if c in value), None)
        if identity is not None and value[identity].nunique() > 1:
            for _, group in value.groupby(identity, sort=False):
                validate_table(group)
            return value
        dates = pd.to_datetime(value["日期"], errors="coerce")
        if dates.isna().any():
            issues.append("invalid or missing dates")
        if dates.duplicated().any():
            issues.append("duplicate dates")
        if not (dates.is_monotonic_increasing or dates.is_monotonic_decreasing):
            issues.append("dates are out of order")
    if all(column in value for column in ("开盘", "收盘", "最高", "最低")):
        ohlc = value[["开盘", "收盘", "最高", "最低"]].apply(pd.to_numeric, errors="coerce")
        if any(ohlc[column].map(lambda v: pd.isna(v) or not math.isfinite(v)).any()
               for column in ohlc):
            issues.append("missing, nonnumeric or nonfinite OHLC")
        if ((ohlc["最高"] < ohlc[["开盘", "收盘", "最低"]].max(axis=1)) |
                (ohlc["最低"] > ohlc[["开盘", "收盘", "最高"]].min(axis=1))).any():
            issues.append("inconsistent OHLC")
    if "单位净值" in value:
        nav = pd.to_numeric(value["单位净值"], errors="coerce")
        if nav.map(lambda v: pd.isna(v) or not math.isfinite(v)).any():
            issues.append("missing, nonnumeric or nonfinite unit NAV")
        if "累计净值" in value:
            cumulative = value["累计净值"]
            # An upstream placeholder is allowed for cumulative NAV, but a
            # supplied value must be numeric and finite. Daily change is optional.
            supplied = ~cumulative.isna() & ~cumulative.isin(["", "-", "--"])
            numbers = pd.to_numeric(cumulative.loc[supplied], errors="coerce")
            if numbers.map(lambda v: pd.isna(v) or not math.isfinite(v)).any():
                issues.append("nonnumeric or nonfinite cumulative NAV")
    if issues:
        message = "; ".join(issues)
        if mode == "strict":
            raise DataQualityError(message)
        warnings.warn(message, RuntimeWarning, stacklevel=2)
    return value


def response_guard(function):
    """Validate call binding; retain legacy missing-field error translation."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def run(*args, **kwargs):
        try:
            signature.bind(*args, **kwargs)
        except TypeError as cause:
            raise ParameterError(str(cause)) from cause
        try:
            return function(*args, **kwargs)
        except EfinanceError:
            raise
        except (KeyError, IndexError) as cause:
            raise ResponseError("{}: unexpected upstream structure ({})".format(function.__name__, cause)) from cause
    return run


@contextlib.contextmanager
def response_parsing(name):
    """Only parsing blocks may translate TypeError/ValueError to ResponseError."""
    try:
        yield
    except EfinanceError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as cause:
        raise ResponseError("{}: unexpected upstream structure ({})".format(name, cause)) from cause
