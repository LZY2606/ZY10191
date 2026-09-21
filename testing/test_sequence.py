"""Executable timing/sequence specifications for hook invocation.

Every test in this module is the executable counterpart of one timing table
in ``docs/hook_call_sequence.rst``: the test id (``SCENARIO-xx``) is rendered
next to the table heading so the documented order can be replayed and
regression-tested.

The :class:`EventLog` scaffold below records one logical event per
implementation lifecycle point instead of only comparing final values.  This
lets the tests distinguish "implementation never invoked", "returned None",
"returned an empty container" and "wrapper rewrote the result", which all look
identical if only the hook return value is asserted.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from typing import cast
import warnings

import pytest

from pluggy import HookimplMarker
from pluggy import HookspecMarker
from pluggy import PluginManager
from pluggy import Result


hookspec = HookspecMarker("sequence")
hookimpl = HookimplMarker("sequence")


def _prefix_items(result: object, prefix: str) -> list[str]:
    """Transform helper used by new-wrapper tests: prefix every result item."""
    return [prefix + str(item) for item in cast(Iterable[object], result)]


# --------------------------------------------------------------------------- #
# Event recording scaffold
# --------------------------------------------------------------------------- #
# Event vocabulary (see docs/hook_call_sequence.rst, "Event vocabulary"):
#   ("enter", name)           function/generator entered before the yield
#   ("return", name, value)   plain implementation returned ``value``
#   ("raise", name, etype)    a plain implementation raised ``etype``
#   ("yield", name)           wrapper reached its (single) yield point
#   ("resume", name, kind)    wrapper resumed after yield; kind is one of
#                             "result" / "raise:<ExceptionType>"
#   ("force_result", name, value)  old-style wrapper called force_result()
#   ("force_exception", name, etype)
#                             old-style wrapper called force_exception()
#
# "not called" is not an event: its absence from the log together with the
# final result is the observable contract.
Event = tuple[object, ...]


_UNSET: object = object()


class EventLog:
    """Records lifecycle events of hook implementations.

    Instances are intentionally immutable-looking: implementations append
    events through the narrow helper methods so that the exact set of recorded
    event shapes stays in sync with the documentation tables.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    def enter(self, name: str) -> None:
        self.events.append(("enter", name))

    def return_(self, name: str, value: object) -> None:
        self.events.append(("return", name, value))

    def raise_(self, name: str, exc: BaseException) -> None:
        self.events.append(("raise", name, type(exc).__name__))

    def yield_(self, name: str) -> None:
        self.events.append(("yield", name))

    def resume(self, name: str, outcome: object) -> None:
        if isinstance(outcome, BaseException):
            kind = "raise:" + type(outcome).__name__
        else:
            kind = "result"
        self.events.append(("resume", name, kind))

    def force_result(self, name: str, value: object) -> None:
        self.events.append(("force_result", name, value))

    def force_exception(self, name: str, exc: BaseException) -> None:
        self.events.append(("force_exception", name, type(exc).__name__))

    def assert_exact(self, expected: Sequence[Event]) -> None:
        """Assert the full event stream with a readable diff."""
        assert list(self.events) == list(expected), (
            "event stream mismatch\n"
            f"  expected: {list(expected)}\n"
            f"  actual:   {list(self.events)}"
        )


# --------------------------------------------------------------------------- #
# Plugin builders
# --------------------------------------------------------------------------- #
# All implementations take the event log explicitly (never via closure-shared
# mutable globals) and are registered under explicit, deterministic names so
# the tests neither depend on plugin repr() nor on set iteration order.
def make_plain(
    log: EventLog,
    name: str,
    result: object = None,
    *,
    raises: BaseException | None = None,
    tryfirst: bool = False,
    trylast: bool = False,
    unregister_pm: PluginManager | None = None,
) -> type:
    class PlainPlugin:
        @hookimpl(tryfirst=tryfirst, trylast=trylast)
        def hook(self) -> object:
            log.enter(name)
            if unregister_pm is not None:
                unregister_pm.unregister(self)
            if raises is not None:
                log.raise_(name, raises)
                raise raises
            log.return_(name, result)
            return result

    PlainPlugin.__name__ = f"Plain_{name}"
    return PlainPlugin


def make_new_wrapper(
    log: EventLog,
    name: str,
    *,
    transform: Callable[[object], object] | None = None,
    pre_raises: BaseException | None = None,
    post_raises: BaseException | None = None,
    return_value: object = _UNSET,
    catch_returns: object = _UNSET,
    tryfirst: bool = False,
    trylast: bool = False,
) -> type:
    """New-style wrapper (``wrapper=True``).

    ``transform(result)`` maps the value received at the yield; the return
    value of the generator becomes the hook return value for outer wrappers.
    ``pre_raises`` fails before the yield (inner work never runs);
    ``post_raises`` fails after a successful resume.
    ``catch_returns`` makes the wrapper catch any exception raised at the
    yield (inside the generator) and return the given value.
    """

    class WrapperPlugin:
        @hookimpl(wrapper=True, tryfirst=tryfirst, trylast=trylast)
        def hook(self):
            log.enter(name)
            if pre_raises is not None:
                raise pre_raises
            log.yield_(name)
            try:
                inner = yield
            except BaseException as exc:
                log.resume(name, exc)
                if catch_returns is not _UNSET:
                    return catch_returns
                raise
            log.resume(name, inner)
            if post_raises is not None:
                raise post_raises
            if transform is not None:
                return transform(inner)
            if return_value is not _UNSET:
                return return_value
            return inner

    WrapperPlugin.__name__ = f"NewWrapper_{name}"
    return WrapperPlugin


def make_old_wrapper(
    log: EventLog,
    name: str,
    *,
    force: object = _UNSET,
    force_exc: BaseException | None = None,
    tryfirst: bool = False,
    trylast: bool = False,
) -> type:
    """Old-style wrapper (``hookwrapper=True``).

    The wrapper observes the :class:`Result` and may call ``force_result`` /
    ``force_exception``.  It never returns a value itself; the adapter
    ``run_old_style_hookwrapper`` ends by returning ``result.get_result()``.
    """

    class OldWrapperPlugin:
        @hookimpl(hookwrapper=True, tryfirst=tryfirst, trylast=trylast)
        def hook(self):
            log.enter(name)
            log.yield_(name)
            outcome: Result[object] = yield
            if outcome.exception is not None:
                log.resume(name, outcome.exception)
            else:
                log.resume(name, outcome.get_result())
            if force_exc is not None:
                log.force_exception(name, force_exc)
                outcome.force_exception(force_exc)
            if force is not _UNSET:
                log.force_result(name, force)
                outcome.force_result(force)

    OldWrapperPlugin.__name__ = f"OldWrapper_{name}"
    return OldWrapperPlugin


class SequenceSpecs:
    @hookspec
    def hook(self) -> object: ...


class FirstResultSpecs:
    @hookspec(firstresult=True)
    def hook(self) -> object: ...


class HistoricSpecs:
    @hookspec(historic=True)
    def hook(self, arg: int) -> object: ...


def make_pm(specs: type | None = SequenceSpecs) -> PluginManager:
    pm = PluginManager("sequence")
    if specs is not None:
        pm.add_hookspecs(specs)
    return pm


# --------------------------------------------------------------------------- #
# Table-driven timing cases
# --------------------------------------------------------------------------- #
#
# Registration order is always given explicitly in the test; assertions refer
# to implementations by stable string names, never by repr(plugin) or by set
# iteration order.
def _register_all(pm: PluginManager, plugins: Iterable[tuple[str, object]]) -> None:
    for name, plugin in plugins:
        assert pm.register(plugin, name=name) == name


def test_scenario_01_lifo_plain_order() -> None:
    """SCENARIO-01: plain implementations run LIFO; None is not a result.

    Docs table: "SCENARIO-01 -- plain LIFO and None filtering".
    """
    log = EventLog()
    pm = make_pm()
    _register_all(
        pm,
        [
            ("p_one", make_plain(log, "one", result="r1")()),
            ("p_none", make_plain(log, "none", result=None)()),
            ("p_two", make_plain(log, "two", result="r2")()),
        ],
    )
    assert pm.hook.hook() == ["r2", "r1"]
    log.assert_exact(
        [
            ("enter", "two"),
            ("return", "two", "r2"),
            ("enter", "none"),
            ("return", "none", None),
            ("enter", "one"),
            ("return", "one", "r1"),
        ]
    )


def test_scenario_02_mixed_wrappers_order() -> None:
    """SCENARIO-02: new/old wrappers interleave; setup outer->inner,
    teardown inner->outer; result reduction.

    ``_hookimpls`` layout (HookCaller._add_hookimpl): wrappers occupy a
    contiguous suffix; iteration is reversed in _multicall, so the wrapper
    started first is the OUTERMOST one and is resumed LAST.
    """
    log = EventLog()
    pm = make_pm()
    _register_all(
        pm,
        [
            (
                "w_new",
                make_new_wrapper(
                    log, "Wnew", transform=lambda r: _prefix_items(r, "N:")
                )(),
            ),
            ("w_old", make_old_wrapper(log, "Wold")()),
            ("p_two", make_plain(log, "two", result=2)()),
            ("p_one", make_plain(log, "one", result=1)()),
        ],
    )
    assert pm.hook.hook() == ["N:1", "N:2"]
    log.assert_exact(
        [
            ("enter", "Wold"),
            ("yield", "Wold"),
            ("enter", "Wnew"),
            ("yield", "Wnew"),
            ("enter", "one"),
            ("return", "one", 1),
            ("enter", "two"),
            ("return", "two", 2),
            ("resume", "Wnew", "result"),
            ("resume", "Wold", "result"),
        ]
    )


def test_scenario_03_tryfirst_trylast_layering() -> None:
    """SCENARIO-03: tryfirst/trylast ordering inside and around the
    wrapper split point.

    Layout built by HookCaller._add_hookimpl (in _hookimpls order, iterated
    reversed at call time, see the comment on HookCaller):
        trylast nonwrappers | nonwrappers | tryfirst nonwrappers
        | trylast wrappers | wrappers | tryfirst wrappers
    """
    log = EventLog()
    pm = make_pm()
    _register_all(
        pm,
        [
            ("plain_last", make_plain(log, "plain_last", "L", trylast=True)()),
            ("plain", make_plain(log, "plain", "P")()),
            ("plain_first", make_plain(log, "plain_first", "F", tryfirst=True)()),
            ("wrap_last", make_old_wrapper(log, "wrap_last", trylast=True)()),
            ("wrap", make_new_wrapper(log, "wrap")()),
            ("wrap_first", make_old_wrapper(log, "wrap_first", tryfirst=True)()),
        ],
    )
    assert pm.hook.hook() == ["F", "P", "L"]
    log.assert_exact(
        [
            # wrapper setups, outermost first:
            # tryfirst wrapper is outermost, then plain wrapper, then trylast
            ("enter", "wrap_first"),
            ("yield", "wrap_first"),
            ("enter", "wrap"),
            ("yield", "wrap"),
            ("enter", "wrap_last"),
            ("yield", "wrap_last"),
            # nonwrappers: tryfirst first, default, trylast last
            ("enter", "plain_first"),
            ("return", "plain_first", "F"),
            ("enter", "plain"),
            ("return", "plain", "P"),
            ("enter", "plain_last"),
            ("return", "plain_last", "L"),
            # teardown innermost first
            ("resume", "wrap_last", "result"),
            ("resume", "wrap", "result"),
            ("resume", "wrap_first", "result"),
        ]
    )


def test_scenario_04_firstresult_none_and_empty_container() -> None:
    """SCENARIO-04: firstresult halts at the first non-None result.

    An empty container ``[]`` is a real, non-None result and therefore halts
    the loop just like any other value; only ``None`` is skipped.  Later
    implementations are not entered at all.  Decision branch:
    ``if res is not None: ... if firstresult: break`` in _multicall.
    """
    log = EventLog()
    pm = make_pm(FirstResultSpecs)
    # Registration order defines LIFO; register None-returner first so it is
    # called LAST, and the empty container first among the interesting ones.
    _register_all(
        pm,
        [
            ("p_none", make_plain(log, "returns_none", result=None)()),
            ("p_value", make_plain(log, "returns_value", result="V")()),
            ("p_empty", make_plain(log, "returns_empty", result=[])()),
        ],
    )
    assert pm.hook.hook() == []
    log.assert_exact(
        [
            ("enter", "returns_empty"),
            ("return", "returns_empty", []),
        ]
    )


def test_scenario_04b_firstresult_skips_none_takes_value() -> None:
    """SCENARIO-04b: None is skipped through, first non-None wins."""
    log = EventLog()
    pm = make_pm(FirstResultSpecs)
    _register_all(
        pm,
        [
            ("p_value", make_plain(log, "later_value", result="V")()),
            ("p_none", make_plain(log, "earlier_none", result=None)()),
        ],
    )
    assert pm.hook.hook() == "V"
    log.assert_exact(
        [
            ("enter", "earlier_none"),
            ("return", "earlier_none", None),
            ("enter", "later_value"),
            ("return", "later_value", "V"),
        ]
    )


def test_scenario_05_wrappers_rewrite_result() -> None:
    """SCENARIO-05: new wrapper return value and old wrapper force_result()
    both replace what outer wrappers/the caller observe, and plain results
    are not mutated.

    Reduction branch in _multicall: a wrapper teardown returning via
    StopIteration sets ``result = si.value``; old-style wrappers replace the
    outcome via Result.force_result() inside run_old_style_hookwrapper.
    """
    log = EventLog()
    pm = make_pm()
    _register_all(
        pm,
        [
            # innermost new wrapper maps each item
            (
                "w_new",
                make_new_wrapper(
                    log, "Wnew", transform=lambda r: _prefix_items(r, "m:")
                )(),
            ),
            # middle old wrapper force_result replaces the whole list
            ("w_old", make_old_wrapper(log, "Wold", force=["forced"])()),
            ("p", make_plain(log, "plain", result=1)()),
        ],
    )
    assert pm.hook.hook() == ["forced"]
    log.assert_exact(
        [
            ("enter", "Wold"),
            ("yield", "Wold"),
            ("enter", "Wnew"),
            ("yield", "Wnew"),
            ("enter", "plain"),
            ("return", "plain", 1),
            ("resume", "Wnew", "result"),
            ("resume", "Wold", "result"),
            ("force_result", "Wold", ["forced"]),
        ]
    )


def test_scenario_06_wrapper_raises_before_yield() -> None:
    """SCENARIO-06: exception in wrapper setup (before yield) skips every
    inner implementation and every not-yet-started wrapper.

    Branch: the for-loop in _multicall is wrapped by ``except BaseException``;
    already-started teardowns still run with the exception thrown in.
    """
    log = EventLog()
    pm = make_pm()
    boom = ValueError("before-yield")
    _register_all(
        pm,
        [
            # Registered first => innermost wrapper; it is the one that fails
            # before reaching its yield.
            ("w_failing", make_new_wrapper(log, "Wfail", pre_raises=boom)()),
            # Registered second => OUTER wrapper, already paused at its yield
            # when the inner setup fails, so it still unwinds with the error.
            ("w_outer", make_old_wrapper(log, "Wouter")()),
            # These plain implementations are never entered.
            ("p_inner", make_plain(log, "inner", result=1)()),
            ("p_after", make_plain(log, "after", result=2)()),
        ],
    )
    with pytest.raises(ValueError, match="before-yield"):
        pm.hook.hook()
    log.assert_exact(
        [
            ("enter", "Wouter"),
            ("yield", "Wouter"),
            ("enter", "Wfail"),
            ("resume", "Wouter", "raise:ValueError"),
        ]
    )


def test_scenario_07_plain_impl_raises_baseexception() -> None:
    """SCENARIO-07: a plain impl raising BaseException (not just Exception)
    stops subsequent plain impls; the exception is delivered to wrappers
    inside->out, and an outer new-style wrapper recovers by returning.

    Branch in _multicall: ``except BaseException as exc: exception = exc``
    after the impl loop, then ``teardown.throw(exception)`` while unwinding.
    The wrapper registered first is the INNERMOST one (see SCENARIO-02/03):
    here the innermost new-style wrapper catches the BaseException at its
    yield and returns a value, so the outer old-style wrapper only observes
    the recovered result.  An old-style wrapper itself cannot suppress the
    exception (run_old_style_hookwrapper ends with
    ``return result.get_result()``); only an enclosing new-style wrapper can.
    """
    log = EventLog()
    pm = make_pm()
    boom = KeyboardInterrupt("boom")
    _register_all(
        pm,
        [
            # Registered first => innermost wrapper: catches and recovers.
            ("w_recover", make_new_wrapper(log, "Wrec", catch_returns="recovered")()),
            # Registered second => outermost wrapper: observes the result.
            ("w_observe", make_old_wrapper(log, "Wobs")()),
            # Plain impls in LIFO call order: earlier -> boom -> later(skipped).
            ("p_later", make_plain(log, "later", result=2)()),
            ("p_boom", make_plain(log, "boom", raises=boom)()),
            ("p_earlier", make_plain(log, "earlier", result=0)()),
        ],
    )
    assert pm.hook.hook() == "recovered"
    log.assert_exact(
        [
            ("enter", "Wobs"),
            ("yield", "Wobs"),
            ("enter", "Wrec"),
            ("yield", "Wrec"),
            ("enter", "earlier"),
            ("return", "earlier", 0),
            ("enter", "boom"),
            ("raise", "boom", "KeyboardInterrupt"),
            ("resume", "Wrec", "raise:KeyboardInterrupt"),
            ("resume", "Wobs", "result"),
        ]
    )


def test_scenario_08_wrapper_raises_after_yield() -> None:
    """SCENARIO-08: an exception raised in an inner wrapper's post-yield
    block becomes the outcome seen by outer wrappers.

    Registration order defines nesting: the wrapper registered first is the
    innermost one (HookCaller keeps wrappers in a suffix and _multicall
    iterates reversed), so the failing wrapper is registered before the
    saving wrapper.

    New-style teardown raising propagates out of ``teardown.send()`` into the
    ``except BaseException as e: exception = e`` branch of _multicall's
    unwind loop; the next (outer) teardown gets ``throw(exception)``.  The
    outer old-style wrapper receives it inside run_old_style_hookwrapper at
    ``res = yield`` (turned into a Result carrying the exception) and
    neutralizes it with force_result().
    """
    log = EventLog()
    pm = make_pm()
    teardown_error = ValueError("teardown-fail")
    _register_all(
        pm,
        [
            # Registered first => innermost wrapper; fails after resuming.
            ("w_inner", make_new_wrapper(log, "Winner", post_raises=teardown_error)()),
            # Registered second => outermost wrapper; saves the hook.
            ("w_outer", make_old_wrapper(log, "Wouter", force="saved")()),
            ("p", make_plain(log, "plain", result=1)()),
        ],
    )
    assert pm.hook.hook() == "saved"
    log.assert_exact(
        [
            ("enter", "Wouter"),
            ("yield", "Wouter"),
            ("enter", "Winner"),
            ("yield", "Winner"),
            ("enter", "plain"),
            ("return", "plain", 1),
            ("resume", "Winner", "result"),
            ("resume", "Wouter", "raise:ValueError"),
            ("force_result", "Wouter", "saved"),
        ]
    )


def test_scenario_09_force_exception_then_outer_reads() -> None:
    """SCENARIO-09: an inner old-style wrapper force_exception() replaces
    even a successful plain result; the outer new-style wrapper receives
    the forced exception at its yield and recovers inside the generator.

    Branch: Result.force_exception sets ``_exception`` and clears
    ``_result``; run_old_style_hookwrapper returns ``result.get_result()``
    which raises, and _multicall throws it into the next (outer) teardown.
    """
    log = EventLog()
    pm = make_pm()
    forced = RuntimeError("forced")
    _register_all(
        pm,
        [
            # Registered first => innermost: old-style wrapper forces failure.
            ("w_inner", make_old_wrapper(log, "Winner", force_exc=forced)()),
            # Registered second => outermost: new-style wrapper recovers.
            (
                "w_outer",
                make_new_wrapper(log, "Wouter", catch_returns="outer-recovered")(),
            ),
            ("p", make_plain(log, "plain", result=1)()),
        ],
    )
    assert pm.hook.hook() == "outer-recovered"
    log.assert_exact(
        [
            ("enter", "Wouter"),
            ("yield", "Wouter"),
            ("enter", "Winner"),
            ("yield", "Winner"),
            ("enter", "plain"),
            ("return", "plain", 1),
            ("resume", "Winner", "result"),
            ("force_exception", "Winner", "RuntimeError"),
            ("resume", "Wouter", "raise:RuntimeError"),
        ]
    )


def test_scenario_10_subset_hook_caller() -> None:
    """SCENARIO-10: subset_hook_caller filters _hookimpls per call via the
    _SubsetHookCaller property; the original caller is untouched.

    Branch: ``_SubsetHookCaller._hookimpls`` is recomputed on every access
    (no copied snapshot), so later registrations/unregistrations are visible
    through the subset; removing a non-implementing plugin returns the
    original caller (PluginManager.subset_hook_caller fast path).
    """
    log = EventLog()
    pm = make_pm()
    _register_all(
        pm,
        [
            ("pa", make_plain(log, "A", "a")()),
            ("pb", make_plain(log, "B", "b")()),
            ("pc", make_plain(log, "C", "c")()),
        ],
    )
    plugin_b = pm.get_plugin("pb")
    subset = pm.subset_hook_caller("hook", [plugin_b])
    assert subset is not pm.hook.hook
    assert subset() == ["c", "a"]
    assert pm.hook.hook() == ["c", "b", "a"]
    # Fast path: nothing to remove -> original caller itself.
    assert pm.subset_hook_caller("hook", [object()]) is pm.hook.hook

    # Live view: register another plugin; both callers pick it up.
    _register_all(pm, [("pd", make_plain(log, "D", "d")())])
    assert subset() == ["d", "c", "a"]
    assert pm.hook.hook() == ["d", "c", "b", "a"]


def test_scenario_11_historic_replay() -> None:
    """SCENARIO-11: call_historic memorizes every call; a hookimpl
    registered afterwards is invoked once per memorized call immediately at
    register time, via a single-impl multicall (HookCaller._maybe_apply_history).

    Guarantees asserted (all directly from the code, not thread-safety
    folklore):
      * each call appends one ``(kwargs, result_callback)`` entry to
        HookCaller._call_history;
      * a late impl replays the WHOLE history in insertion order at
        register() time, and cannot return values to the original caller;
      * non-None replay results are fed to result_callback one at a time;
      * wrappers/hookwrappers are rejected for a historic spec at register().
    """
    log: list[tuple[str, int]] = []

    class EarlyImpl:
        @hookimpl
        def hook(self, arg: int) -> str:
            log.append(("early", arg))
            return f"early-{arg}"

    class LateImpl:
        @hookimpl
        def hook(self, arg: int) -> str:
            log.append(("late", arg))
            return f"late-{arg}"

    class Late2Impl:
        @hookimpl
        def hook(self, arg: int) -> str:
            log.append(("late2", arg))
            return f"late2-{arg}"

    pm = make_pm(HistoricSpecs)
    callbacks: list[object] = []
    pm.register(EarlyImpl(), name="early")
    pm.hook.hook.call_historic(kwargs={"arg": 1}, result_callback=callbacks.append)
    pm.hook.hook.call_historic(kwargs={"arg": 2}, result_callback=callbacks.append)

    # The two original calls already delivered early-1/early-2; registering
    # the late impl replays BOTH prior calls at once (history insertion order).
    assert callbacks == ["early-1", "early-2"]
    pm.register(LateImpl(), name="late")
    assert callbacks == ["early-1", "early-2", "late-1", "late-2"]

    # A call made now hits all currently registered impls (LIFO).
    pm.hook.hook.call_historic(kwargs={"arg": 3}, result_callback=callbacks.append)
    assert callbacks == [
        "early-1",
        "early-2",
        "late-1",
        "late-2",
        "late-3",
        "early-3",
    ]

    # Registering yet later replays the entire history in insertion order.
    pm.register(Late2Impl(), name="late2")
    assert callbacks == [
        "early-1",
        "early-2",
        "late-1",
        "late-2",
        "late-3",
        "early-3",
        "late2-1",
        "late2-2",
        "late2-3",
    ]
    assert log == [
        ("early", 1),
        ("early", 2),
        ("late", 1),
        ("late", 2),
        ("late", 3),
        ("early", 3),
        ("late2", 1),
        ("late2", 2),
        ("late2", 3),
    ]


def test_scenario_11_historic_rejects_wrappers() -> None:
    """Historic hooks cannot use wrappers: verified in PluginManager._verify_hook
    at register() time (a historic firstresult spec is rejected at spec mark)."""
    from pluggy._manager import PluginValidationError

    pm_new = make_pm(HistoricSpecs)

    class NewWrap:
        @hookimpl(wrapper=True)
        def hook(self, arg: int):
            yield

    class OldWrap:
        @hookimpl(hookwrapper=True)
        def hook(self, arg: int):
            yield

    with pytest.raises(PluginValidationError, match="historic incompatible"):
        pm_new.register(NewWrap(), name="newwrap")
    with pytest.raises(PluginValidationError, match="historic incompatible"):
        pm_new.register(OldWrap(), name="oldwrap")
    # Nothing was installed.
    assert pm_new.hook.hook.get_hookimpls() == []


def test_scenario_12_dynamic_unregister_uses_snapshot() -> None:
    """SCENARIO-12: unregister() during a call does not change the CURRENT
    invocation; it only affects later calls.

    HookCaller.__call__ passes ``self._hookimpls.copy()`` to _multicall, and
    HookCaller._remove_plugin mutates the list in place.  So an impl that
    unregisters itself still completes this call, but is absent afterwards.
    """
    log = EventLog()
    pm = make_pm()
    _register_all(
        pm,
        [
            ("p_earlier", make_plain(log, "earlier", result="earlier-r")()),
            ("p_self", make_plain(log, "selfunreg", result=None, unregister_pm=pm)()),
            ("p_later", make_plain(log, "later", result="later-r")()),
        ],
    )
    before = len(pm.hook.hook.get_hookimpls())
    # LIFO: later, selfunreg (unregisters itself), earlier -- all still run.
    assert pm.hook.hook() == ["later-r", "earlier-r"]
    log.assert_exact(
        [
            ("enter", "later"),
            ("return", "later", "later-r"),
            ("enter", "selfunreg"),
            # It executed and returned None; None is simply not a result.
            ("return", "selfunreg", None),
            ("enter", "earlier"),
            ("return", "earlier", "earlier-r"),
        ]
    )
    assert len(pm.hook.hook.get_hookimpls()) == before - 1

    # The next call never enters the unregistered implementation.
    log.events.clear()
    assert pm.hook.hook() == ["later-r", "earlier-r"]
    log.assert_exact(
        [
            ("enter", "later"),
            ("return", "later", "later-r"),
            ("enter", "earlier"),
            ("return", "earlier", "earlier-r"),
        ]
    )


def test_scenario_13_signature_trimming() -> None:
    """SCENARIO-13: impls may request a subset of the hookspec arguments;
    _multicall binds ``[caller_kwargs[arg] for arg in hook_impl.argnames]``.

    Parameters with defaults are NOT in argnames (varnames), so they keep
    their default and are never filled from the call; a required parameter
    absent from the call raises HookCallError.
    """
    log: list[tuple[str, object]] = []

    class TrimSpecs:
        @hookspec
        def hook(self, a: int, b: int) -> object: ...

    class Full:
        @hookimpl
        def hook(self, a: int, b: int) -> int:
            log.append(("full", (a, b)))
            return a + b

    class Trimmed:
        @hookimpl
        def hook(self, a: int) -> int:
            log.append(("trimmed", a))
            return a

    class WithDefault:
        @hookimpl
        def hook(self, a: int, b: int = 99) -> int:
            log.append(("default", (a, b)))
            return a

    pm = make_pm(TrimSpecs)
    _register_all(
        pm,
        [("full", Full()), ("trimmed", Trimmed()), ("default", WithDefault())],
    )
    # LIFO: default(a=10, b keeps 99), trimmed(a=10), full(a=10,b=20).
    assert pm.hook.hook(a=10, b=20) == [10, 10, 30]
    assert log == [
        ("default", (10, 99)),
        ("trimmed", 10),
        ("full", (10, 20)),
    ]

    from pluggy import HookCallError

    # All registered impls only need a/b; calling without 'b' hits the
    # _multicall KeyError -> HookCallError binding branch (after a warning
    # from _verify_all_args_are_provided).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(HookCallError, match="argument 'b'"):
            pm.hook.hook(a=1)
