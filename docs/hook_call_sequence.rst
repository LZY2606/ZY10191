.. _hook_call_sequence:

Hook call sequence and result reduction
=======================================

This page is an **executable timing specification** of how pluggy turns a set
of marked hook specifications and implementations into an ordered hook call.
Every timing table below has a matching table-driven test case in
``testing/test_sequence.py`` (the ``SCENARIO-xx`` id links the two), so the
documented order can be replayed by running::

    python -m pytest testing/test_sequence.py -q

Nothing on this page relies on plugin ``repr()``, dictionary/set iteration
order, wall-clock timing, the network, or the file system. All names in the
tables are explicit registration names chosen by the tests.

Components involved
-------------------

The definitions are collected and executed by four cooperating objects:

* :class:`~pluggy.HookspecMarker` / :class:`~pluggy.HookimplMarker`
  (``src/pluggy/_hooks.py``) stamp an ``<project>_spec`` / ``<project>_impl``
  options ``dict`` on the function. The options carry ``firstresult``,
  ``historic``, ``wrapper``, ``hookwrapper``, ``tryfirst``, ``trylast`` and
  ``specname``.
* :class:`~pluggy.PluginManager` (``src/pluggy/_manager.py``) discovers the
  stamped attributes during :meth:`~pluggy.PluginManager.add_hookspecs` /
  :meth:`~pluggy.PluginManager.register`, builds one
  :class:`~pluggy.HookCaller` per hook name and inserts each
  :class:`~pluggy.HookImpl` in ordering position.
* :class:`~pluggy.HookCaller` (``src/pluggy/_hooks.py``) holds the ordered
  ``_hookimpls`` list, performs history replay and kwargs verification, copies
  the impl list, and delegates execution to ``_hookexec``.
* ``pluggy._callers._multicall`` (``src/pluggy/_callers.py``) performs
  the actual forward loop and wrapper unwind;
  :class:`~pluggy.Result` (``src/pluggy/_result.py``) is the mutable outcome
  object used by old-style wrappers.

The ordered list
----------------

``HookCaller._add_hookimpl`` keeps wrappers in a **contiguous suffix** of
``_hookimpls`` and inserts plain implementations into the prefix. Stored
list order is::

    trylast nonwrappers | nonwrappers | tryfirst nonwrappers
    | trylast wrappers | wrappers | tryfirst wrappers

``_multicall`` iterates this list **in reverse**, which yields, at call time:

* wrapper **setup** (code before the ``yield``) runs first, outermost to
  innermost;
* plain implementations then run, ``tryfirst`` first, default in reverse
  registration (LIFO) order, ``trylast`` last;
* wrapper **teardown** (code after the ``yield``) runs last, innermost to
  outermost.

Consequence for registration: with no ``tryfirst``/``trylast`` options, the
**first registered wrapper is the innermost wrapper** (it starts last and
resumes first) and the **last registered wrapper is the outermost**. Within
each ordering category registration is still LIFO.

Complexity: one hook call is ``O(n)`` in the number of participating
implementations (a single reverse pass plus one unwind pass), uses ``O(n)``
temporary storage for the wrapper teardowns and the results list, and copies
the ``_hookimpls`` list once in ``HookCaller.__call__`` so mutations during
the call cannot corrupt the running iteration.

Event vocabulary
----------------

The test scaffold ``EventLog`` (``testing/test_sequence.py``) records one
event per lifecycle point. Tables here use the same vocabulary:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Event
     - Meaning
   * - ``enter``
     - the implementation function/generator was entered
   * - ``return``
     - a plain implementation returned a value (possibly ``None``)
   * - ``raise``
     - a plain implementation raised, with the exception type
   * - ``yield``
     - a wrapper reached its single yield point (setup finished)
   * - ``resume``
     - a wrapper resumed after the yield, with ``result`` or
       ``raise:<ExceptionType>`` delivered at the yield
   * - ``force_result``
     - an old-style wrapper called ``Result.force_result``
   * - ``force_exception``
     - an old-style wrapper called ``Result.force_exception``

An implementation that never runs produces **no event**; that absence is
part of the contract. This distinguishes four situations that collapse to
the same final value if only results are compared:

* the implementation was not entered (later in a ``firstresult`` call, or
  removed by a subset caller / unregister);
* it was entered and returned ``None`` (never appended to the results list);
* it returned an empty container such as ``[]`` (a real, non-None result);
* a wrapper rewrote the aggregate result that the caller eventually sees.

.. note::

   In the tables, setup events appear in the order
   ``enter`` immediately followed by ``yield`` for a wrapper, matching when
   the generator pauses. The plain calls sit between the last ``yield`` and
   the first ``resume``.

SCENARIO-01 -- plain LIFO and None filtering
--------------------------------------------

Test: ``test_scenario_01_lifo_plain_order``.

Three plain implementations are registered in order
``one -> none -> two``; ``none`` returns ``None``.

================  ==================
Step              Event
================  ==================
1                 ``enter two``
2                 ``return two "r2"``
3                 ``enter none``
4                 ``return none None``
5                 ``enter one``
6                 ``return one "r1"``
================  ==================

Final result: ``["r2", "r1"]``. The decision is the plain-impl branch in
``_multicall``::

    res = hook_impl.function(*args)
    if res is not None:          # None is filtered here
        results.append(res)

SCENARIO-02 -- mixed new/old wrappers
-------------------------------------

Test: ``test_scenario_02_mixed_wrappers_order``. Registrations in order:
new wrapper ``Wnew``, old wrapper ``Wold``, plain ``two``, plain ``one``.
The wrapper registered first (``Wnew``) is innermost; ``Wold`` is
outermost. ``Wnew`` returns a mapped list; ``Wold`` only observes.

=====  =============================  ===========================
Step   Event                           Layer
=====  =============================  ===========================
1      ``enter Wold`` / ``yield``      outermost setup
2      ``enter Wnew`` / ``yield``      innermost setup
3      ``enter one`` / ``return 1``    LIFO plain calls
4      ``enter two`` / ``return 2``
5      ``resume Wnew result``          innermost teardown first
6      ``resume Wold result``          outermost teardown last
=====  =============================  ===========================

Final result: ``["N:1", "N:2"]``. Setup is driven by the reverse ``for``
loop in ``_multicall``; teardown by ``for teardown in reversed(teardowns)``.
New wrappers hand the value directly at the ``yield``; old wrappers are
adapted by ``run_old_style_hookwrapper`` which sends a :class:`~pluggy.Result`
and finishes with ``return result.get_result()``.

SCENARIO-03 -- tryfirst / trylast layering
------------------------------------------

Test: ``test_scenario_03_tryfirst_trylast_layering``. One implementation of
each category is present: plain ``plain_last`` (trylast), ``plain``,
``plain_first`` (tryfirst); wrappers ``wrap_first`` (tryfirst old),
``wrap`` (default new), ``wrap_last`` (trylast old).

Stored layout (``_hookimpls``), iterated reversed at call time:

* wrapper setups: ``wrap_first`` (outermost) -> ``wrap`` ->
  ``wrap_last`` (innermost);
* plain calls: ``plain_first`` -> ``plain`` -> ``plain_last``;
* teardowns: ``wrap_last`` -> ``wrap`` -> ``wrap_first``.

Final result: ``["F", "P", "L"]``. Ordering is decided solely by the
insertion positions computed in ``HookCaller._add_hookimpl``
(``tryfirst`` inserts at the category end, ``trylast`` at the start,
default just after the last non-tryfirst entry).

SCENARIO-04 -- firstresult, None and empty containers
-----------------------------------------------------

Tests: ``test_scenario_04_firstresult_none_and_empty_container`` and
``test_scenario_04b_firstresult_skips_none_takes_value``.

For a ``firstresult`` spec the halt condition is inside the plain branch::

    if res is not None:
        results.append(res)
        if firstresult:
            break

Two distinct cases are pinned by tests:

* implementations returning, in call order, ``[]`` then others: the empty
  container **is** non-None, so the loop halts immediately and the hook
  returns ``[]``; later implementations emit no events (SCENARIO-04).
* implementations returning, in call order, ``None`` then ``"V"``: the
  ``None`` result is skipped, ``"V"`` halts the loop and is returned
  (SCENARIO-04b).

Return value reduction for ``firstresult`` happens once after the loop::

    result = results[0] if results else None

All wrappers still wrap the call; only the plain-impl forward loop is
short-circuited.

SCENARIO-05 -- wrappers rewrite the result
------------------------------------------

Test: ``test_scenario_05_wrappers_rewrite_result``. An innermost new wrapper
maps every item, and an outer old-style wrapper calls
``Result.force_result(["forced"])``. The plain implementation still returns
``1`` (its ``return 1`` event is recorded unchanged); what changes is the
aggregate seen outside.

The new wrapper's value becomes the outcome via the
``except StopIteration as si: result = si.value`` branch of the unwind loop;
the old wrapper's value replaces the outcome through
``Result.force_result`` (``_result`` is overwritten, ``_exception``
cleared), and ``run_old_style_hookwrapper`` returns ``result.get_result()``.
Final result: ``["forced"]``.

SCENARIO-06 -- exception before a wrapper yield
-----------------------------------------------

Test: ``test_scenario_06_wrapper_raises_before_yield``. A wrapper raises
during setup, before its single yield.

* the reverse forward loop is aborted (``except BaseException as exc:
  exception = exc``), so every not-yet-started wrapper and every plain
  implementation is skipped;
* wrappers already paused at their yield still unwind and receive the
  exception via ``teardown.throw(exception)``.

Events recorded: the already-started outer wrapper ``enter``/``yield``,
then the failing wrapper ``enter`` only, then the outer wrapper's
``resume ... raise:ValueError``. No plain implementation runs. The hook
re-raises the original ``ValueError``.

SCENARIO-07 -- plain impl raises BaseException
----------------------------------------------

Test: ``test_scenario_07_plain_impl_raises_baseexception``. A plain
implementation raises :class:`KeyboardInterrupt`, which is a
:class:`BaseException` but not an :class:`Exception`; a plain implementation
registered earlier in the list (called earlier) still ran, and later plain
implementations are not entered.

* ``except BaseException`` (not ``except Exception``) captures it;
* unwind proceeds innermost to outermost with ``throw(exception)``;
* the innermost new-style wrapper catches it **at its yield inside the
  generator** and returns ``"recovered"``; the unwind then sets
  ``exception = None`` (``StopIteration`` branch), so the outer old-style
  wrapper resumes normally and observes the recovered value.

Contract consequence: an old-style wrapper alone cannot swallow an
exception -- ``run_old_style_hookwrapper`` always ends with
``return result.get_result()``. Suppression must come from an enclosing
new-style wrapper (or ``force_result`` in an old-style wrapper, see
SCENARIO-08). Final result: ``"recovered"``.

SCENARIO-08 -- exception after a wrapper yield
----------------------------------------------

Test: ``test_scenario_08_wrapper_raises_after_yield``. The innermost
(new-style) wrapper resumes normally on the plain result, then raises in its
post-yield block. The outer (old-style) wrapper saves the call.

=====  ==================================
Step   Event
=====  ==================================
1-2    outer then inner wrapper setup
3      plain ``return 1``
4      ``resume Winner result`` then ``ValueError`` raised
5      ``resume Wouter raise:ValueError`` (thrown into the adapter)
6      ``force_result Wouter "saved"``
=====  ==================================

The inner teardown exception exits ``teardown.send()`` through
``except BaseException as e: exception = e``; the next teardown receives it
with ``teardown.throw(exception)``. In ``run_old_style_hookwrapper`` the
throw is caught at ``res = yield`` and wrapped as
``Result(None, exc)``; the user teardown calling ``force_result`` clears
that exception, so the hook returns ``"saved"``.

SCENARIO-09 -- force_exception read by another wrapper
------------------------------------------------------

Test: ``test_scenario_09_force_exception_then_outer_reads``. The inner
old-style wrapper sees a successful plain result but calls
``Result.force_exception(RuntimeError("forced"))``; the outer new-style
wrapper catches the forced exception at its yield and returns
``"outer-recovered"``.

``force_exception`` overwrites ``_exception`` and clears ``_result`` and
the stored traceback; ``run_old_style_hookwrapper`` then re-raises through
``get_result()``, and ``_multicall`` throws it into the outer generator.
This proves the forced exception is indistinguishable from one genuinely
raised by an inner implementation. Final result: ``"outer-recovered"``.

SCENARIO-10 -- subset_hook_caller
---------------------------------

Test: ``test_scenario_10_subset_hook_caller``. ``subset_hook_caller``
returns a :class:`~pluggy.HookCaller` proxy (``_SubsetHookCaller``) whose
``_hookimpls`` property filters the **original** caller's list on every
access; it does not copy implementations.

* removing plugin ``B`` from ``A, B, C`` yields ``["c", "a"]`` while the
  original caller still yields ``["c", "b", "a"]``;
* if none of the plugins to remove actually implement the hook,
  ``PluginManager.subset_hook_caller`` returns the **original** caller (a
  documented fast path, asserted by identity);
* because the subset is a live filtered view, a plugin registered
  afterwards participates in both the subset and the original call.

SCENARIO-11 -- historic replay and the concurrency boundary
-----------------------------------------------------------

Tests: ``test_scenario_11_historic_replay`` and
``test_scenario_11_historic_rejects_wrappers``.

For a ``historic`` spec, ``HookCaller.set_specification`` allocates
``_call_history``. ``call_historic`` appends one
``(kwargs, result_callback)`` tuple, invokes the currently registered
implementations, and feeds each non-None result to the callback. When a new
implementation is registered, ``PluginManager.register`` calls
``HookCaller._maybe_apply_history``, which runs the **single new**
implementation through ``_multicall`` once per memorized call, in history
insertion order, at register time:

.. list-table::
   :header-rows: 1
   :widths: 28 72

   * - Moment
     - Deliveries
   * - ``call_historic(arg=1)``
     - early-1 to callback
   * - ``call_historic(arg=2)``
     - early-2 to callback
   * - register ``late``
     - late-1, late-2 (full replay of the new impl)
   * - ``call_historic(arg=3)``
     - late-3, early-3 (current impls, LIFO)
   * - register ``late2``
     - late2-1, late2-2, late2-3 (full replay)

Compatibility and concurrency, stated exactly as implemented:

* historic hooks cannot return values to the original caller; the only sink
  is ``result_callback``, invoked per non-None result;
* historic is incompatible with ``firstresult`` (rejected when marking the
  spec) and with ``wrapper``/``hookwrapper`` (rejected at ``register`` by
  ``PluginManager._verify_hook``);
* replay is synchronous and runs inline inside ``register``: by the time
  ``register`` returns, all replay calls for that implementation have
  completed.  There is **no** locking around ``_call_history`` or
  ``register``: pluggy provides no guarantee for concurrent ``register`` /
  ``call_historic`` from multiple threads, and callers needing
  cross-thread publication must provide their own synchronization.

SCENARIO-12 -- dynamic unregister during a call
-----------------------------------------------

Test: ``test_scenario_12_dynamic_unregister_uses_snapshot``. An
implementation calls ``PluginManager.unregister`` on its own plugin while
the hook is running.

``HookCaller.__call__`` passes ``self._hookimpls.copy()`` to ``_multicall``
and ``HookCaller._remove_plugin`` mutates the shared list in place.
Therefore:

* the current invocation still executes every implementation captured in
  the snapshot, including the one that unregistered itself (its
  ``return None`` event is recorded but contributes no result);
* the next invocation uses the shortened list, so the removed
  implementation emits no events at all.

The same snapshot is why a plugin registered from within a running hook
(``PluginManager.register`` appends to the live list) does not join the
in-progress multicall either; it participates starting with the next call.

SCENARIO-13 -- implementation signature trimming
------------------------------------------------

Test: ``test_scenario_13_signature_trimming``. A hookimpl may request fewer
positional parameters than the spec declares. ``HookImpl.__init__`` records
``argnames`` via ``varnames`` (stripping the conventional ``self``/``cls``),
and ``_multicall`` binds only those::

    args = [caller_kwargs[argname] for argname in hook_impl.argnames]

Consequences pinned by the test:

* an impl declaring only ``a`` receives ``a`` and runs even though the call
  also provides ``b``;
* a parameter declared with a default (``b=99``) is part of the function
  signature but is not filled from the call, so the default survives;
* requesting a parameter the call does not provide raises
  :class:`~pluggy.HookCallError` from the ``KeyError`` binding branch.

Arguments with defaults are treated as optional keyword parameters and are
not part of the bound positional set; there is no implicit injection for
keyword-only parameters.

Branch reference
----------------

Where ordering, reduction and exception propagation are actually decided:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Decision
     - Source location
   * - Stored order / split point
     - ``HookCaller._add_hookimpl`` in ``src/pluggy/_hooks.py`` (wrapper suffix, tryfirst/trylast insertion)
   * - Snapshot per call
     - ``self._hookimpls.copy()`` in ``HookCaller.__call__`` / ``call_historic``
   * - Positional argument binding
     - ``for hook_impl in reversed(hook_impls)`` and the ``hook_impl.argnames`` comprehension in ``_multicall`` (``src/pluggy/_callers.py``)
   * - None filtering / firstresult
     - ``if res is not None`` and ``if firstresult: break`` in ``_multicall``
   * - Wrapper setup
     - the ``hookwrapper`` / ``wrapper`` branches in the forward loop (``next(function_gen)``)
   * - Exception capture
     - ``except BaseException as exc`` around the forward loop
   * - Unwind order
     - ``for teardown in reversed(teardowns)``
   * - Exception into teardown
     - ``teardown.throw(exception)`` and the ``StopIteration-as-result`` / ``except BaseException`` branches
   * - firstresult reduction
     - ``results[0] if results else None``
   * - Old-style outcome object
     - ``run_old_style_hookwrapper`` and ``Result.force_result`` / ``force_exception`` / ``get_result`` (``src/pluggy/_result.py``)
   * - Historic replay
     - ``HookCaller._maybe_apply_history``
   * - Subset filtering
     - ``_SubsetHookCaller._hookimpls`` property
   * - Removal during a call
     - ``HookCaller._remove_plugin``

Compatibility trade-offs
------------------------

* New-style (``wrapper=True``) and old-style (``hookwrapper=True``)
  wrappers are fully interoperable in the same call (SCENARIO-02). New-style
  wrappers can return a replacement value or let an exception propagate;
  old-style wrappers cannot return a value and only communicate through the
  :class:`~pluggy.Result` object.
* Teardown exceptions raised by old-style wrappers emit
  :class:`~pluggy.PluggyTeardownRaisedWarning`; the documented guidance is to
  use ``force_exception`` instead. New-style wrappers intentionally raise or
  return as ordinary generators.
* None-vs-value semantics are preserved for backward compatibility: only an
  explicit non-None return is a result, and an empty container counts as a
  value (including under ``firstresult``).
* Historic calls intentionally forgo return values and reject wrappers;
  these constraints are validated rather than silently ignored.
