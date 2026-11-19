import sqlite3, json
from datetime import datetime, timezone
from app.config import DATA_DIR, RULE_VERSION, digest_of
from app.errors import BusinessError
from app import publish
from app.modules.m8_boundary.rules import report_for

def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"runs.sqlite", timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)")
    return db

def _sync_report(result):
    """页面与导出都读已发布结论；M8 表格同步为该结论，避免两处口径不一。"""
    boundary=result.get("boundary")
    if isinstance(boundary,dict) and result.get("reports"):
        reports=list(result["reports"])
        reports[-1]=report_for(boundary)
        result["reports"]=reports
    return result

def _attach_publication(result, text):
    """发布容量分界结论；失败时回退到上一版完整结论，并保留本次失败记录。"""
    boundary=result.get("boundary")
    if not isinstance(boundary,dict):
        return result
    try:
        published=publish.publish(boundary, run_id=result.get("digest","")[:16])
    except publish.PublishInterrupted as exc:
        series=(boundary.get("basis") or {}).get("series")
        fallback=publish.get_or_none(series)
        result["boundary_status"]="failed"
        result["boundary_error"]=str(exc)
        result["boundary_unpublished"]=boundary
        if fallback is not None:
            result["boundary"]=fallback["conclusion"]
            result["publication"]=fallback["publication"]
            result["boundary_status"]="stale"
        return _sync_report(result)
    result["boundary"]=published["conclusion"]
    result["publication"]=published["publication"]
    result["boundary_status"]="published"
    return _sync_report(result)

def save(text, result):
    digest=digest_of(text)
    ident=digest[:16]
    result=_attach_publication(dict(result), text)
    payload=json.dumps(dict(result,rule_version=RULE_VERSION),ensure_ascii=False)
    with connect() as db:
        db.execute(
            "INSERT INTO runs(id,digest,result,created) VALUES(?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET result=excluded.result",
            (ident,digest,payload,datetime.now(timezone.utc).isoformat()),
        )
    return get(ident)

def get(ident):
    with connect() as db:
        row=db.execute("SELECT * FROM runs WHERE id=?",(ident,)).fetchone()
    if row is None: raise BusinessError("NOT_FOUND","批次不存在",404)
    item=dict(row);item["result"]=json.loads(item["result"]);item["rule_version"]=item["result"].get("rule_version","2026.11.18.1")
    return item

def history():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,revision,decision,created FROM runs ORDER BY created DESC LIMIT 40")]

def review(ident,revision,decision):
    if not isinstance(decision,str) or decision not in {"通过","退回"}: raise BusinessError("DECISION","复核决定无效")
    current=get(ident)
    if decision=="通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED","存在阻断项，请先修正数据",409)
    with connect() as db:
        changed=db.execute("UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",(decision,ident,revision)).rowcount
        if not changed: raise BusinessError("STALE","版本已更新，请刷新",409)
    return get(ident)
