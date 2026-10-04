"""Explicit offline fixture profile, separate from the default business entry.

The original tasks, hidden answers, fixture decisions and graders are unchanged.
This adapter only exercises their mechanical control expectations. Default
business rejection remains covered without this adapter in its own regressions.
"""
from kong.evaluation.runner import run_suite
from kong.runtime.loop import Runtime


async def mechanical_fixture_suite(*args, **kwargs):
    if kwargs.get("live"):
        raise ValueError("The mechanical fixture adapter cannot run live model trials")
    original = Runtime.new.__func__
    def explicit(cls, *new_args, **new_kwargs):
        new_kwargs["completion_scope"] = "mechanical"
        return original(cls, *new_args, **new_kwargs)
    Runtime.new = classmethod(explicit)
    try:
        kwargs.setdefault("profile", "offline-fixture-mechanical-rc3")
        return await run_suite(*args, **kwargs)
    finally:
        Runtime.new = classmethod(original)
