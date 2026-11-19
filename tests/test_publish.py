"""容量分界的发布语义：幂等、失败回退、记录保留、页面与导出同一结论。"""
import os
import tempfile

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="capacity-publish-"))

from app import publish, store  # noqa: E402
from app.capacity import canonical_seconds  # noqa: E402
from app.exporter import evidence_csv  # noqa: E402
from app.pipeline import run  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample_data", "capacity-shift.csv")
TEXT = open(SAMPLE, encoding="utf-8").read()


def _restore(text):
    """把容量从 10 恢复到 11，模拟“容量恢复/再次修订”。"""
    old = "2026-11-18T04:00:00+00:00,2026-11-20T00:00:00+00:00,capacity,2,10"
    new = "2026-11-18T04:00:00+00:00,2026-11-20T00:00:00+00:00,capacity,2,11"
    assert old in text
    return text.replace(old, new)


@pytest.fixture(autouse=True)
def clean_store(monkeypatch):
    from pathlib import Path

    directory = Path(tempfile.mkdtemp(prefix="capacity-publish-"))
    monkeypatch.setattr(publish, "DATA_DIR", directory)
    monkeypatch.setattr(store, "DATA_DIR", directory)
    publish.FAULT_HOOK = None
    yield
    publish.FAULT_HOOK = None


def _publish(text, run_id="r"):
    return publish.publish(run(text)["boundary"], run_id=run_id)


def test_same_revision_is_idempotent():
    first = _publish(TEXT, "r1")
    second = _publish(TEXT, "r1")
    assert first["revision"] == 1 and first["idempotent"] is False
    assert second["revision"] == 1 and second["idempotent"] is True
    assert publish.revisions(second["series"]) == publish.revisions(first["series"])
    assert len(publish.revisions(first["series"])) == 1
    statuses = [a["status"] for a in publish.attempts(first["series"])]
    assert statuses == ["published", "idempotent"]


def test_capacity_restore_creates_new_revision_with_incremental_merge():
    first = _publish(TEXT, "r1")
    second = _publish(_restore(TEXT), "r2")
    assert second["revision"] == 2 and second["previous_revision"] == 1
    merge = second["conclusion"]["merge"]
    assert merge["previous_revision"] == 1
    assert merge["carried_segments"] >= 1
    assert merge["recomputed_segments"] >= 1
    assert merge["affected_from"] is not None
    carried = [s for s in second["conclusion"]["segments"] if s["carried"]]
    assert carried and all(s["source_revision"] == 1 for s in carried)
    assert [r["revision"] for r in publish.revisions(first["series"])] == [1, 2]


def test_repeat_same_restored_revision_does_not_double_charge():
    _publish(TEXT, "r1")
    _publish(_restore(TEXT), "r2")
    again = _publish(_restore(TEXT), "r2")
    assert again["revision"] == 2 and again["idempotent"] is True
    assert [r["revision"] for r in publish.revisions(again["series"])] == [1, 2]


@pytest.mark.parametrize("phase", ["before_write", "after_write"])
def test_interruption_keeps_previous_conclusion(phase):
    first = _publish(TEXT, "r1")
    before = publish.get(first["series"])
    protected_before = before["conclusion"]["protected_basis"]
    publish.FAULT_HOOK = lambda hook_phase, payload: "模拟发布中断" if hook_phase == phase else None
    with pytest.raises(publish.PublishInterrupted):
        _publish(_restore(TEXT), "r2")
    publish.FAULT_HOOK = None
    after = publish.get(first["series"])
    assert after["revision"] == 1
    assert after["conclusion"]["summary"] == before["conclusion"]["summary"]
    assert after["conclusion"]["protected_basis"] == protected_before
    assert after["conclusion"]["basis"] == before["conclusion"]["basis"]
    attempts = publish.attempts(first["series"])
    assert attempts[-1]["status"] == "failed"
    assert "模拟发布中断" in attempts[-1]["detail"]


def test_retry_after_failure_publishes_once():
    _publish(TEXT, "r1")
    publish.FAULT_HOOK = lambda hook_phase, payload: "中断" if hook_phase == "after_write" else None
    with pytest.raises(publish.PublishInterrupted):
        _publish(_restore(TEXT), "r2")
    publish.FAULT_HOOK = None
    retried = _publish(_restore(TEXT), "r2")
    assert retried["revision"] == 2 and retried["idempotent"] is False
    repeat = _publish(_restore(TEXT), "r2")
    assert repeat["revision"] == 2 and repeat["idempotent"] is True
    assert [r["revision"] for r in publish.revisions(retried["series"])] == [1, 2]


def test_protected_records_are_never_lost():
    first = _publish(TEXT, "r1")
    protected_before = first["conclusion"]["protected_basis"]
    assert protected_before
    # 让旧租约提前结束：受保护记录应转入保留档案而不是消失
    changed = TEXT.replace(
        "L01,CAD-Pro,alice,2026-11-18T02:00:00+00:00,2026-11-18T10:00:00+00:00,offline,1,1",
        "L01,CAD-Pro,alice,2026-11-18T02:00:00+00:00,2026-11-18T06:00:00+00:00,offline,1,1",
    )
    second = _publish(changed, "r2")
    conclusion = second["conclusion"]
    retained = conclusion["retained_protected"]
    current = {(r["lease_id"], canonical_seconds(r["protected_from"])) for r in conclusion["protected_basis"]}
    kept = {(r["lease_id"], canonical_seconds(r["protected_from"])) for r in retained}
    for entry in protected_before:
        key = (entry["lease_id"], canonical_seconds(entry["protected_from"]))
        assert key in current or key in kept
    assert conclusion["basis"]["capacity_lines"] == first["conclusion"]["basis"]["capacity_lines"]
    item = {"id": second["publication"]["run_id"], "digest": second["publication"]["run_id"], "rule_version": "z",
            "decision": "待复核", "revision": 0, "result": {"boundary": conclusion}}
    text = evidence_csv(item)
    assert "boundary-protected" in text and "RETAINED" in text


def test_store_attaches_publication_and_page_matches_export():
    item = store.save(TEXT, run(TEXT))
    result = item["result"]
    assert result["boundary_status"] == "published"
    assert result["publication"]["revision"] == 1
    published = publish.get(result["publication"]["series"])
    assert published["conclusion"]["summary"] == result["boundary"]["summary"]
    text = evidence_csv(item)
    assert '"protected_peak"": {}'.format(result["boundary"]["summary"]["protected_peak"]) in text
    assert result["publication"]["fingerprint"] in text
    assert "boundary-summary" in text and "boundary-segment" in text
    # 导出与页面读同一个已发布结论对象
    assert published["conclusion"]["segments"] == result["boundary"]["segments"]
    assert published["conclusion"]["merge"] == result["boundary"]["merge"]


def test_store_failure_falls_back_to_last_good(monkeypatch):
    first = store.save(TEXT, run(TEXT))
    series = first["result"]["publication"]["series"]
    publish.FAULT_HOOK = lambda hook_phase, payload: "中断" if hook_phase == "after_write" else None
    try:
        second = store.save(_restore(TEXT), run(_restore(TEXT)))
    finally:
        publish.FAULT_HOOK = None
    result = second["result"]
    assert result["boundary_status"] == "stale"
    assert result["boundary_error"] == "中断"
    assert result["publication"]["revision"] == 1
    assert result["boundary"]["summary"] == first["result"]["boundary"]["summary"]
    assert result["boundary_unpublished"] is not None
    assert publish.get(series)["revision"] == 1
