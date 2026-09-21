"""Executable timing tables for hook registration, invocation and teardown.

Every numbered ``SEQ-xx`` case in this module is rendered as a timing table in
``docs/call_sequence.rst``.  The tests assert the exact ordered event stream,
not only the final value, so the following states stay distinguishable:

- an implementation that was never called (no events at all),
- an implementation that returned ``None`` (a ``return None`` event),
- an implementation that returned an empty container (``return ()``),
- a wrapper that rewrote the result it received (``force_result`` or a
  new-style wrapper returning something other than what was yielded).

Event vocabulary (the prefix is the builder id of the implementation):

====================  ========================================================
event                 meaning
====================  ========================================================
``enter``             function body / wrapper body entered (before ``yield``)
``yield``             wrapper suspended at its single ``yield``
``return <v>``        a plain impl returned ``v``, or a new-style wrapper
                      resumed and returned ``v`` (becoming the outer result)
``raise <E>``         the implementation raised exception type ``E`` there
``resume <v>``        wrapper post-yield block resumed; ``v`` is what the
                      ``yield`` produced: for a new-style wrapper the yielded
                      value, for an old-style wrapper a tagged description of
                      the :class:`~pluggy.Result` state
``force_result``      old-style wrapper called ``Result.force_result``
``force_exception``   old-style wrapper called ``Result.force_exception``
====================  ========================================================
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
from dataclasses import field
import types
from typing import Any

import pytest

from pluggy import HookCallError
from pluggy import HookimplMarker
from pluggy import HookspecMarker
from pluggy import PluggyTeardownRaisedWarning
from pluggy import PluginManager
from pluggy import PluginValidationError


hookspec = HookspecMarker("seq")
hookimpl = HookimplMarker("seq")

HOOK_NAME = "seq_hook"


class SeqError(Exception):
    """Exception used by all scripted failure cases."""


class SeqBaseError(BaseException):
    """A non-``Exception`` error, used to prove ``BaseException`` propagation."""


class EventLog:
    """Ordered, append-only recorder shared by all implementations of a case."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def _add(self, label: str, event: str, detail: str | None = None) -> None:
        if detail is None:
            self.events.append(f"{label}: {event}")
        else:
            self.events.append(f"{label}: {event} {detail}")

    def enter(self, label: str) -> None:
        self._add(label, "enter")

    def yield_event(self, label: str) -> None:
        self._add(label, "yield")

    def returned(self, label: str, value: object) -> None:
        self._add(label, "return", _value_tag(value))

    def raised(self, label: str, exc: BaseException) -> None:
        self._add(label, "raise", type(exc).__name__)

    def resumed(self, label: str, value: object) -> None:
        self._add(label, "resume", _value_tag(value))

    def force_result_event(self, label: str) -> None:
        self._add(label, "force_result")

    def force_exception_event(self, label: str) -> None:
        self._add(label, "force_exception")


def _value_tag(value: object) -> str:
    if value is None:
        return "None"
    return repr(value)


# --------------------------------------------------------------------------- #
# Scripted implementation builders
# --------------------------------------------------------------------------- #
#
# Implementations are generated from small source templates so that their
# declared signatures, and therefore ``HookImpl.argnames`` computed by
# ``varnames``, are under case control.  That exercises the argument binding
# branch in ``_multicall``, which binds only the subset of caller kwargs named
# in ``hook_impl.argnames``.
# --------------------------------------------------------------------------- #


def _compile(source: str, env: dict[str, Any]) -> Callable[..., object]:
    ns: dict[str, Any] = {
        "log": env["log"],
        "hookimpl": hookimpl,
        "SeqError": SeqError,
        "SeqBaseError": SeqBaseError,
    }
    exec(compile(source, "<seq-builder>", "exec"), ns)  # noqa: S102
    return ns[HOOK_NAME]  # type: ignore[no-any-return]


def _signature(signature_extra: str | None) -> str:
    if signature_extra is None:
        return "(x)"
    return "(x" + (f", {signature_extra}" if signature_extra else "") + ")"


_PLAIN_TEMPLATE = """
@hookimpl
def seq_hook{sig}:
    log.enter({label!r})
    {body}
"""

_NEW_TEMPLATE = """
@hookimpl(wrapper=True)
def seq_hook{sig}:
    log.enter({label!r})
    {setup}
    log.yield_event({label!r})
    outcome = yield
    log.resumed({label!r}, outcome)
    {teardown}
"""

_OLD_TEMPLATE = """
@hookimpl(hookwrapper=True)
def seq_hook{sig}:
    log.enter({label!r})
    {setup}
    log.yield_event({label!r})
    result = yield
    if result.exception is None:
        log.resumed({label!r}, ("result", result._result))
    else:
        log.resumed({label!r}, ("exception", type(result.exception).__name__))
    {teardown}
"""


def _apply_order(
    function: Callable[..., object], *, tryfirst: bool, trylast: bool
) -> None:
    opts = function.seq_impl  # type: ignore[attr-defined]
    if tryfirst:
        opts["tryfirst"] = True
    if trylast:
        opts["trylast"] = True


def plain(
    log: EventLog,
    label: str,
    *,
    mode: str = "value",
    value: object = None,
    signature_extra: str | None = None,
    tryfirst: bool = False,
    trylast: bool = False,
) -> Callable[..., object]:
    """Build a normal (non-wrapper) implementation.

    Modes:
      - ``value``: return ``value``,
      - ``echo``: return ``(label, x)``,
      - ``raise``: raise :class:`SeqError`,
      - ``raise_base``: raise :class:`SeqBaseError` (a ``BaseException``).
    """
    if mode == "value":
        body = f"log.returned({label!r}, {value!r})\n    return {value!r}"
    elif mode == "echo":
        body = (
            f"value = ({label!r}, x)\n"
            f"    log.returned({label!r}, value)\n"
            "    return value"
        )
    elif mode == "raise":
        body = f"log.raised({label!r}, SeqError())\n    raise SeqError({label!r})"
    elif mode == "raise_base":
        body = (
            f"log.raised({label!r}, SeqBaseError())\n    raise SeqBaseError({label!r})"
        )
    else:  # pragma: no cover - error in the case table, not pluggy
        raise AssertionError(f"unknown plain mode {mode!r}")
    source = _PLAIN_TEMPLATE.format(
        sig=_signature(signature_extra), label=label, body=body
    )
    function = _compile(source, {"log": log})
    _apply_order(function, tryfirst=tryfirst, trylast=trylast)
    return function


def new_wrapper(
    log: EventLog,
    label: str,
    *,
    mode: str = "pass",
    value: object = None,
    signature_extra: str | None = None,
    tryfirst: bool = False,
    trylast: bool = False,
) -> Callable[..., object]:
    """Build a new-style (``wrapper=True``) generator implementation.

    Modes:
      - ``pass``: return the yielded value unchanged,
      - ``return``: return ``value`` instead of the yielded value,
      - ``raise_before``: raise before reaching ``yield``,
      - ``raise_after``: resume then raise,
      - ``reraise``: re-raise the exception thrown into ``yield``,
      - ``catch``: swallow a thrown exception and return ``value``.
    """
    setup = (
        f"log.raised({label!r}, SeqError())\n    raise SeqError({label!r})"
        if mode == "raise_before"
        else ""
    )
    if mode == "pass":
        teardown = f"log.returned({label!r}, outcome)\n    return outcome"
    elif mode in ("return", "catch"):
        teardown = f"log.returned({label!r}, {value!r})\n    return {value!r}"
    elif mode == "raise_after":
        teardown = f"log.raised({label!r}, SeqError())\n    raise SeqError({label!r})"
    elif mode in ("reraise", "raise_before"):
        teardown = "raise"
    else:  # pragma: no cover - error in the case table, not pluggy
        raise AssertionError(f"unknown new-wrapper mode {mode!r}")
    source = _NEW_TEMPLATE.format(
        sig=_signature(signature_extra),
        label=label,
        setup=setup,
        teardown=teardown,
    )
    function = _compile(source, {"log": log})
    _apply_order(function, tryfirst=tryfirst, trylast=trylast)
    return function


def old_wrapper(
    log: EventLog,
    label: str,
    *,
    mode: str = "pass",
    value: object = None,
    signature_extra: str | None = None,
    tryfirst: bool = False,
    trylast: bool = False,
) -> Callable[..., object]:
    """Build an old-style (``hookwrapper=True``) generator implementation.

    Modes:
      - ``pass``: leave the :class:`~pluggy.Result` untouched,
      - ``force_result``: call ``Result.force_result(value)``,
      - ``force_exception``: call ``Result.force_exception(SeqError())``,
      - ``raise_before``: raise before reaching ``yield``,
      - ``raise_after``: resume then raise (warns, then propagates),
      - ``get_result``: call ``get_result()`` and swallow the exception,
        proving an outer wrapper can read a forced exception without changing
        it.
    """
    setup = (
        f"log.raised({label!r}, SeqError())\n    raise SeqError({label!r})"
        if mode == "raise_before"
        else ""
    )
    if mode == "pass":
        teardown = ""
    elif mode == "force_result":
        teardown = (
            f"log.force_result_event({label!r})\n    result.force_result({value!r})"
        )
    elif mode == "force_exception":
        teardown = (
            f"log.force_exception_event({label!r})\n"
            f"    result.force_exception(SeqError({label!r}))"
        )
    elif mode == "raise_after":
        teardown = f"log.raised({label!r}, SeqError())\n    raise SeqError({label!r})"
    elif mode == "get_result":
        teardown = (
            "try:\n"
            "        result.get_result()\n"
            "    except BaseException as exc:\n"
            f'        log.resumed({label!r}, ("caught", type(exc).__name__))'
        )
    elif mode == "raise_before":
        teardown = "raise"
    else:  # pragma: no cover - error in the case table, not pluggy
        raise AssertionError(f"unknown old-wrapper mode {mode!r}")
    source = _OLD_TEMPLATE.format(
        sig=_signature(signature_extra),
        label=label,
        setup=setup,
        teardown=teardown,
    )
    function = _compile(source, {"log": log})
    _apply_order(function, tryfirst=tryfirst, trylast=trylast)
    return function


_BUILDERS = {"plain": plain, "new": new_wrapper, "old": old_wrapper}


@dataclass
class Impl:
    """One row in a table-driven case: a builder kind and its arguments."""

    kind: str
    label: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    def build(self, log: EventLog) -> Callable[..., object]:
        return _BUILDERS[self.kind](log, self.label, **self.kwargs)


@dataclass
class Case:
    """A table-driven case corresponding to one docs timing table."""

    seq_id: str
    impls: list[Impl]
    expected_events: list[str]
    expected: object = None
    firstresult: bool = False
    call_kwargs: dict[str, Any] = field(default_factory=lambda: {"x": 1, "y": 0})
    raises: type[BaseException] | None = None
    exception_label: str | None = None
    warns_teardown: bool = False
    no_spec: bool = False


def _make_spec_module(firstresult: bool) -> types.ModuleType:
    spec_module = types.ModuleType("seq_spec")

    spec_function: Callable[..., object]
    if firstresult:

        @hookspec(firstresult=True)
        def firstresult_hook(x: object, y: object = None) -> object: ...

        spec_function = firstresult_hook
    else:

        @hookspec
        def normal_hook(x: object, y: object, z: object = None) -> object: ...

        spec_function = normal_hook

    spec_function.__name__ = HOOK_NAME
    setattr(spec_module, HOOK_NAME, spec_function)
    return spec_module


def _make_pm(case: Case) -> PluginManager:
    pm = PluginManager("seq")
    if case.no_spec:
        return pm
    pm.add_hookspecs(_make_spec_module(case.firstresult))
    return pm


def _register(case: Case, pm: PluginManager, log: EventLog) -> None:
    for row in case.impls:
        plugin = types.SimpleNamespace(**{HOOK_NAME: row.build(log)})
        pm.register(plugin, name=row.label)


def _failure_message(case: Case, log: EventLog, extra: str = "") -> str:
    return (
        f"[{case.seq_id}] {extra}\n"
        "expected event stream:\n  "
        + "\n  ".join(case.expected_events)
        + "\nactual event stream:\n  "
        + "\n  ".join(log.events)
    )


def _run(case: Case) -> EventLog:
    pm = _make_pm(case)
    log = EventLog()
    _register(case, pm, log)

    catches: Any = (
        pytest.warns(PluggyTeardownRaisedWarning)
        if case.warns_teardown
        else contextlib.nullcontext()
    )
    with catches:
        if case.raises is not None:
            with pytest.raises(case.raises) as excinfo:
                pm.hook.seq_hook(**case.call_kwargs)
            assert log.events == case.expected_events, _failure_message(
                case, log, f"while raising {case.raises.__name__}"
            )
            # The implementation label is carried as the exception message,
            # pinning down exactly which raise reached the hook caller.
            if case.exception_label is not None:
                assert excinfo.value.args == (case.exception_label,), _failure_message(
                    case,
                    log,
                    f"exception label {excinfo.value.args!r} != "
                    f"{(case.exception_label,)!r}",
                )
        else:
            outcome = pm.hook.seq_hook(**case.call_kwargs)
            assert outcome == case.expected, _failure_message(
                case, log, f"final value {outcome!r} != {case.expected!r}"
            )
            assert log.events == case.expected_events, _failure_message(case, log)
    return log


# --------------------------------------------------------------------------- #
# Table-driven cases (each one is a timing table in docs/call_sequence.rst)
# --------------------------------------------------------------------------- #


def P(label: str, **kwargs: Any) -> Impl:
    return Impl("plain", label, kwargs)


def N(label: str, **kwargs: Any) -> Impl:
    return Impl("new", label, kwargs)


def OW(label: str, **kwargs: Any) -> Impl:
    return Impl("old", label, kwargs)


CASES: list[Case] = [
    # SEQ-01: new- and old-style wrappers interleaved with plain impls.
    # Setup order is the reverse of the final _hookimpls list; teardowns run
    # in the reverse of setup order (inner new-style resumes before the outer
    # old-style adapter).
    Case(
        "SEQ-01",
        [P("p1", value=1), N("wn1"), P("p2", value=2), OW("wo1"), P("p3", value=3)],
        [
            "wo1: enter",
            "wo1: yield",
            "wn1: enter",
            "wn1: yield",
            "p3: enter",
            "p3: return 3",
            "p2: enter",
            "p2: return 2",
            "p1: enter",
            "p1: return 1",
            "wn1: resume [3, 2, 1]",
            "wn1: return [3, 2, 1]",
            "wo1: resume ('result', [3, 2, 1])",
        ],
        expected=[3, 2, 1],
    ),
    # SEQ-02: None results are dropped, an empty container is kept.  The
    # ``return None`` event still proves the implementation was called.
    Case(
        "SEQ-02",
        [P("p_none", value=None), P("p_empty", value=()), P("p_v", value="v")],
        [
            "p_v: enter",
            "p_v: return 'v'",
            "p_empty: enter",
            "p_empty: return ()",
            "p_none: enter",
            "p_none: return None",
        ],
        expected=["v", ()],
    ),
    # SEQ-03A: firstresult halts at the first non-None result; earlier (in
    # invocation order) Nones do not halt, later impls are never called.
    Case(
        "SEQ-03A",
        [P("f1", value=None), P("f2", value=None), P("f3", value="hit")],
        [
            "f3: enter",
            "f3: return 'hit'",
        ],
        expected="hit",
        firstresult=True,
    ),
    # SEQ-03B: an empty container is a non-None first result and halts the
    # loop; wrappers still run and receive the single scalar.
    Case(
        "SEQ-03B",
        [P("p2", value="second"), P("p1", value=()), N("w")],
        [
            "w: enter",
            "w: yield",
            "p1: enter",
            "p1: return ()",
            "w: resume ()",
            "w: return ()",
        ],
        expected=(),
        firstresult=True,
    ),
    # SEQ-08: a wrapper raising before ``yield`` aborts setup; it is never
    # suspended so no teardown runs, and no plain impl is entered.
    Case(
        "SEQ-08",
        [P("p1", value=1), N("wb", mode="raise_before")],
        [
            "wb: enter",
            "wb: raise SeqError",
        ],
        raises=SeqError,
        exception_label="wb",
    ),
    # SEQ-08B: an old-style wrapper raising before ``yield`` aborts setup the
    # same way; its adapter generator is discarded without a teardown.
    Case(
        "SEQ-08B",
        [P("p1", value=1), OW("wb", mode="raise_before")],
        [
            "wb: enter",
            "wb: raise SeqError",
        ],
        raises=SeqError,
        exception_label="wb",
    ),
    # SEQ-09: a plain impl raises a BaseException; entered wrappers unwind
    # inner-to-outer via ``throw()``.  A re-raising new-style wrapper emits no
    # ``resume`` (the throw replaces the yield value); the old-style wrapper
    # observes the exception through its Result and does not swallow it.
    Case(
        "SEQ-09",
        [
            P("p", mode="raise_base"),
            N("wn", mode="reraise"),
            OW("wo"),
        ],
        [
            "wo: enter",
            "wo: yield",
            "wn: enter",
            "wn: yield",
            "p: enter",
            "p: raise SeqBaseError",
            "wo: resume ('exception', 'SeqBaseError')",
        ],
        raises=SeqBaseError,
        exception_label="p",
    ),
    # SEQ-10: an inner old-style wrapper forces an exception; an outer
    # old-style wrapper reads it with get_result() (logged as "caught")
    # without clearing it, so the hook still raises the inner exception.
    Case(
        "SEQ-10",
        [
            P("p", value=1),
            OW("wi", mode="force_exception"),
            OW("wo", mode="get_result"),
        ],
        [
            "wo: enter",
            "wo: yield",
            "wi: enter",
            "wi: yield",
            "p: enter",
            "p: return 1",
            "wi: resume ('result', [1])",
            "wi: force_exception",
            "wo: resume ('exception', 'SeqError')",
            "wo: resume ('caught', 'SeqError')",
        ],
        raises=SeqError,
        exception_label="wi",
    ),
    # SEQ-10B: an old-style wrapper uses force_result; the outer new-style
    # wrapper receives the forced value (not the plain impls' list) and
    # returns it.
    Case(
        "SEQ-10B",
        [
            P("p1", value=1),
            P("p2", value=2),
            OW("wo", mode="force_result", value=[9]),
            N("wn"),
        ],
        [
            "wn: enter",
            "wn: yield",
            "wo: enter",
            "wo: yield",
            "p2: enter",
            "p2: return 2",
            "p1: enter",
            "p1: return 1",
            "wo: resume ('result', [2, 1])",
            "wo: force_result",
            "wn: resume [9]",
            "wn: return [9]",
        ],
        expected=[9],
    ),
    # SEQ-11: tryfirst/trylast only reorder inside the nonwrapper and
    # wrapper sections; invocation reverses the resulting list and teardowns
    # unwind in the opposite direction.
    Case(
        "SEQ-11",
        [
            P("pL", mode="echo", trylast=True),
            P("p", mode="echo"),
            P("pF", mode="echo", tryfirst=True),
            N("wl", mode="pass", trylast=True),
            N("w", mode="pass"),
            N("wf", mode="pass", tryfirst=True),
        ],
        [
            "wf: enter",
            "wf: yield",
            "w: enter",
            "w: yield",
            "wl: enter",
            "wl: yield",
            "pF: enter",
            "pF: return ('pF', 1)",
            "p: enter",
            "p: return ('p', 1)",
            "pL: enter",
            "pL: return ('pL', 1)",
            "wl: resume [('pF', 1), ('p', 1), ('pL', 1)]",
            "wl: return [('pF', 1), ('p', 1), ('pL', 1)]",
            "w: resume [('pF', 1), ('p', 1), ('pL', 1)]",
            "w: return [('pF', 1), ('p', 1), ('pL', 1)]",
            "wf: resume [('pF', 1), ('p', 1), ('pL', 1)]",
            "wf: return [('pF', 1), ('p', 1), ('pL', 1)]",
        ],
        expected=[("pF", 1), ("p", 1), ("pL", 1)],
    ),
    # SEQ-13A: an old-style wrapper raising after resume emits
    # PluggyTeardownRaisedWarning; the exception still reaches the outer
    # wrapper and the hook caller.
    Case(
        "SEQ-13A",
        [P("p", value=1), OW("wa", mode="raise_after"), OW("wo")],
        [
            "wo: enter",
            "wo: yield",
            "wa: enter",
            "wa: yield",
            "p: enter",
            "p: return 1",
            "wa: resume ('result', [1])",
            "wa: raise SeqError",
            "wo: resume ('exception', 'SeqError')",
        ],
        raises=SeqError,
        exception_label="wa",
        warns_teardown=True,
    ),
    # SEQ-13B: a new-style wrapper raising after resume propagates directly
    # with no PluggyTeardownRaisedWarning.
    Case(
        "SEQ-13B",
        [P("p", value=1), N("w", mode="raise_after")],
        [
            "w: enter",
            "w: yield",
            "p: enter",
            "p: return 1",
            "w: resume [1]",
            "w: raise SeqError",
        ],
        raises=SeqError,
        exception_label="w",
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.seq_id for c in CASES])
def test_table_driven_sequence(case: Case) -> None:
    _run(case)


# --------------------------------------------------------------------------- #
# SEQ-04: subset_hook_caller filters by plugin object at invocation time
# --------------------------------------------------------------------------- #


def test_seq_04_subset_hook_caller() -> None:
    # SEQ-04
    pm = _make_pm(Case("SEQ-04", [], []))
    log = EventLog()

    class Plugin:
        def __init__(self, label: str) -> None:
            setattr(self, HOOK_NAME, plain(log, label, mode="echo"))

    plugin_a = Plugin("a")
    plugin_b = Plugin("b")
    plugin_c = Plugin("c")
    pm.register(plugin_a, name="a")
    pm.register(plugin_b, name="b")
    pm.register(plugin_c, name="c")

    subset = pm.subset_hook_caller(HOOK_NAME, remove_plugins=[plugin_b])

    # The subset excludes b while keeping the caller's LIFO order a, c.
    assert subset(x=1, y=0) == [("c", 1), ("a", 1)]
    assert log.events == [
        "c: enter",
        "c: return ('c', 1)",
        "a: enter",
        "a: return ('a', 1)",
    ], _failure_message(
        Case(
            "SEQ-04",
            [],
            [
                "c: enter",
                "c: return ('c', 1)",
                "a: enter",
                "a: return ('a', 1)",
            ],
        ),
        log,
        "subset call",
    )

    # The original HookCaller is unchanged: b is still called.
    log.events.clear()
    assert pm.hook.seq_hook(x=2, y=0) == [("c", 2), ("b", 2), ("a", 2)]
    assert log.events == [
        "c: enter",
        "c: return ('c', 2)",
        "b: enter",
        "b: return ('b', 2)",
        "a: enter",
        "a: return ('a', 2)",
    ], "subset must proxy, not copy, the underlying _hookimpls list"

    # The subset is a live proxy: a plugin registered afterwards takes part.
    plugin_d = Plugin("d")
    pm.register(plugin_d, name="d")
    log.events.clear()
    assert subset(x=3, y=0) == [("d", 3), ("c", 3), ("a", 3)]
    assert [event for event in log.events if event.endswith("enter")] == [
        "d: enter",
        "c: enter",
        "a: enter",
    ], "b must stay excluded while d joins through the shared proxy"


# --------------------------------------------------------------------------- #
# SEQ-05: historic calls replay past calls on late registrations
# --------------------------------------------------------------------------- #


def _make_historic_pm() -> PluginManager:
    pm = PluginManager("seq")

    class HistoricSpec:
        @hookspec(historic=True)
        def seq_hook(self, x: object) -> object: ...

    pm.add_hookspecs(HistoricSpec)
    return pm


def test_seq_05a_historic_replay_on_late_registration() -> None:
    # SEQ-05A
    pm = _make_historic_pm()
    log = EventLog()
    callback_results: list[object] = []
    pm.hook.seq_hook.call_historic(
        kwargs={"x": "first"}, result_callback=callback_results.append
    )
    assert callback_results == []

    late = types.SimpleNamespace(**{HOOK_NAME: plain(log, "late", mode="echo")})
    pm.register(late, name="late")  # triggers _maybe_apply_history replay
    assert callback_results == [("late", "first")]
    assert log.events == [
        "late: enter",
        "late: return ('late', 'first')",
    ], "the late impl receives the recorded kwargs, not the current ones"


def test_seq_05b_historic_none_vs_empty_containers() -> None:
    # SEQ-05B
    """None is the only value dropped by result reduction.  Both ``[]`` and
    ``()`` are real, non-None results: the single-impl replay wraps them in a
    one-element list, which is truthy, so both are delivered (``[]`` as the
    element ``res[0]``, not as the wrapper list)."""
    pm = _make_historic_pm()
    log = EventLog()
    delivered: list[object] = []
    pm.hook.seq_hook.call_historic(kwargs={"x": 1}, result_callback=delivered.append)

    pm.register(
        types.SimpleNamespace(**{HOOK_NAME: plain(log, "none", value=None)}),
        name="none",
    )
    pm.register(
        types.SimpleNamespace(**{HOOK_NAME: plain(log, "empty_list", value=[])}),
        name="empty_list",
    )
    pm.register(
        types.SimpleNamespace(**{HOOK_NAME: plain(log, "empty_tuple", value=())}),
        name="empty_tuple",
    )

    # None: _multicall appends nothing -> the replay wrapper list is empty and
    # falsy, so the callback is skipped.
    # [] and (): each yields a one-element (truthy) wrapper list and is
    # delivered verbatim as element 0.
    assert delivered == [[], ()]
    assert [event for event in log.events if "enter" in event] == [
        "none: enter",
        "empty_list: enter",
        "empty_tuple: enter",
    ], "all three impls were replayed; only None produced no delivery"


def test_seq_05c_historic_replays_every_recorded_call() -> None:
    # SEQ-05C
    pm = _make_historic_pm()
    log = EventLog()
    pm.hook.seq_hook.call_historic(kwargs={"x": 1})
    pm.hook.seq_hook.call_historic(kwargs={"x": 2})

    pm.register(
        types.SimpleNamespace(**{HOOK_NAME: plain(log, "late", mode="echo")}),
        name="late",
    )
    assert log.events == [
        "late: enter",
        "late: return ('late', 1)",
        "late: enter",
        "late: return ('late', 2)",
    ], "_maybe_apply_history iterates the whole _call_history in order"


def test_seq_05d_historic_rejects_wrappers() -> None:
    # SEQ-05D
    pm = _make_historic_pm()
    log = EventLog()
    pm.hook.seq_hook.call_historic(kwargs={"x": 1})
    with pytest.raises(PluginValidationError, match="historic incompatible"):
        pm.register(
            types.SimpleNamespace(**{HOOK_NAME: new_wrapper(log, "w")}),
            name="w",
        )
    with pytest.raises(PluginValidationError, match="historic incompatible"):
        pm.register(
            types.SimpleNamespace(**{HOOK_NAME: old_wrapper(log, "o")}),
            name="o",
        )
    assert log.events == [], "a rejected wrapper never executes, even as replay"


def test_seq_05e_historic_initial_call_uses_snapshot_then_replays_later() -> None:
    # SEQ-05E
    """A historic call made after some registrations calls current impls, and
    each later registration replays only to that new impl."""
    pm = _make_historic_pm()
    log = EventLog()
    pm.register(
        types.SimpleNamespace(**{HOOK_NAME: plain(log, "early", mode="echo")}),
        name="early",
    )
    pm.hook.seq_hook.call_historic(kwargs={"x": "h"})
    assert log.events == [
        "early: enter",
        "early: return ('early', 'h')",
    ]
    log.events.clear()
    pm.register(
        types.SimpleNamespace(**{HOOK_NAME: plain(log, "late", mode="echo")}),
        name="late",
    )
    assert log.events == [
        "late: enter",
        "late: return ('late', 'h')",
    ], "replay is scoped to [method], the early impl is not re-invoked"


# --------------------------------------------------------------------------- #
# SEQ-07: unregister during a call cannot change the in-flight invocation
# --------------------------------------------------------------------------- #


def test_seq_07_unregister_during_call_uses_snapshot() -> None:
    # SEQ-07
    pm = _make_pm(Case("SEQ-07", [], []))
    log = EventLog()

    plugin_b = types.SimpleNamespace(**{HOOK_NAME: plain(log, "b", value="b")})
    plugin_a = types.SimpleNamespace(**{HOOK_NAME: plain(log, "a", value="a")})

    class UnregisteringPlugin:
        @hookimpl(tryfirst=True)
        def seq_hook(self, x: object) -> str:
            log.enter("k")
            # Mutate the registry while another multicall is iterating its
            # own copy: HookCaller._remove_plugin rewrites _hookimpls[:].
            if pm.is_registered(plugin_a):
                pm.unregister(plugin_a)
                pm.unregister(plugin_b)
            log.returned("k", "k")
            return "k"

    plugin_k = UnregisteringPlugin()
    pm.register(plugin_a, name="a")
    pm.register(plugin_b, name="b")
    pm.register(plugin_k, name="k")

    # First call: HookCaller.__call__ snapshots _hookimpls.copy(); tryfirst
    # makes k run first, and it unregisters both a and b while the copied list
    # is still being iterated by _multicall.
    assert pm.hook.seq_hook(x=1, y=0) == ["k", "b", "a"]
    first_call = list(log.events)
    assert first_call == [
        "k: enter",
        "k: return 'k'",
        "b: enter",
        "b: return 'b'",
        "a: enter",
        "a: return 'a'",
    ], "the in-flight call must finish on the snapshot copy (#438)"

    # A second call sees the mutated registry: k alone remains.
    log.events.clear()
    assert pm.hook.seq_hook(x=2, y=0) == ["k"]
    assert log.events == [
        "k: enter",
        "k: return 'k'",
    ], "unregister takes effect for calls started afterwards, not in flight"


# --------------------------------------------------------------------------- #
# SEQ-06: HookImpl.argnames trims caller kwargs; missing args are a hard error
# --------------------------------------------------------------------------- #


def _argnames(pm: PluginManager, plugin_name: str) -> tuple[str, ...]:
    (hookimpl_obj,) = (
        impl
        for impl in pm.hook.seq_hook.get_hookimpls()
        if impl.plugin_name == plugin_name
    )
    return hookimpl_obj.argnames


def test_seq_06a_signature_trimming_binds_only_declared_kwargs() -> None:
    # SEQ-06A
    pm = _make_pm(Case("SEQ-06", [], []))
    log = EventLog()
    pm.register(
        types.SimpleNamespace(**{HOOK_NAME: plain(log, "only_x")}),
        name="only_x",
    )
    pm.register(
        types.SimpleNamespace(
            **{HOOK_NAME: plain(log, "x_and_y", signature_extra="y")}
        ),
        name="x_and_y",
    )

    assert _argnames(pm, "only_x") == ("x",)
    assert _argnames(pm, "x_and_y") == ("x", "y")

    # The caller supplies y; only the impl that declares y receives it.
    # Both impls return None, which is dropped from the result list, so the
    # empty result plus the recorded events prove both bodies ran.
    assert pm.hook.seq_hook(x=10, y=20) == []
    assert log.events == [
        "x_and_y: enter",
        "x_and_y: return None",
        "only_x: enter",
        "only_x: return None",
    ], "arg binding is [caller_kwargs[a] for a in hook_impl.argnames]"


def test_seq_06b_missing_caller_kwarg_raises_hookcallerror() -> None:
    # SEQ-06B
    # With a spec, an undeclared arg is a registration error.  The call-time
    # branch instead bites a spec-less caller (or an impl requesting an
    # optional-spec argument that the invocation omits): _multicall's list
    # comprehension raises KeyError, converted to HookCallError.
    pm = _make_pm(Case("SEQ-06", [], [], no_spec=True))
    log = EventLog()
    pm.register(
        types.SimpleNamespace(
            **{HOOK_NAME: plain(log, "needs_z", signature_extra="z")}
        ),
        name="needs_z",
    )
    with pytest.raises(HookCallError, match="hook call must provide argument 'z'"):
        pm.hook.seq_hook(x=1)
    assert log.events == [], "KeyError becomes HookCallError before the body runs"


def test_seq_06c_keyword_only_args_are_not_bound_by_multicall() -> None:
    # SEQ-06C
    """varnames excludes keyword-only parameters from argnames, so multicall
    never passes them (they fall back to declared defaults)."""
    pm = _make_pm(Case("SEQ-06", [], []))
    log = EventLog()

    function = plain(log, "kwonly", signature_extra="*, z='default'")
    pm.register(types.SimpleNamespace(**{HOOK_NAME: function}), name="kwonly")
    assert _argnames(pm, "kwonly") == ("x",)
    # None is dropped from results, so an empty result list plus the recorded
    # events proves the body ran even though multicall never binds z.
    assert pm.hook.seq_hook(x=1, y=2, z="ignored") == []
    assert log.events == ["kwonly: enter", "kwonly: return None"]


def test_seq_06d_optional_impl_allowed_without_spec() -> None:
    # SEQ-06D
    """Without a spec, an impl requesting an argument the caller omits still
    fails at call time inside _multicall (registration itself is unchecked)."""
    case = Case("SEQ-06", [], [], no_spec=True)
    pm = _make_pm(case)
    log = EventLog()
    pm.register(
        types.SimpleNamespace(**{HOOK_NAME: plain(log, "p", signature_extra="y")}),
        name="p",
    )
    with pytest.raises(HookCallError, match="'y'"):
        pm.hook.seq_hook(x=1)


# --------------------------------------------------------------------------- #
# SEQ-12: re-entrant registration inside a historic call is well defined via
# the same snapshot copy; pluggy makes no cross-thread locking guarantee.
# --------------------------------------------------------------------------- #


def test_seq_12_reentrant_registration_during_historic_call() -> None:
    # SEQ-12
    pm = _make_historic_pm()
    log = EventLog()
    delivered: list[object] = []

    late_plugin = types.SimpleNamespace(**{HOOK_NAME: plain(log, "late", mode="echo")})

    def register_late(x: object) -> None:
        log.enter("early")
        log.returned("early", None)
        if not pm.is_registered(late_plugin):
            pm.register(late_plugin, name="late")

    early_plugin = types.SimpleNamespace()
    setattr(
        early_plugin,
        HOOK_NAME,
        hookimpl(register_late),
    )

    pm.register(early_plugin, name="early")
    pm.hook.seq_hook.call_historic(kwargs={"x": "h"}, result_callback=delivered.append)

    # The outer multicall iterates its [early] snapshot: registering late does
    # not add late to that copy.  Registration's _maybe_apply_history replays
    # the single recorded call to late exactly once, nested inside early.
    assert delivered == [("late", "h")]
    assert log.events == [
        "early: enter",
        "early: return None",
        "late: enter",
        "late: return ('late', 'h')",
    ], (
        "re-entrant register() during a historic call replays once and does "
        "not insert the new impl into the running multicall's copy"
    )

    # A subsequent call invokes both current impls; order is LIFO, late first,
    # then early.  Only late's non-None result reaches the callback.
    log.events.clear()
    delivered.clear()
    pm.hook.seq_hook.call_historic(kwargs={"x": "h2"}, result_callback=delivered.append)
    assert delivered == [("late", "h2")]
    assert log.events == [
        "late: enter",
        "late: return ('late', 'h2')",
        "early: enter",
        "early: return None",
    ], "the snapshot for the new call already contains both impls"
