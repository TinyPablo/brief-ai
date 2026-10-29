"""A shared time and retry budget for one cloud generation, including tools."""

from contextvars import ContextVar
from functools import wraps
import time

import httpx

GENERATION_SECONDS = 120
_current = ContextVar('provider_budget', default=None)


class ProviderError(Exception):
    def __init__(self, code, detail, status=502):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def describe_error(exc):
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(exc, 'status_code', None) or getattr(exc, 'code', None)
    body = getattr(exc, 'body', None)
    if isinstance(body, dict):
        body = body.get('error', body)
        quota = isinstance(body, dict) and body.get('code') in {'insufficient_quota', 'billing_hard_limit_reached'}
    else:
        quota = False
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or type(exc).__name__ in {'APITimeoutError', 'ReadTimeout', 'ConnectTimeout'} or status in (408, 504):
        return ProviderError('provider_timeout', 'The model did not respond within the time limit. Try again or choose another model.', 504)
    if status == 402 or quota:
        return ProviderError('provider_quota', 'The provider account has no available credits or quota. Check billing before retrying.')
    if status == 429:
        return ProviderError('provider_rate_limit', 'The provider rate limit was reached. Wait briefly before trying again.', 503)
    if status in (500, 502, 503, 529):
        return ProviderError('provider_busy', 'The model provider is temporarily unavailable or overloaded. Try again shortly or choose another model.', 503)
    if status in (401, 403):
        return ProviderError('provider_access', 'The provider rejected access. Check the API key, project permissions and model access.')
    if isinstance(exc, httpx.TransportError) or type(exc).__name__ in {'APIConnectionError', 'ConnectError', 'ReadError'}:
        return ProviderError('provider_connection', 'Could not connect to the model provider. Check the connection and try again.')
    if status in (400, 404, 422):
        return ProviderError('provider_request', 'The provider rejected this request. Check model availability and request compatibility.')
    return ProviderError('api_error', 'The model request failed. Retry the message to continue.')


class Budget:
    def __init__(self):
        self.deadline = time.monotonic() + GENERATION_SECONDS
        self.retry_available = True

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise describe_error(TimeoutError())
        return seconds

    def call(self, send):
        while True:
            seconds = self.remaining()
            try:
                result = send(seconds)
                self.remaining()
                return result
            except Exception as exc:
                error = describe_error(exc)
                # Retry explicit provider rejections only, never ambiguous network timeouts.
                retryable = error.code in {'provider_busy', 'provider_rate_limit'}
                if not retryable or not self.retry_available or self.remaining() <= 2:
                    raise error from exc
                self.retry_available = False
                time.sleep(1)


def bounded(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if _current.get() is not None:
            return function(*args, **kwargs)
        token = _current.set(Budget())
        try:
            return function(*args, **kwargs)
        finally:
            _current.reset(token)
    return wrapper


def request(send):
    return (_current.get() or Budget()).call(send)
