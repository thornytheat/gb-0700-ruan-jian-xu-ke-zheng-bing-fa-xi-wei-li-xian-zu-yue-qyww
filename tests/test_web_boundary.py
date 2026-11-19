"""容量分界的页面与证据导出口径一致。"""
import os
import tempfile

import pytest

from app import publish, store
from app.exporter import evidence_csv
from app.pipeline import run
from fastapi.testclient import TestClient
from app.main import app

TEXT = open(os.path.join(os.path.dirname(__file__), "..", "sample_data", "capacity-shift.csv"), encoding="utf-8").read()
RESTORED = TEXT.replace(
    "2026-11-18T04:00:00+00:00,2026-11-20T00:00:00+00:00,capacity,2,10",
    "2026-11-18T04:00:00+00:00,2026-11-20T00:00:00+00:00,capacity,2,11",
)
client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from pathlib import Path

    directory = Path(tempfile.mkdtemp(prefix="capacity-web-"))
    monkeypatch.setattr(publish, "DATA_DIR", directory)
    monkeypatch.setattr(store, "DATA_DIR", directory)
    publish.FAULT_HOOK = None
    yield
    publish.FAULT_HOOK = None


def test_module8_api_returns_published_conclusion():
    response = client.post("/api/8", json={"text": TEXT})
    assert response.status_code == 200
    payload = response.json()
    assert payload["publication"]["revision"] == 1
    assert payload["boundary"]["summary"]["protected_peak"] >= 9
    assert payload["boundary"]["merge"]["revision"] == 1


def test_module8_api_is_idempotent_on_repeat():
    first = client.post("/api/8", json={"text": TEXT}).json()
    second = client.post("/api/8", json={"text": TEXT}).json()
    assert second["idempotent"] is True
    assert second["publication"]["revision"] == first["publication"]["revision"]


def test_page_and_export_show_same_conclusion_after_revision():
    first = store.save(TEXT, run(TEXT))
    second = store.save(RESTORED, run(RESTORED))
    assert second["result"]["publication"]["revision"] == 2
    series = second["result"]["publication"]["series"]
    published = publish.get(series)["conclusion"]

    page = client.get(f"/module/8?run_id={second['id']}")
    assert page.status_code == 200
    # 页面徽标显示已发布版本
    assert "已发布" in page.text and f"v{published['publication']['revision']}" in page.text
    # 页面 KPI 与导出摘要同源
    assert f">{published['summary']['protected_peak']}<" in page.text
    text = evidence_csv(second)
    assert f'"protected_peak"": {published["summary"]["protected_peak"]}' in text
    assert published["publication"]["fingerprint"] in text
    # 页面表格行数等于已发布结论的分段数（页面读取的 M8 report 已同步为发布结论）
    rows = second["result"]["reports"][7]["rows"]
    assert len(rows) == len(published["segments"])


def test_export_uses_same_rows_as_published_conclusion():
    item = store.save(RESTORED, run(RESTORED))
    result = item["result"]
    report_rows = result["reports"][7]["rows"]
    assert len(report_rows) == len(result["boundary"]["segments"])
    for row, segment in zip(report_rows, result["boundary"]["segments"]):
        assert row["a"] == segment["a"] and row["b"] == segment["b"]
        assert row["new_excess"] == segment["new_excess"]
        assert row["protected"] == segment["protected"]


def test_boundary_revision_endpoint_lists_history():
    first = store.save(TEXT, run(TEXT))
    second = store.save(RESTORED, run(RESTORED))
    series = second["result"]["publication"]["series"]
    payload = client.get(f"/api/boundary/{series}/revisions").json()
    assert [r["revision"] for r in payload["revisions"]] == [1, 2]
    assert payload["attempts"][-1]["status"] == "published"
    current = client.get(f"/api/boundary/{series}").json()
    assert current["revision"] == 2
    assert current["conclusion"]["summary"] == second["result"]["boundary"]["summary"]
