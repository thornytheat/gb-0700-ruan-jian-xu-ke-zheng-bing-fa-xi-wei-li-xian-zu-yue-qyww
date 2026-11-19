"""容量分界结论的发布与修订。

发布语义：
- 同一系列（租约证据不变）下，容量每发生一次有效修订就形成一个新的发布版本。
- 结论指纹相同（同一修订被重跑）时不新增版本、不重复扣减，直接返回已发布结论。
- 合并时只重算受影响的区间：产品/生效时刻/起止都相同且内容一致的段标记 carried，
  其余段重新计算；删除的区间记录在 merge.removed_* 中。
- 写入使用单个事务；任何异常（含模拟中断）都会回滚，旧版结论、旧版原始依据和
  受保护旧租约记录原样保留，只额外追加一条失败尝试记录。
"""

import json
import sqlite3
from datetime import datetime, timezone

from app.capacity import (
    boundary_fingerprint,
    canonical_seconds,
    merge_publication,
    to_seconds,
)
from app.config import DATA_DIR  # noqa: F401  (测试用 monkeypatch 直接替换本模块的 DATA_DIR)
from app.errors import BusinessError

FAULT_HOOK = None
"""测试/演练用故障钩子：callable(phase, payload)，phase 为 before_write 或 after_write。"""


class PublishInterrupted(Exception):
    """发布被中断；调用方应回退到上一版完整结论。"""


_SCHEMA_READY = set()


def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = str(DATA_DIR)
    db = sqlite3.connect(DATA_DIR / "runs.sqlite", timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA synchronous=NORMAL")
    if key in _SCHEMA_READY:
        return db
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS publications (series TEXT PRIMARY KEY, revision INTEGER NOT NULL,"
        " fingerprint TEXT NOT NULL, run_id TEXT, conclusion TEXT NOT NULL, basis TEXT NOT NULL, updated TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS publication_revisions (series TEXT NOT NULL, revision INTEGER NOT NULL,"
        " fingerprint TEXT NOT NULL, run_id TEXT, conclusion TEXT NOT NULL, basis TEXT NOT NULL,"
        " created TEXT NOT NULL, PRIMARY KEY (series, revision))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS publication_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, series TEXT NOT NULL,"
        " requested_fingerprint TEXT, status TEXT NOT NULL, revision INTEGER, detail TEXT NOT NULL, created TEXT NOT NULL)"
    )
    _SCHEMA_READY.add(key)
    return db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _phase(phase, payload):
    hook = FAULT_HOOK
    if hook is None:
        return
    result = hook(phase, payload)
    if result is not None and result is not False:
        raise PublishInterrupted(str(result))


def _validation_error(conclusion):
    try:
        for segment in conclusion.get("segments", []):
            capacity = int(segment["capacity"])
            protected = int(segment["protected"])
            new = int(segment["new_seats"])
            remaining = max(0, capacity - protected)
            over = max(0, protected + new - capacity)
            if int(segment["remaining"]) != remaining:
                return "剩余容量与受保护占席不一致"
            if int(segment["new_excess"]) != max(0, new - remaining):
                return "新超用与剩余容量不一致"
            if int(segment["protected_over"]) + int(segment["new_excess"]) != over:
                return "超用分解恒等式不成立"
            if sum(int(x["seats"]) for x in segment.get("offenders", [])) != int(segment["new_excess"]):
                return "责任分配不等于新超用"
            if to_seconds(segment["a"]) >= to_seconds(segment["b"]):
                return "区间无效"
        if not conclusion.get("basis", {}).get("rule_version"):
            return "原始依据缺少规则版本"
        if "capacity_lines" not in conclusion.get("basis", {}):
            return "原始依据缺少容量记录"
    except (KeyError, TypeError, ValueError) as exc:
        return f"结论字段缺失：{exc}"
    return None


def _protected_keys(entry):
    return (
        str(entry.get("product", "")),
        str(entry.get("lease_id", "")),
        canonical_seconds(entry.get("protected_from", 0)),
    )


def _retained_protected(previous, conclusion):
    """旧版受保护记录在本次结论中不再出现时，转入保留档案，绝不丢记录。"""
    retained = []
    seen = set()
    if previous:
        for entry in previous.get("retained_protected", []):
            key = _protected_keys(entry)
            if key not in seen:
                seen.add(key)
                retained.append(dict(entry))
        current = {_protected_keys(r) for r in conclusion.get("protected_basis", [])}
        for entry in previous.get("protected_basis", []):
            key = _protected_keys(entry)
            if key in current or key in seen:
                continue
            seen.add(key)
            item = dict(entry)
            item["reason"] = item.get("reason", "PRIOR_START")
            item["retained"] = True
            item["retained_from_revision"] = previous.get("publication", {}).get("revision")
            retained.append(item)
    return retained


def _build_publication(series, revision, fingerprint, run_id, previous_revision, status="published"):
    return {
        "series": series,
        "revision": revision,
        "fingerprint": fingerprint,
        "run_id": run_id,
        "status": status,
        "idempotent": False,
        "previous_revision": previous_revision,
        "published_at": _now(),
    }


def publish(conclusion, run_id=None):
    """发布或复用一条容量分界结论；返回发布记录。"""
    if not isinstance(conclusion, dict):
        raise BusinessError("BOUNDARY", "缺少容量分界结论")
    basis = conclusion.get("basis") or {}
    series = basis.get("series")
    if not series:
        raise BusinessError("BOUNDARY", "缺少系列指纹")
    fingerprint = boundary_fingerprint(conclusion)

    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM publications WHERE series=?", (series,)).fetchone()
        if row is not None and row["fingerprint"] == fingerprint:
            existing = json.loads(row["conclusion"])
            publication = dict(existing.get("publication") or {})
            publication.update({
                "series": series,
                "revision": row["revision"],
                "fingerprint": row["fingerprint"],
                "run_id": row["run_id"],
                "status": "published",
                "idempotent": True,
                "previous_revision": publication.get("previous_revision"),
                "published_at": row["updated"],
            })
            db.execute(
                "INSERT INTO publication_attempts(series,requested_fingerprint,status,revision,detail,created)"
                " VALUES(?,?,?,?,?,?)",
                (series, fingerprint, "idempotent", row["revision"], "同一修订重跑，未重复扣减", _now()),
            )
            db.execute("COMMIT")
            existing["publication"] = dict(publication, idempotent=True)
            return {
                "series": series,
                "revision": row["revision"],
                "fingerprint": row["fingerprint"],
                "status": "published",
                "idempotent": True,
                "previous_revision": publication.get("previous_revision"),
                "published_at": row["updated"],
                "conclusion": existing,
                "publication": publication,
                "basis": json.loads(row["basis"]),
            }

        previous = json.loads(row["conclusion"]) if row is not None else None
        previous_revision = int(row["revision"]) if row is not None else None
        # 恢复到旧容量属于新的修订：新版本重算，继承当前保留档案，不复用历史版本号
        revision = (int(row["revision"]) + 1) if row is not None else 1
        merged = merge_publication(previous, conclusion)
        merged["retained_protected"] = _retained_protected(previous, merged)
        merged["merge"]["revision"] = revision
        merged["merge"]["series"] = series

        problem = _validation_error(merged)
        if problem:
            raise PublishInterrupted(problem)

        publication = _build_publication(series, revision, fingerprint, run_id, previous_revision)
        merged["publication"] = publication
        _phase("before_write", {"series": series, "revision": revision, "publication": publication})
        now = _now()
        db.execute(
            "INSERT INTO publications(series,revision,fingerprint,run_id,conclusion,basis,updated)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(series) DO UPDATE SET revision=excluded.revision,"
            " fingerprint=excluded.fingerprint, run_id=excluded.run_id, conclusion=excluded.conclusion,"
            " basis=excluded.basis, updated=excluded.updated",
            (series, revision, fingerprint, run_id,
             json.dumps(merged, ensure_ascii=False), json.dumps(basis, ensure_ascii=False), now),
        )
        db.execute(
            "INSERT INTO publication_revisions(series,revision,fingerprint,run_id,conclusion,basis,created)"
            " VALUES(?,?,?,?,?,?,?)",
            (series, revision, fingerprint, run_id,
             json.dumps(merged, ensure_ascii=False), json.dumps(basis, ensure_ascii=False), now),
        )
        db.execute(
            "INSERT INTO publication_attempts(series,requested_fingerprint,status,revision,detail,created)"
            " VALUES(?,?,?,?,?,?)",
            (series, fingerprint, "published", revision,
             f"受影响区间 {merged['merge'].get('affected_from')}—{merged['merge'].get('affected_until')}；"
             f"重算 {merged['merge'].get('recomputed_segments')} 段，沿用 {merged['merge'].get('carried_segments')} 段",
             now),
        )
        _phase("after_write", {"series": series, "revision": revision, "publication": publication})
        db.execute("COMMIT")
        return {
            "series": series,
            "revision": revision,
            "fingerprint": fingerprint,
            "status": "published",
            "idempotent": False,
            "previous_revision": previous_revision,
            "published_at": now,
            "conclusion": merged,
            "publication": publication,
            "basis": basis,
        }
    except Exception as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _record_failure(series, fingerprint, run_id, basis, previous_revision if "previous_revision" in locals() else None, exc)
        if isinstance(exc, PublishInterrupted):
            raise
        raise PublishInterrupted(str(exc)) from exc
    finally:
        db.close()


def _record_failure(series, fingerprint, run_id, basis, previous_revision, exc):
    """失败尝试另开事务记录，不触碰已发布的结论表。"""
    try:
        db = _connect()
        try:
            db.execute(
                "INSERT INTO publication_attempts(series,requested_fingerprint,status,revision,detail,created)"
                " VALUES(?,?,?,?,?,?)",
                (series, fingerprint, "failed", previous_revision,
                 json.dumps({"error": str(exc), "run_id": run_id, "basis": basis}, ensure_ascii=False), _now()),
            )
        finally:
            db.close()
    except sqlite3.Error:
        pass


def get(series):
    db = _connect()
    try:
        row = db.execute("SELECT * FROM publications WHERE series=?", (str(series),)).fetchone()
    finally:
        db.close()
    if row is None:
        raise BusinessError("NOT_FOUND", "系列尚无已发布结论", 404)
    conclusion = json.loads(row["conclusion"])
    publication = dict(conclusion.get("publication") or {})
    publication.update({"series": row["series"], "revision": row["revision"], "fingerprint": row["fingerprint"],
                        "run_id": row["run_id"], "status": "published", "idempotent": False,
                        "published_at": row["updated"]})
    conclusion["publication"] = publication
    return {"series": row["series"], "revision": row["revision"], "fingerprint": row["fingerprint"],
            "run_id": row["run_id"], "status": "published", "idempotent": False,
            "published_at": row["updated"], "conclusion": conclusion,
            "publication": publication, "basis": json.loads(row["basis"])}


def get_or_none(series):
    try:
        return get(series)
    except BusinessError:
        return None


def revisions(series):
    db = _connect()
    try:
        rows = db.execute(
            "SELECT series,revision,fingerprint,run_id,created FROM publication_revisions"
            " WHERE series=? ORDER BY revision", (str(series),)
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


def attempts(series):
    db = _connect()
    try:
        rows = db.execute(
            "SELECT series,requested_fingerprint,status,revision,detail,created FROM publication_attempts"
            " WHERE series=? ORDER BY id", (str(series),)
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]
