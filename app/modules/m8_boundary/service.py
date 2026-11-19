def execute(text):
    from app.pipeline import run
    from app import publish
    from app.modules.m8_boundary.rules import report_for
    result = run(text)
    boundary = result["boundary"]
    published = publish.publish(boundary, run_id=result.get("digest", "")[:16])
    conclusion = published["conclusion"]
    return {
        "module": 8,
        "report": report_for(conclusion),
        "quarantine": result["quarantine"],
        "balance": result["balance"],
        "boundary": conclusion,
        "publication": published["publication"],
        "idempotent": published["idempotent"],
    }
