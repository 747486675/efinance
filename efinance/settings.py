"""Process defaults. Running requests retain their original settings."""
import math
import threading

from .exceptions import ParameterError

_lock = threading.Lock()
_config = dict(connect_timeout=10, read_timeout=30, max_attempts=3,
               max_concurrency=8, requests_per_second=2, validation="warn")


def get_config():
    with _lock:
        return dict(_config)


def configure(**kwargs):
    with _lock:
        unknown = set(kwargs) - set(_config)
        if unknown:
            raise ParameterError("Unknown configuration: {}".format(", ".join(sorted(unknown))))
        candidate = dict(_config, **kwargs)
        for key in ("connect_timeout", "read_timeout", "requests_per_second"):
            value = candidate[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ParameterError("{} must be a finite positive number".format(key))
        for key in ("max_attempts", "max_concurrency"):
            value = candidate[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ParameterError("{} must be a positive integer".format(key))
        if candidate["validation"] not in ("off", "warn", "strict"):
            raise ParameterError("validation must be off, warn or strict")
        _config.update(candidate)
        return dict(_config)
