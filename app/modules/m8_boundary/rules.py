from app.capacity import compute_boundary
from app.report import report


def _num(value):
    return 0 if value is None else value


def rows_for(conclusion):
    """把已发布结论的分段转成模块表格行，页面与证据导出共用同一份数据。"""
    rows = []
    for segment in conclusion.get("segments", []):
        rows.append({
            "product": segment["product"],
            "anchor": segment["anchor"],
            "a": segment["a"],
            "b": segment["b"],
            "capacity": segment["capacity"],
            "protected": segment["protected"],
            "remaining": segment["remaining"],
            "new_seats": segment["new_seats"],
            "new_excess": segment["new_excess"],
            "protected_over": segment["protected_over"],
            "offenders": " ".join(f"{x['id']}:{x['seats']}" for x in segment.get("offenders", [])),
            "status": segment["status"],
            "carried": segment.get("carried", False),
            "source_revision": segment.get("source_revision"),
        })
    return rows


def report_for(conclusion):
    summary = conclusion.get("summary") or {}
    return report(
        rows_for(conclusion),
        产品=summary.get("products", 0),
        生效时刻=summary.get("epochs", 0),
        容量变更=summary.get("capacity_changes", 0),
        受保护峰值=summary.get("protected_peak", 0),
        受保护席位小时=summary.get("protected_seat_hours", 0),
        新占用峰值=summary.get("new_peak", 0),
        剩余容量下限=_num(summary.get("remaining_min")),
        新超用峰值=summary.get("new_excess_peak", 0),
        新超用席位小时=summary.get("new_excess_seat_hours", 0),
    )


def apply(ctx):
    """M8 容量分界：按生效时刻划分受保护旧租约与新占用，只对新占用计超用。"""
    boundary = compute_boundary(ctx)
    ctx["boundary"] = boundary
    ctx["boundary_summary"] = boundary["summary"]
    return report_for(boundary)
