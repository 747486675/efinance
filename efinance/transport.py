"""One retry owner and one process-wide, non-bursting request gate."""
import contextlib
import email.utils
import functools
import math
import random
import threading
import time

import requests
from urllib3.exceptions import ReadTimeoutError

from .exceptions import NetworkError, ParameterError, RateLimitError, ResponseError, TimeoutError
from .settings import get_config

_local = threading.local()
_condition = threading.Condition()
_active = 0
_last_start = None
_RETRY_STATUS = (429, 500, 502, 503, 504)


def current_attempts():
    return getattr(_local, "tries", None)


@contextlib.contextmanager
def attempts_override(tries):
    if tries is not None and (isinstance(tries, bool) or not isinstance(tries, int) or tries < 1):
        raise ParameterError("tries must be a positive integer")
    old = getattr(_local, "tries", None)
    _local.tries = tries if tries is not None else old
    try:
        yield
    finally:
        _local.tries = old


def request_attempts(function):
    """Accept a legacy tries kwarg without nesting retry loops."""
    @functools.wraps(function)
    def run(*args, **kwargs):
        tries = kwargs.pop("tries", None)
        with attempts_override(tries):
            return function(*args, **kwargs)
    return run


def inherit_attempts(function):
    """Capture the caller's attempt budget before submitting thread work."""
    tries = current_attempts()

    @functools.wraps(function)
    def run(*args, **kwargs):
        with attempts_override(tries):
            return function(*args, **kwargs)
    return run


@contextlib.contextmanager
def _slot(config):
    global _active, _last_start
    with _condition:
        while True:
            now = time.monotonic()
            delay = 0 if _last_start is None else 1.0 / config["requests_per_second"] - (now - _last_start)
            if _active < config["max_concurrency"] and delay <= 0:
                _active += 1
                _last_start = now
                break
            _condition.wait(timeout=max(0.001, delay) if delay > 0 else None)
    try:
        yield
    finally:
        with _condition:
            _active -= 1
            _condition.notify_all()


def _retry_after(value):
    if not value:
        return 0.0
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except (ValueError, TypeError):
        try:
            return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def _is_timeout(error):
    """Requests wraps response-body ReadTimeoutError in ConnectionError."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (requests.Timeout, ReadTimeoutError)):
            return True
        pending.extend(current.args)
        pending.extend((current.__cause__, current.__context__, getattr(current, "reason", None)))
    return False


def _enforce_response_length(response, **kwargs):
    # urllib3 1.x (used on Python 3.6) does not enforce Content-Length by
    # default. Enable it before Requests or user hooks read the response body.
    if hasattr(response.raw, "enforce_content_length"):
        response.raw.enforce_content_length = True
    return response


class FinanceSession(requests.Session):
    def request(self, method, url, **kwargs):
        config = get_config()
        previous = getattr(_local, "request_config", None)
        _local.request_config = config
        try:
            return self._request(config, method, url, **kwargs)
        finally:
            _local.request_config = previous

    def send(self, request, **kwargs):
        # Release the slot before following redirects: each hop enters its own
        # slot, including when max_concurrency is one. Session.send consumes
        # non-streaming bodies inside the slot and retains Requests' cookies,
        # hooks, proxy handling and redirect method/authentication rules.
        allow_redirects = kwargs.pop("allow_redirects", True)
        config = getattr(_local, "request_config", None) or get_config()
        hooks = request.hooks.setdefault("response", [])
        if _enforce_response_length not in hooks:
            hooks.insert(0, _enforce_response_length)
        with _slot(config):
            response = super(FinanceSession, self).send(
                request, allow_redirects=False, **kwargs)
        if allow_redirects:
            history = list(self.resolve_redirects(response, request, **kwargs))
            if history:
                history.insert(0, response)
                response = history.pop()
                response.history = history
            response._next = None
        return response

    def _request(self, config, method, url, **kwargs):
        tries = kwargs.pop("tries", None)
        if tries is None:
            tries = getattr(_local, "tries", None)
        tries = config["max_attempts"] if tries is None else tries
        if isinstance(tries, bool) or not isinstance(tries, int) or tries < 1:
            raise ParameterError("tries must be a positive integer")
        kwargs.setdefault("timeout", (config["connect_timeout"], config["read_timeout"]))
        for attempt in range(1, tries + 1):
            retry_delay = 0
            try:
                response = super(FinanceSession, self).request(method, url, **kwargs)
                if response.status_code in _RETRY_STATUS:
                    retry_delay = _retry_after(response.headers.get("Retry-After"))
                    kind = RateLimitError if response.status_code == 429 else NetworkError
                    error = kind("Upstream HTTP {}".format(response.status_code), attempts=attempt)
                    try:
                        response.raise_for_status()
                    except requests.HTTPError as cause:
                        error.__cause__ = cause
                    response.close()
                else:
                    try:
                        response.raise_for_status()
                    except requests.HTTPError as cause:
                        response.close()
                        raise ResponseError("Upstream HTTP {}".format(response.status_code), attempts=attempt) from cause
                    # Decode errors are structural errors, never network retries.
                    original_json = response.json
                    def checked_json(*args, **kwargs):
                        try:
                            return original_json(*args, **kwargs)
                        except ValueError as cause:
                            raise ResponseError("Upstream returned invalid JSON", attempts=attempt) from cause
                    response.json = checked_json
                    return response
            except (requests.Timeout, requests.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as cause:
                kind = TimeoutError if _is_timeout(cause) else NetworkError
                error = kind(str(cause), attempts=attempt)
                error.__cause__ = cause
            except (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema) as cause:
                raise ParameterError(str(cause)) from cause
            if attempt == tries:
                raise error
            time.sleep(max(retry_delay, min(30, 2 ** (attempt - 1)) + random.uniform(0, 0.25)))
