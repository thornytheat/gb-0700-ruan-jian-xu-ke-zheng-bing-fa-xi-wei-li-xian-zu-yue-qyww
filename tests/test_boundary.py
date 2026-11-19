"""容量下调后的席位责任分界：纯计算与发布语义。"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="capacity-boundary-"))

from app.capacity import (  # noqa: E402
    allocate_excess,
    boundary_fingerprint,
    build_epochs,
    capacity_state,
    compute_boundary,
    merge_publication,
)
from app.pipeline import run  # noqa: E402
from app.timeops import instant  # noqa: E402

SHIFT = open(
    os.path.join(os.path.dirname(__file__), "..", "sample_data", "capacity-shift.csv"), encoding="utf-8"
).read()


def ts(value):
    return instant(value)


def _lease(lease_id, start, end, kind="online", seats=1, product="CAD", user="u", rev=1, line=1):
    return {
        "lease_id": lease_id,
        "canonical": product,
        "user": user,
        "kind": kind,
        "start": start,
        "end": end,
        "rev": rev,
        "quantity": seats,
        "line": line,
        "a": ts(start),
        "b": ts(end),
    }


def _capacity(lease_id, start, end, seats, version=1, product="CAD", line=1):
    return {
        "lease_id": lease_id,
        "canonical": product,
        "user": "",
        "kind": "capacity",
        "start": start,
        "end": end,
        "rev": version,
        "quantity": seats,
        "line": line,
        "a": ts(start),
        "b": ts(end),
    }


def _ctx(leases, capacities):
    return {"leases": leases, "normalized": capacities, "input_count": len(leases) + len(capacities)}


# --- 生效时刻与世代 ---------------------------------------------------------

def test_epoch_anchor_is_capacity_change():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T08:00:00+00:00", 10, 1),
        _capacity("c2", "2026-11-18T04:00:00+00:00", "2026-11-18T08:00:00+00:00", 6, 2),
    ]
    leases = [_lease("L1", "2026-11-18T02:00:00+00:00", "2026-11-18T06:00:00+00:00")]
    epochs = build_epochs(caps, leases)
    assert [e["anchor"] for e in epochs] == [
        ts("2026-11-18T00:00:00+00:00"),
        ts("2026-11-18T04:00:00+00:00"),
    ]
    assert [e["seats"] for e in epochs] == [10, 6]


def test_capacity_state_version_priority():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T08:00:00+00:00", 10, 1),
        _capacity("c2", "2026-11-18T00:00:00+00:00", "2026-11-18T08:00:00+00:00", 4, 3),
    ]
    state = capacity_state(caps, ts("2026-11-18T01:00:00+00:00"))
    assert state["seats"] == 4 and state["version"] == 3 and not state["conflict"]


def test_capacity_state_conflict_is_explicit():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T08:00:00+00:00", 10, 2),
        _capacity("c2", "2026-11-18T00:00:00+00:00", "2026-11-18T08:00:00+00:00", 4, 2),
    ]
    state = capacity_state(caps, ts("2026-11-18T01:00:00+00:00"))
    assert state["conflict"] is True and state["seats"] == 4


# --- 受保护旧租约不追溯 ----------------------------------------------------

def test_offline_prior_start_is_protected_not_excess():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 10, 1),
        _capacity("c2", "2026-11-18T04:00:00+00:00", "2026-11-18T12:00:00+00:00", 3, 2),
    ]
    leases = [
        _lease(f"off{i}", "2026-11-18T01:00:00+00:00", "2026-11-18T10:00:00+00:00", kind="offline")
        for i in range(5)
    ]
    boundary = compute_boundary(_ctx(leases, caps))
    after = [s for s in boundary["segments"] if s["anchor"] == ts("2026-11-18T04:00:00+00:00")]
    assert after and all(s["capacity"] == 3 for s in after)
    assert all(s["protected"] == 5 for s in after)
    assert all(s["new_excess"] == 0 for s in after)
    assert all(s["protected_over"] == 2 for s in after)
    assert all(s["status"] == "PROTECTED" for s in after)


def test_offline_release_before_expiry_still_protected():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 10, 1),
        _capacity("c2", "2026-11-18T04:00:00+00:00", "2026-11-18T12:00:00+00:00", 2, 2),
    ]
    leases = [_lease("off", "2026-11-18T01:00:00+00:00", "2026-11-18T10:00:00+00:00", kind="offline")]
    boundary = compute_boundary(_ctx(leases, caps))
    protected = [p for p in boundary["protected_basis"] if p["lease_id"] == "off"]
    assert protected and protected[0]["expiry_protected"] is True
    assert protected[0]["reason"] == "OFFLINE_UNTIL_EXPIRY"
    assert protected[0]["protected_until"] == ts("2026-11-18T10:00:00+00:00")


def test_online_prior_start_protected_until_own_end():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 10, 1),
        _capacity("c2", "2026-11-18T04:00:00+00:00", "2026-11-18T12:00:00+00:00", 2, 2),
    ]
    leases = [
        _lease("old", "2026-11-18T01:00:00+00:00", "2026-11-18T05:00:00+00:00", kind="online"),
        _lease("new1", "2026-11-18T05:00:00+00:00", "2026-11-18T09:00:00+00:00", kind="online"),
        _lease("new2", "2026-11-18T05:00:00+00:00", "2026-11-18T09:00:00+00:00", kind="online"),
    ]
    boundary = compute_boundary(_ctx(leases, caps))
    anchor = ts("2026-11-18T04:00:00+00:00")
    early = [s for s in boundary["segments"] if s["anchor"] == anchor and s["b"] <= ts("2026-11-18T05:00:00+00:00")]
    later = [s for s in boundary["segments"] if s["anchor"] == anchor and s["a"] >= ts("2026-11-18T05:00:00+00:00")]
    assert early and all(s["protected"] == 1 and s["new_seats"] == 0 and s["new_excess"] == 0 for s in early)
    assert later and all(s["protected"] == 0 and s["new_seats"] == 2 and s["new_excess"] == 0 for s in later)
    # 容量 2 恰好容纳两个新租约；再加一个即越过剩余容量
    extra = [s for s in compute_boundary(_ctx(leases + [ _lease("new3", "2026-11-18T05:00:00+00:00", "2026-11-18T09:00:00+00:00", kind="online")], caps))["segments"]
             if s["anchor"] == anchor and s["a"] >= ts("2026-11-18T05:00:00+00:00")]
    assert extra and all(s["new_excess"] == 1 and s["status"] == "OVERUSE" for s in extra)


# --- 剩余容量与新占用 ------------------------------------------------------

def test_new_usage_respects_remaining_capacity():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 10, 1),
        _capacity("c2", "2026-11-18T04:00:00+00:00", "2026-11-18T12:00:00+00:00", 6, 2),
    ]
    leases = [
        _lease(f"off{i}", "2026-11-18T01:00:00+00:00", "2026-11-18T08:00:00+00:00", kind="offline")
        for i in range(4)
    ] + [
        _lease(f"new{i}", "2026-11-18T04:00:00+00:00", "2026-11-18T06:00:00+00:00", kind="online")
        for i in range(3)
    ]
    boundary = compute_boundary(_ctx(leases, caps))
    segment = next(s for s in boundary["segments"] if s["anchor"] == ts("2026-11-18T04:00:00+00:00"))
    assert segment["capacity"] == 6
    assert segment["protected"] == 4
    assert segment["remaining"] == 2
    assert segment["new_seats"] == 3
    assert segment["new_excess"] == 1
    assert sum(x["seats"] for x in segment["offenders"]) == 1


def test_protected_over_plus_new_excess_identity():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 10, 1),
        _capacity("c2", "2026-11-18T04:00:00+00:00", "2026-11-18T12:00:00+00:00", 4, 2),
    ]
    leases = [
        _lease(f"off{i}", "2026-11-18T01:00:00+00:00", "2026-11-18T08:00:00+00:00", kind="offline")
        for i in range(6)
    ] + [
        _lease(f"new{i}", "2026-11-18T04:00:00+00:00", "2026-11-18T06:00:00+00:00", kind="online")
        for i in range(2)
    ]
    boundary = compute_boundary(_ctx(leases, caps))
    for segment in boundary["segments"]:
        assert segment["protected_over"] + segment["new_excess"] == max(
            0, segment["concurrent"] - segment["capacity"]
        )


def test_allocate_excess_only_in_new_leases():
    old = {"a": ts("2026-11-18T01:00:00+00:00"), "lease_id": "old", "user": "old", "quantity": 5}
    new = {"a": ts("2026-11-18T05:00:00+00:00"), "lease_id": "new", "user": "new", "quantity": 1}
    assert allocate_excess([old, new][1:], 1) == [{"id": "new", "user": "new", "seats": 1}]


def test_no_capacity_means_all_new_excess():
    leases = [_lease("L1", "2026-11-18T01:00:00+00:00", "2026-11-18T02:00:00+00:00")]
    boundary = compute_boundary(_ctx(leases, []))
    assert boundary["summary"]["new_excess_peak"] == 1
    assert boundary["summary"]["conflicts"] == 0


# --- 容量恢复 / 再次修订 ---------------------------------------------------

def test_capacity_restore_recomputes_from_new_anchor():
    caps = [
        _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 10, 1),
        _capacity("c2", "2026-11-18T04:00:00+00:00", "2026-11-18T12:00:00+00:00", 4, 2),
        _capacity("c3", "2026-11-18T08:00:00+00:00", "2026-11-18T12:00:00+00:00", 9, 3),
    ]
    leases = [
        _lease(f"off{i}", "2026-11-18T01:00:00+00:00", "2026-11-18T11:00:00+00:00", kind="offline")
        for i in range(3)
    ] + [
        _lease(f"new{i}", "2026-11-18T05:00:00+00:00", "2026-11-18T07:00:00+00:00", kind="online")
        for i in range(3)
    ]
    boundary = compute_boundary(_ctx(leases, caps))
    assert [e["anchor"] for e in boundary["epochs"]] == [
        ts("2026-11-18T00:00:00+00:00"),
        ts("2026-11-18T04:00:00+00:00"),
        ts("2026-11-18T08:00:00+00:00"),
    ]
    restored = next(e for e in boundary["epochs"] if e["anchor"] == ts("2026-11-18T08:00:00+00:00"))
    assert restored["capacity"] == 9
    assert restored["protected_peak"] == 3
    assert restored["new_excess_peak"] == 0


def test_recompute_after_revision_only_touches_affected_range():
    leases = [_lease("a", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00")]
    first = compute_boundary(_ctx(
        leases,
        [_capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 5, 1)],
    ))
    second = compute_boundary(_ctx(
        leases,
        [
            _capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 5, 1),
            _capacity("c2", "2026-11-18T06:00:00+00:00", "2026-11-18T12:00:00+00:00", 3, 2),
        ],
    ))
    merged = merge_publication(first, second)
    assert merged["merge"]["carried_segments"] >= 1
    assert merged["merge"]["recomputed_segments"] >= 1
    carried = [s for s in merged["segments"] if s["carried"]]
    assert carried and all(float(s["b"]) <= float(ts("2026-11-18T06:00:00+00:00")) for s in carried)
    assert merged["merge"]["affected_from"] is not None


def test_merge_carries_unchanged_full_range():
    capacities = [_capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 5, 1)]
    leases = [
        _lease("a", "2026-11-18T00:00:00+00:00", "2026-11-18T06:00:00+00:00"),
        _lease("b", "2026-11-18T06:00:00+00:00", "2026-11-18T12:00:00+00:00"),
    ]
    first = compute_boundary(_ctx(leases, capacities))
    merged = merge_publication(first, compute_boundary(_ctx(leases, capacities)))
    assert merged["merge"]["recomputed_segments"] == 0
    assert merged["merge"]["carried_segments"] == len(merged["segments"])
    assert merged["merge"]["affected_from"] is None


def test_fingerprint_stable_for_same_revision():
    ctx = _ctx(
        [_lease("a", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00")],
        [_capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 5, 1)],
    )
    assert boundary_fingerprint(compute_boundary(ctx)) == boundary_fingerprint(compute_boundary(ctx))


def test_fingerprint_changes_with_capacity():
    lease = _lease("a", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00")
    a = _ctx([lease], [_capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 5, 1)])
    b = _ctx([lease], [_capacity("c1", "2026-11-18T00:00:00+00:00", "2026-11-18T12:00:00+00:00", 4, 1)])
    assert boundary_fingerprint(compute_boundary(a)) != boundary_fingerprint(compute_boundary(b))


# --- 样例与管线 ------------------------------------------------------------

def test_shift_sample_protected_and_breach():
    boundary = run(SHIFT)["boundary"]
    assert boundary["summary"]["capacity_changes"] == 2
    assert boundary["summary"]["protected_peak"] >= 9
    assert boundary["summary"]["new_excess_peak"] >= 1
    statuses = {s["status"] for s in boundary["segments"]}
    assert {"PROTECTED", "OVERUSE"} <= statuses


def test_shift_sample_offline_protected():
    boundary = run(SHIFT)["boundary"]
    ids = {p["lease_id"] for p in boundary["protected_basis"] if p["kind"] == "offline"}
    assert {"L01", "L08"} <= ids


def test_original_sample_has_no_protected():
    from app.pipeline import sample_text
    boundary = run(sample_text())["boundary"]
    assert boundary["summary"]["protected_leases"] == 0
    assert boundary["summary"]["capacity_changes"] == 0
