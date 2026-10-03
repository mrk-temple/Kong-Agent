"""Per-driver measurements; nested phases overlap and must not be added together."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import perf_counter

_active = ContextVar("kong_active_metrics", default=None)
_measuring = ContextVar("kong_measuring", default=frozenset())


@contextmanager
def metrics_scope(metrics):
    token = _active.set(metrics)
    try:
        yield
    finally:
        _active.reset(token)


@contextmanager
def measure(key, metrics=None):
    metrics = metrics if metrics is not None else _active.get()
    marker = (id(metrics), key)
    if metrics is None or marker in _measuring.get():
        yield
        return
    token = _measuring.set(_measuring.get() | {marker})
    started = perf_counter()
    try:
        yield
    finally:
        setattr(metrics, key, getattr(metrics, key) + perf_counter() - started)
        _measuring.reset(token)


def count(key, amount=1):
    metrics = _active.get()
    if metrics is not None:
        setattr(metrics, key, getattr(metrics, key) + amount)


def last_count(key, amount):
    metrics = _active.get()
    if metrics is not None:
        setattr(metrics, key, amount)


def attributed(key):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with measure(key):
                return function(*args, **kwargs)
        return call
    return decorate
