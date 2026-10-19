"""Stable exceptions shared by the library, CLI and optional HTTP service."""


class EfinanceError(Exception):
    def __init__(self, message, attempts=1):
        super().__init__(message)
        self.attempts = attempts


class ParameterError(EfinanceError, ValueError):
    pass


class NetworkError(EfinanceError):
    pass


class TimeoutError(NetworkError):
    pass


class RateLimitError(NetworkError):
    pass


class SymbolNotFoundError(EfinanceError):
    pass


class ResponseError(EfinanceError):
    pass


class DataQualityError(EfinanceError):
    pass
