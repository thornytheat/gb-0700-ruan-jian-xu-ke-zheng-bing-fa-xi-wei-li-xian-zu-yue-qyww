from app.errors import BusinessError, object_payload
from fastapi import APIRouter, Body
from app.modules.m8_boundary.service import execute
router=APIRouter()

def _text(payload):
    payload=object_payload(payload)
    from app.pipeline import sample_text
    text=payload.get("text",sample_text())
    if not isinstance(text,str):
        raise BusinessError("TEXT","输入须为文本")
    return text

@router.post("/api/8")
def calculate(payload:object=Body(default=None)):
    return execute(_text(payload))

@router.get("/api/8")
def sample():
    """独立样例：只计算并展示分界，不发布（GET 无副作用）。"""
    from app.pipeline import run,sample_text
    from app.modules.m8_boundary.rules import report_for
    result=run(sample_text())
    return {"module":8,"report":report_for(result["boundary"]),"quarantine":result["quarantine"],
            "balance":result["balance"],"boundary":result["boundary"],"publication":None,"idempotent":False}
