"""Spike: verify langgraph Send map-reduce behavior (U1 + U2).

U1 (correctness): does ``Send`` fan-out to a compiled subgraph node + a
    dict-merge reducer correctly collect ALL items into the parent state?
    Asserted by ``test_send_map_reduce_collects_all_items`` (brief verbatim).

U2 (concurrency): in a SYNC graph, are multiple ``Send`` instances executed
    in parallel (overlapping wall-clock intervals) or strictly serially?
    Observed by ``test_send_concurrency_is_parallel_or_serial`` which
    instruments the subgraph's ``_process`` to record start/end monotonic
    time + thread id and inspects whether the intervals overlap.

Spike environment:
- langgraph 1.2.9 (installed in backend/.venv)
- sync graph, no real LLM
"""

from __future__ import annotations

import threading
import time
from typing import Annotated, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.types import Send


# ---------------------------------------------------------------------------
# Shared schema / helpers (from the brief, verbatim where applicable)
# ---------------------------------------------------------------------------


def _merge(left: dict, right: dict) -> dict:
    return {**(left or {}), **(right or {})}


class ParentState(TypedDict, total=False):
    items: list[str]
    results: Annotated[dict, _merge]


class SubState(TypedDict, total=False):
    item: str
    out: str


def _fanout(state: ParentState):
    return [Send("process", {"item": x}) for x in state["items"]]


def _process(state: SubState):
    return {"out": state["item"] + "_done"}


def _reduce_subgraph():
    sub = StateGraph(SubState)
    sub.add_node("process", _process)
    sub.add_edge(START, "process")
    sub.add_edge("process", END)
    return sub.compile()


# ---------------------------------------------------------------------------
# U1: brief's verbatim test
# ---------------------------------------------------------------------------


def test_send_map_reduce_collects_all_items():
    parent = StateGraph(ParentState)
    parent.add_node("fanout", _fanout)
    parent.add_node("process", _reduce_subgraph())
    parent.add_edge(START, "fanout")
    parent.add_conditional_edges("fanout", lambda x: x)
    parent.add_edge("process", END)
    graph = parent.compile()

    result = graph.invoke({"items": ["a", "b", "c"]})
    assert set(result["results"].keys()) == {"a_done", "b_done", "c_done"}


# ---------------------------------------------------------------------------
# U2: concurrency observation
# ---------------------------------------------------------------------------
#
# NOTE: The brief's exact wiring (node returning [Send] + passthrough cond
# edge) raises InvalidUpdateError in langgraph 1.2.9 (see U1). To observe
# U2 concurrency behavior we therefore use the CANONICAL Send wiring from
# langgraph's own Send docstring: the Send-returning function is used
# DIRECTLY as the conditional-edge function from START. The subgraph shares
# the ``results`` key with the parent so the dict-merge reducer can collect
# outputs (the brief's schema defect is also corrected here).
#
# This is a valid U2 probe: the scheduling behavior of Send'd tasks is the
# same regardless of how Send is wired (node vs cond edge) -- the crash in
# U1 happens before any task scheduling.


class SubStateShared(TypedDict, total=False):
    """Subgraph state sharing ``results`` with the parent (corrected schema)."""
    item: str
    results: Annotated[dict, _merge]


def test_send_concurrency_is_parallel_or_serial():
    """Record [start, end, thread_id] for each ``_process`` invocation.

    With 3 items and a 50ms sleep inside ``_process``:
      - strictly serial total wall-clock ~ 150ms+, intervals disjoint
      - parallel total wall-clock ~ 50ms, intervals overlap
    The test does NOT hard-assert parallel or serial (either outcome is a
    valid spike result); it records evidence and asserts the timing signature
    matches exactly one of the two categories.
    """
    intervals: list[tuple[float, float, int]] = []
    lock = threading.Lock()

    def _process_slow(state: SubStateShared):
        tid = threading.get_ident()
        start = time.monotonic()
        time.sleep(0.05)  # simulate work
        end = time.monotonic()
        with lock:
            intervals.append((start, end, tid))
        return {"results": {state["item"] + "_done": True}}

    def _reduce_subgraph_slow():
        sub = StateGraph(SubStateShared)
        sub.add_node("process", _process_slow)
        sub.add_edge(START, "process")
        sub.add_edge("process", END)
        return sub.compile()

    parent = StateGraph(ParentState)
    parent.add_node("process", _reduce_subgraph_slow())
    parent.add_conditional_edges(START, _fanout)  # canonical Send wiring
    parent.add_edge("process", END)
    graph = parent.compile()

    wall_start = time.monotonic()
    result = graph.invoke({"items": ["a", "b", "c"]})
    wall_end = time.monotonic()

    # Corrected-wiring sanity: items still collected via dict-merge reducer
    assert set(result["results"].keys()) == {"a_done", "b_done", "c_done"}
    assert len(intervals) == 3, f"expected 3 process invocations, got {len(intervals)}"

    # Normalize intervals to wall_start as origin for readable output
    normalized = [
        (s - wall_start, e - wall_start, tid) for (s, e, tid) in intervals
    ]
    normalized.sort(key=lambda x: x[0])

    def overlaps(a, b):
        return a[0] < b[1] and b[0] < a[1]

    any_overlap = any(
        overlaps(normalized[i], normalized[j])
        for i in range(len(normalized))
        for j in range(i + 1, len(normalized))
    )
    distinct_threads = len({tid for _, _, tid in normalized})

    total_wall = wall_end - wall_start
    sum_durations = sum(e - s for s, e, _ in normalized)
    max_duration = max(e - s for s, e, _ in normalized)

    # Expose evidence via print (visible with `pytest -v -s`)
    print("\n--- U2 concurrency evidence ---")
    print("intervals (start, end, dur, thread_id) relative to invoke start:")
    for s, e, tid in normalized:
        print(f"  start={s:.4f}s end={e:.4f}s dur={e - s:.4f}s thread={tid}")
    print(f"total wall-clock: {total_wall:.4f}s")
    print(f"sum of durations: {sum_durations:.4f}s")
    print(f"max  duration:    {max_duration:.4f}s")
    print(f"distinct threads: {distinct_threads}")
    print(f"any overlap?     : {any_overlap}")
    print(f"conclusion:      : {'PARALLEL' if any_overlap else 'SERIAL'}")
    print("-------------------------------")

    # Sanity check the detected signature is internally consistent
    if any_overlap:
        # Parallel: total wall-clock should be closer to max than to sum
        assert total_wall < sum_durations * 0.9, (
            f"inconsistent: overlap detected but total_wall={total_wall:.4f}s "
            f">= 0.9 * sum_durations={sum_durations * 0.9:.4f}s"
        )
    else:
        # Serial: total wall-clock should approximate sum of durations
        assert total_wall >= sum_durations * 0.9, (
            f"inconsistent: no overlap but total_wall={total_wall:.4f}s "
            f"< 0.9 * sum_durations={sum_durations * 0.9:.4f}s"
        )
