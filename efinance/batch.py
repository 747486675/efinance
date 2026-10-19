"""Bounded, ordered batches; task failures always reach the caller."""
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .exceptions import EfinanceError, ParameterError
from .settings import get_config
from .transport import attempts_override, inherit_attempts


class BatchResult:
    def __init__(self, function, items, kwargs, options):
        self.successes = OrderedDict()
        self.failures = OrderedDict()
        self.cancelled = []
        self._function, self._items = function, items
        self._kwargs, self._options = kwargs, options

    def retry_failed(self, **options):
        retry_options = dict(self._options, **options)
        retried = fetch(self._function, list(self.failures), kwargs=self._kwargs, **retry_options)
        for item, value in retried.successes.items():
            self.successes[item] = value
            self.failures.pop(item, None)
        self.failures.update(retried.failures)
        # A cancelled retry preserves the previous failure so retry_failed can
        # try it again. Terminal states must remain mutually exclusive.
        cancelled = set(self.cancelled + retried.cancelled)
        self.cancelled = [item for item in self._items if item in cancelled
                          and item not in self.successes and item not in self.failures]
        self.successes = OrderedDict((item, self.successes[item]) for item in self._items if item in self.successes)
        self.failures = OrderedDict((item, self.failures[item]) for item in self._items if item in self.failures)
        return self


def fetch(function, items, kwargs=None, max_workers=None, progress=None, cancel_event=None, tries=None):
    if not callable(function) or isinstance(items, (str, bytes)):
        raise ParameterError("fetch requires a callable and an iterable of hashable items")
    try:
        items = list(dict.fromkeys(items))
    except TypeError as cause:
        raise ParameterError("items must be an iterable of hashable values") from cause
    kwargs = dict(kwargs or {})
    workers = get_config()["max_concurrency"] if max_workers is None else max_workers
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ParameterError("max_workers must be a positive integer")
    workers = min(workers, get_config()["max_concurrency"])
    with attempts_override(tries):
        pass
    options = dict(max_workers=workers, progress=progress, cancel_event=cancel_event, tries=tries)
    result = BatchResult(function, items, kwargs, options)
    completed, position = 0, 0

    @inherit_attempts
    def invoke(item):
        with attempts_override(tries):
            return function(item, **kwargs)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        while position < len(items) or pending:
            cancelled = cancel_event is not None and cancel_event.is_set()
            if cancelled:
                result.cancelled.extend(items[position:])
                position = len(items)
            while not cancelled and position < len(items) and len(pending) < workers:
                item = items[position]
                pending[pool.submit(invoke, item)] = item
                position += 1
                cancelled = cancel_event is not None and cancel_event.is_set()
            if not pending:
                continue
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                try:
                    result.successes[item] = future.result()
                except Exception as error:
                    result.failures[item] = dict(item=item, error_type=type(error).__name__,
                                                 message=str(error), attempts=getattr(error, "attempts", 1))
                completed += 1
                if progress is not None:
                    progress(completed, len(items), item)
    result.successes = OrderedDict((item, result.successes[item]) for item in items if item in result.successes)
    result.failures = OrderedDict((item, result.failures[item]) for item in items if item in result.failures)
    return result


def retry_failed(result, **options):
    return result.retry_failed(**options)


def legacy_fetch(function, items, **kwargs):
    result = fetch(function, items, **kwargs)
    if result.failures:
        error = EfinanceError("Batch failed for: {}".format(", ".join(map(str, result.failures))))
        error.result = result
        raise error
    return result.successes
