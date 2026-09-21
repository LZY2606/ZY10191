.. _call_sequence:

Hook call sequence reference
============================

This page is the executable timing reference for the hook call loop.  Every
``SEQ-xx`` table below is mirrored by a test case in
``testing/test_call_sequence.py`` (same id), which records an ordered event
stream (``enter``, ``yield``, ``resume``, ``return``, ``raise``,
``force_result``, ``force_exception``) for each implementation and asserts it
exactly.  Because the tests compare event streams rather than only final
values, they distinguish four states that a return-value comparison cannot:

* an implementation that was *never called* (no events at all),
* an implementation that *returned* ``None`` (a ``return None`` event),
* an implementation that returned an *empty container* (``return ()``),
* a wrapper that *rewrote* the result it received.

Code locations below refer to the branches that actually decide ordering or
exception propagation, in ``src/pluggy/_hooks.py`` (``HookCaller``,
``HookImpl``), ``src/pluggy/_callers.py`` (``_multicall`` and
``run_old_style_hookwrapper``), ``src/pluggy/_result.py`` (``Result``) and
``src/pluggy/_manager.py`` (``PluginManager``).

How the chain is assembled
--------------------------

``PluginManager.register`` discovers implementations and calls
``HookCaller._add_hookimpl`` (``_hooks.py``).  The internal list is laid out
as (see the comment on ``HookCaller._hookimpls``):

1. ``trylast`` non-wrappers
2. plain non-wrappers
3. ``tryfirst`` non-wrappers
4. ``trylast`` wrappers
5. plain wrappers
6. ``tryfirst`` wrappers

``_multicall`` iterates this list with ``reversed(hook_impls)``
(``_callers.py``), so invocation is LIFO within each section, and
``tryfirst``/``trylast`` only reorder *within* their section — a ``trylast``
wrapper still wraps every non-wrapper, and a ``tryfirst`` plain impl still
runs inside every wrapper.

Registration inserts cost O(n) in the number of implementations of that hook
(list insert plus a scan for the split point); a hook call is O(n) plus the
wrapper teardown pass, which is also O(n).

The canonical mixed table (SEQ-01)
----------------------------------

Plugins registered in order ``p1`` (plain), ``wn1`` (new-style wrapper),
``p2`` (plain), ``wo1`` (old-style wrapper), ``p3`` (plain).  The assembled
list is ``[p1, p2, p3, wn1, wo1]`` (all non-wrappers before all wrappers,
registration order within each section).

.. list-table::
   :header-rows: 1

   * - step
     - event
     - decided by
   * - 1
     - ``wo1: enter`` / ``wo1: yield``
     - ``_multicall`` reversed loop; old-style setup runs inside
       ``run_old_style_hookwrapper`` (``next(teardown)``)
   * - 2
     - ``wn1: enter`` / ``wn1: yield``
     - ``_multicall`` wrapper branch: ``next(function_gen)``
   * - 3
     - ``p3: enter`` / ``p3: return 3``
     - plain branch: ``res = hook_impl.function(*args)``
   * - 4
     - ``p2: enter`` / ``p2: return 2``
     - plain branch
   * - 5
     - ``p1: enter`` / ``p1: return 1``
     - plain branch
   * - 6
     - ``wn1: resume [3, 2, 1]`` / ``wn1: return [3, 2, 1]``
     - teardown loop ``teardown.send(result)``; ``StopIteration.value``
       becomes the new result
   * - 7
     - ``wo1: resume ('result', [3, 2, 1])``
     - teardown loop; the old-style adapter sends a ``Result`` into the
       generator and returns ``result.get_result()``
   * - final
     - hook returns ``[3, 2, 1]``
     - ``_multicall`` tail: ``return result``

Two rules are visible here:

* **Setup order** (``enter``/``yield``) is outermost-wrapper-first, i.e. the
  reverse of the assembled list.
* **Teardown order** (``resume``) is the reverse of setup order: the
  innermost (last-entered) wrapper resumes first, because the teardown loop
  runs ``for teardown in reversed(teardowns)``.

Ordering with tryfirst/trylast (SEQ-11)
---------------------------------------

Registered in order: ``pL`` (trylast plain), ``p`` (plain), ``pF`` (tryfirst
plain), ``wl`` (trylast wrapper), ``w`` (wrapper), ``wf`` (tryfirst wrapper).
Assembled list: ``[pL, p, pF, wl, w, wf]``.

.. list-table::
   :header-rows: 1

   * - phase
     - order
     - decided by
   * - wrapper setup
     - ``wf``, ``w``, ``wl``
     - ``_add_hookimpl``: tryfirst appended at section end, trylast inserted
       at section start; reversed iteration
   * - plain calls
     - ``pF``, ``p``, ``pL``
     - same rule, non-wrapper section
   * - wrapper teardown
     - ``wl``, ``w``, ``wf``
     - ``reversed(teardowns)``

Result reduction: None vs empty containers (SEQ-02)
---------------------------------------------------

.. list-table::
   :header-rows: 1

   * - impl returns
     - appended to results?
     - event stream proof
   * - ``None``
     - no
     - ``p_none: return None`` present, value absent from the result list
   * - ``()``
     - yes
     - ``p_empty: return ()`` and ``()`` appears in the result list
   * - ``"v"``
     - yes
     - ``p_v: return 'v'``

The deciding branch is ``if res is not None: results.append(res)`` in
``_multicall``: ``None`` is the *only* dropped value.  Empty containers are
kept, which is why the tests must not equate "returned an empty container"
with "was not called".

firstresult (SEQ-03A, SEQ-03B)
------------------------------

``firstresult=True`` hooks stop the plain-impl loop at the first non-``None``
result (``if firstresult: break`` in ``_multicall``) and reduce to a scalar
(``result = results[0] if results else None``).

.. list-table::
   :header-rows: 1

   * - case
     - behaviour
     - proof
   * - SEQ-03A
     - ``f3`` returns ``"hit"``; ``f1``/``f2`` (which would return ``None``)
       are never entered
     - event stream contains only ``f3`` events
   * - SEQ-03B
     - an empty container ``()`` is a non-``None`` first result and halts the
       loop; the wrapper still resumes with the scalar ``()``
     - ``w: resume ()``; ``p2`` has no events

All wrappers always run their teardown, even after the loop breaks early.

Exceptions
----------

Wrapper raising before ``yield`` (SEQ-08, SEQ-08B)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A wrapper that raises before its ``yield`` is never suspended, so it is not
appended to ``teardowns`` and no post-yield code runs for it.  The exception
is captured by the ``except BaseException`` around the setup loop and
re-raised after the (empty) teardown pass.  Plain impls behind it are never
entered: the event stream is exactly ``wb: enter``, ``wb: raise SeqError``.
SEQ-08 covers a new-style wrapper; SEQ-08B shows an old-style wrapper behaves
identically (its adapter generator is discarded without a teardown).

Plain impl raising ``BaseException`` (SEQ-09)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The setup loop catches ``BaseException`` (not just ``Exception``), stores it
in ``exception``, and the teardown loop delivers it to each entered wrapper
inner-to-outer via ``teardown.throw(exception)``.

.. list-table::
   :header-rows: 1

   * - wrapper
     - observes
     - proof
   * - new-style ``wn`` (re-raises)
     - the ``throw`` replaces the yield value, so the ``resume`` line never
       executes — no ``wn: resume`` event
     - absence of ``wn: resume`` in the stream
   * - old-style ``wo``
     - its adapter receives a ``Result(None, exc)``; the generator sees
       ``result.exception`` set
     - ``wo: resume ('exception', 'SeqBaseError')``
   * - hook caller
     - the same ``BaseException`` instance propagates
     - ``pytest.raises(SeqBaseError)`` with label ``"p"``

Results already collected before the raise are discarded: the hook raises
instead of returning.

Wrapper raising after resume (SEQ-13A, SEQ-13B)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* New-style (SEQ-13B): the raise escapes ``teardown.send(result)``, is caught
  by the teardown loop's ``except BaseException`` and becomes the pending
  exception, delivered to outer wrappers and finally re-raised.  The event
  stream shows ``w: resume [1]`` then ``w: raise SeqError``.
* Old-style (SEQ-13A): same propagation, but
  ``run_old_style_hookwrapper`` first emits
  :class:`~pluggy.PluggyTeardownRaisedWarning` (``_warn_teardown_exception``)
  because old-style wrappers are expected to use
  :meth:`~pluggy.Result.force_exception` instead of raising.

force_exception / force_result (SEQ-10, SEQ-10B)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* SEQ-10: inner old-style wrapper ``wi`` calls
  ``Result.force_exception(SeqError("wi"))``.  The adapter's
  ``return result.get_result()`` then raises it; the outer old-style wrapper
  ``wo`` receives a ``Result`` whose ``exception`` is that ``SeqError`` and
  can *read* it with ``get_result()`` (logged as
  ``wo: resume ('caught', 'SeqError')``) without clearing it — the hook still
  raises ``wi``'s exception.
* SEQ-10B: an old-style wrapper calls ``Result.force_result([9])``; the outer
  new-style wrapper receives ``[9]`` (not the plain impls' ``[2, 1]``) and
  returns it, so the hook returns ``[9]``.

``Result.force_result`` and ``Result.force_exception`` (``_result.py``) are
the only sanctioned ways for old-style wrappers to change the outcome; both
overwrite any previous result *and* exception.

subset_hook_caller (SEQ-04)
---------------------------

``PluginManager.subset_hook_caller`` (``_manager.py``) returns a
``_SubsetHookCaller`` proxy (``_hooks.py``) whose ``_hookimpls`` property
filters the *original* caller's list by plugin object on every access.
Consequences, all pinned by SEQ-04:

* the subset call runs the remaining impls in the caller's LIFO order;
* the original caller is untouched (a direct call still includes the removed
  plugin);
* the subset is live: a plugin registered *after* the subset was created
  takes part in subsequent subset calls.

Historic calls (SEQ-05, SEQ-12)
-------------------------------

``HookCaller.call_historic`` appends ``(kwargs, result_callback)`` to
``_call_history`` and executes against a snapshot copy of the current impls.
``PluginManager.register`` then calls ``HookCaller._maybe_apply_history`` for
each newly registered impl, which replays every recorded call against
``[method]`` — the new impl only.

.. list-table::
   :header-rows: 1

   * - case
     - guarantee
   * - SEQ-05A
     - a late registration replays recorded calls with the recorded kwargs,
       synchronously inside ``register``
   * - SEQ-05B
     - ``None`` results are never delivered to ``result_callback``; empty
       containers (``[]``, ``()``) *are* delivered, because the single-impl
       replay wraps them in a truthy one-element list
   * - SEQ-05C
     - every recorded call is replayed, in record order
   * - SEQ-05D
     - wrappers are rejected for historic hooks
       (``PluginValidationError`` from ``_verify_hook``) and never execute
   * - SEQ-05E
     - the initial ``call_historic`` covers impls registered so far; replay
       does not re-invoke them
   * - SEQ-12
     - re-entrant ``register()`` during a historic call replays the recorded
       calls to the new impl exactly once and does not insert it into the
       running multicall's snapshot copy

Concurrency boundary: pluggy's data structures are plain lists and dicts.
The snapshot copies in ``HookCaller.__call__``/``call_historic`` make
*re-entrant* (same-thread) registration and unregistration during a call well
defined, but there is **no locking**: concurrent registration, unregistration
or historic replay from multiple threads is not a supported guarantee.  Do
not rely on more than the single-threaded re-entrancy described above.

Dynamic unregister during a call (SEQ-07)
-----------------------------------------

``HookCaller.__call__`` passes ``self._hookimpls.copy()`` to the hook
executor (issue #438).  An impl that unregisters other plugins mid-call
(``PluginManager.unregister`` → ``HookCaller._remove_plugin``, which rewrites
``_hookimpls[:]``) therefore cannot change the in-flight invocation: the
current call finishes on its snapshot, and only later calls observe the
mutation.  SEQ-07 proves both halves with one plugin that unregisters two
others from inside its own hook body.

Signature trimming (SEQ-06)
---------------------------

``HookImpl`` stores ``argnames`` computed by ``varnames`` (``_hooks.py``):
positional parameters without defaults, with keyword-only parameters
excluded.  ``_multicall`` binds arguments as
``[caller_kwargs[argname] for argname in hook_impl.argnames]``:

* impls declaring a subset of the spec's arguments receive only that subset
  (SEQ-06A);
* a keyword-only parameter is never bound by the call loop (SEQ-06C);
* if the invocation omits an argument the impl requires, the ``KeyError``
  becomes :class:`~pluggy.HookCallError` before the impl body runs (SEQ-06B);
  with a spec present, an undeclared argument is instead a registration-time
  :class:`~pluggy.PluginValidationError`, and without any spec the same
  call-time ``HookCallError`` applies (SEQ-06D).

Compatibility notes
-------------------

* Old-style (``hookwrapper=True``) and new-style (``wrapper=True``) wrappers
  interoperate freely; the old-style adapter is
  ``run_old_style_hookwrapper``.  Old-style wrappers cannot return results —
  they mutate the ``Result`` — and raising from their teardown triggers
  :class:`~pluggy.PluggyTeardownRaisedWarning`.  New code should prefer
  ``wrapper=True``.
* ``firstresult`` and ``historic`` are mutually exclusive (enforced by
  ``HookspecMarker.__call__``); historic hooks reject wrappers at
  registration.
* All ordering guarantees above are deterministic for a fixed registration
  order and do not depend on plugin object ``repr``, ``id`` ordering or set
  iteration order; the tests register plugins explicitly by name and compare
  ordered event streams.
