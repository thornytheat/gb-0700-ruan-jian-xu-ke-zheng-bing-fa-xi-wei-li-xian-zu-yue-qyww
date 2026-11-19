"""容量下调后的席位责任分界（纯计算，可独立测试）。

规则：
- 容量记录每发生一次状态变化（下调、恢复、再次修订、容量区间开始/结束），该时刻形成
  一个“生效时刻”（anchor），从这一刻起新一代容量生效。
- 在生效时刻之前已经开始的租约是“受保护旧租约”（protected）。离线租约即使收到
  release 也保留到原到期时刻，因此永远不会被新容量追溯判为超用；在线旧租约同样
  只按自己的结束时刻退出。
- 受保护旧租约占用的席位从新容量中扣除，剩下的才是“剩余容量”（remaining）。
- 生效时刻及之后开始的租约是“新占用”（new）。只有新占用越过剩余容量才形成新超用
  （new_excess），责任只在新的租约之间分配。
- 受保护旧租约本身就超出新容量的部分记为 protected_over：它说明缺口来自历史承诺，
  不向旧租约追责。恒等式 protected_over + new_excess = max(0, 总并发-容量)。

时间在内部使用 Decimal 秒，输出交给 app.report.plain 统一转字符串。
"""

import hashlib
import json
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from app.report import plain

ZERO = Decimal("0")
HOUR = Decimal("3600")
META_KEYS = {"carried", "source_revision"}


def to_seconds(value):
    """把内部 Decimal 或序列化后的字符串统一成 Decimal 秒。"""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("时间值无法解析")


def canonical_seconds(value):
    """稳定的秒数字符串，用于跨运行比较区间键。"""
    text = format(to_seconds(value).normalize(), "f")
    return "0" if text in {"", "-0"} else text


def seconds_decimal(value):
    return Decimal(canonical_seconds(value))


def _rev(row):
    return int(row.get("rev", row.get("version", 0)))


def _quantity(row):
    return int(row["quantity"])


# ---------------------------------------------------------------------------
# 容量状态与世代
# ---------------------------------------------------------------------------

def capacity_state(rows, at):
    """某时刻的有效容量状态：重叠区间最高 version 优先，同最高版数量冲突取最小值。"""
    at = to_seconds(at)
    valid = [r for r in rows if to_seconds(r["a"]) <= at < to_seconds(r["b"])]
    if not valid:
        return {"present": False, "seats": 0, "version": None, "conflict": False}
    version = max(_rev(r) for r in valid)
    same = [r for r in valid if _rev(r) == version]
    values = sorted({_quantity(r) for r in same})
    return {"present": True, "seats": values[0], "version": version, "conflict": len(values) > 1}


def build_epochs(capacity_rows, lease_rows):
    """按容量状态变化切分世代；每个世代的起点就是它的生效时刻。"""
    points = set()
    for r in list(capacity_rows) + list(lease_rows):
        points.add(to_seconds(r["a"]))
        points.add(to_seconds(r["b"]))
    points = sorted(points)
    epochs = []
    for start, end in zip(points, points[1:]):
        state = capacity_state(capacity_rows, start)
        key = (state["present"], state["seats"], state["version"], state["conflict"])
        last = epochs[-1] if epochs else None
        if last is not None and last["_key"] == key and last["b"] == start:
            last["b"] = end
        else:
            epochs.append({"a": start, "b": end, "anchor": start, "_key": key, **state})
    for epoch in epochs:
        epoch.pop("_key", None)
    return epochs


def allocate_excess(active, extra):
    """只在传入的租约之间分配超用：开始越晚越先承担责任，再按编号倒序。"""
    charged = []
    remaining = int(extra)
    for row in sorted(active, key=lambda r: (to_seconds(r["a"]), str(r.get("lease_id", ""))), reverse=True):
        take = min(remaining, _quantity(row))
        if take:
            charged.append({"id": str(row.get("lease_id", "")), "user": str(row.get("user", "")), "seats": take})
            remaining -= take
        if not remaining:
            break
    return charged


def _seat_hours(duration, seats):
    return (Decimal(duration) * Decimal(seats) / HOUR).quantize(Decimal("0.000001"))


def segments_for_epoch(epoch, leases):
    """在一个世代内部按租约端点切段，区分受保护旧租约与新占用（扫线，O(n log n)）。"""
    start, end, anchor = epoch["a"], epoch["b"], epoch["anchor"]
    events = {}
    for r in leases:
        a, b = to_seconds(r["a"]), to_seconds(r["b"])
        if a >= end or b <= start:
            continue
        left = max(a, start)
        right = min(b, end)
        events.setdefault(left, ([], []))[0].append(r)
        events.setdefault(right, ([], []))[1].append(r)
    cuts = sorted(events)
    capacity = epoch["seats"] if epoch["present"] else 0
    active = []
    segments = []
    for point, following in zip(cuts, cuts[1:]):
        begins, ends = events[point]
        if ends:
            finished = {id(r) for r in ends}
            active = [r for r in active if id(r) not in finished]
        if begins:
            active.extend(begins)
        if not active:
            continue
        protected = [r for r in active if to_seconds(r["a"]) < anchor]
        fresh = [r for r in active if to_seconds(r["a"]) >= anchor]
        protected_seats = sum(_quantity(r) for r in protected)
        new_seats = sum(_quantity(r) for r in fresh)
        remaining = max(0, capacity - protected_seats)
        new_excess = max(0, new_seats - remaining)
        protected_over = max(0, protected_seats - capacity)
        if epoch["conflict"]:
            status = "CAPACITY_CONFLICT"
        elif new_excess:
            status = "OVERUSE"
        elif protected_seats:
            status = "PROTECTED"
        else:
            status = "PASS"
        duration = following - point
        segments.append({
            "product": epoch["product"],
            "anchor": anchor,
            "a": point,
            "b": following,
            "capacity": capacity,
            "capacity_version": epoch["version"],
            "conflict": bool(epoch["conflict"]),
            "concurrent": protected_seats + new_seats,
            "protected": protected_seats,
            "protected_over": protected_over,
            "new_seats": new_seats,
            "remaining": remaining,
            "new_excess": new_excess,
            "protected_seat_hours": _seat_hours(duration, protected_seats),
            "new_excess_seat_hours": _seat_hours(duration, new_excess),
            "protected_ids": sorted(str(r.get("lease_id", "")) for r in protected),
            "offenders": allocate_excess(fresh, new_excess),
            "status": status,
        })
    return segments


def _summarize_epoch(epoch, segments):
    anchor, until = epoch["anchor"], epoch["b"]
    protected_peak = max((s["protected"] for s in segments), default=0)
    new_peak = max((s["new_seats"] for s in segments), default=0)
    excess_peak = max((s["new_excess"] for s in segments), default=0)
    conflict = bool(epoch["conflict"])
    if conflict:
        status = "CAPACITY_CONFLICT"
    elif excess_peak:
        status = "OVERUSE"
    elif protected_peak:
        status = "PROTECTED"
    else:
        status = "PASS"
    return {
        "product": epoch["product"],
        "anchor": anchor,
        "until": until,
        "capacity": epoch["seats"] if epoch["present"] else 0,
        "capacity_version": epoch["version"],
        "conflict": conflict,
        "protected_peak": protected_peak,
        "protected_over_peak": max((s["protected_over"] for s in segments), default=0),
        "protected_seat_hours": sum((s["protected_seat_hours"] for s in segments), ZERO),
        "new_peak": new_peak,
        "remaining_min": min((s["remaining"] for s in segments), default=None),
        "new_excess_peak": excess_peak,
        "new_excess_seat_hours": sum((s["new_excess_seat_hours"] for s in segments), ZERO),
        "protected_ids": sorted({sid for s in segments for sid in s["protected_ids"]}),
        "segments": len(segments),
        "status": status,
    }


def _capacity_basis(rows):
    return [
        {
            "lease_id": str(r.get("lease_id", "")),
            "product": r.get("canonical"),
            "user": str(r.get("user", "")),
            "start": r.get("start"),
            "end": r.get("end"),
            "kind": "capacity",
            "version": _rev(r),
            "seats": _quantity(r),
            "line": r.get("line", 0),
            "a": seconds_decimal(r["a"]),
            "b": seconds_decimal(r["b"]),
        }
        for r in sorted(rows, key=lambda x: (to_seconds(x["a"]), to_seconds(x["b"])))
    ]


def _lease_basis(rows):
    return [
        {
            "lease_id": str(r.get("lease_id", "")),
            "product": r.get("canonical"),
            "user": str(r.get("user", "")),
            "kind": r.get("kind"),
            "start": r.get("start"),
            "end": r.get("end"),
            "version": _rev(r),
            "seats": _quantity(r),
            "line": r.get("line", 0),
            "a": seconds_decimal(r["a"]),
            "b": seconds_decimal(r["b"]),
        }
        for r in sorted(rows, key=lambda x: (to_seconds(x["a"]), str(x.get("lease_id", ""))))
    ]


def series_fingerprint(leases, capacity_rows=()):
    """系列指纹：同一批产品与租约编号视为同一审计系列。

    容量记录（含其编号、版本、数量、区间）不参与指纹，因此容量下调、恢复、再次修订
    即使新增容量行，也仍归入同一系列并形成新发布版本；同一编号的租约补传（改期、
    改席位）同样归入原系列，由新版本记录。
    """
    from app.config import RULE_VERSION

    products = sorted(
        {str(r.get("canonical", "")) for r in list(leases) + list(capacity_rows)}
    )
    lease_ids = sorted(
        {(str(r.get("canonical", "")), str(r.get("lease_id", ""))) for r in leases}
    )
    payload = json.dumps(
        {"rule": RULE_VERSION, "products": products, "leases": lease_ids},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_boundary(ctx):
    """从已裁决租约与已归一容量记录计算完整分界结论（内部为 Decimal 秒）。"""
    return _compute_boundary(ctx)


def _compute_boundary(ctx):
    """从已裁决租约与已归一容量记录计算完整分界结论。"""
    leases = ctx.get("leases", [])
    capacity = [r for r in ctx.get("normalized", []) if r.get("kind") == "capacity"]
    products = sorted({r["canonical"] for r in leases} | {r["canonical"] for r in capacity})
    all_segments, all_epochs, products_out = [], [], []
    protected_seen = {}
    for product in products:
        cap_rows = [r for r in capacity if r["canonical"] == product]
        lease_rows = [r for r in leases if r["canonical"] == product]
        epochs = build_epochs(cap_rows, lease_rows)
        segments, summaries = [], []
        by_id = {}
        for row in lease_rows:
            by_id.setdefault(str(row.get("lease_id", "")), []).append(row)
        for epoch in epochs:
            epoch["product"] = product
            part = segments_for_epoch(epoch, lease_rows)
            segments.extend(part)
            summaries.append(_summarize_epoch(epoch, part))
            protected_here = sorted({sid for piece in part for sid in piece["protected_ids"]})
            for lease_id in protected_here:
                for row in by_id.get(lease_id, []):
                    if to_seconds(row["a"]) >= epoch["anchor"]:
                        continue
                    key = (product, lease_id, canonical_seconds(epoch["anchor"]))
                    if key in protected_seen:
                        continue
                    protected_seen[key] = {
                        "lease_id": lease_id,
                        "product": product,
                        "user": str(row.get("user", "")),
                        "kind": row.get("kind"),
                        "start": row.get("start"),
                        "end": row.get("end"),
                        "version": _rev(row),
                        "seats": _quantity(row),
                        "line": row.get("line", 0),
                        "protected_from": epoch["anchor"],
                        "protected_until": row["b"],
                        "expiry_protected": row.get("kind") == "offline",
                        "reason": "OFFLINE_UNTIL_EXPIRY" if row.get("kind") == "offline" else "PRIOR_START",
                    }
                    break
        product_segments = segments
        all_segments.extend(product_segments)
        all_epochs.extend(summaries)
        products_out.append({
            "product": product,
            "capacity_basis": _capacity_basis(cap_rows),
            "epochs": summaries,
            "segments": product_segments,
            "summary": _summarize_product(product, summaries, segments),
        })
    protected_basis = [protected_seen[k] for k in sorted(protected_seen)]
    basis = {
        "run_digest": ctx.get("digest", ""),
        "rule_version": _rule_version(),
        "series": series_fingerprint(leases, capacity),
        "input_lines": ctx.get("input_count"),
        "capacity_lines": _capacity_basis(capacity),
        "lease_lines": _lease_basis(leases),
    }
    summary = _summarize_all(products_out, all_epochs, all_segments)
    return {
        "basis": basis,
        "summary": summary,
        "products": products_out,
        "epochs": all_epochs,
        "segments": all_segments,
        "protected_basis": protected_basis,
    }


def _rule_version():
    from app.config import RULE_VERSION

    return RULE_VERSION


def _summarize_product(product, epochs, segments):
    protected_ids = {sid for e in epochs for sid in e["protected_ids"]}
    remaining = [e["remaining_min"] for e in epochs if e["remaining_min"] is not None]
    conflicts = sum(1 for e in epochs if e["conflict"])
    breach = any(s["new_excess"] for s in segments)
    if conflicts:
        status = "CAPACITY_CONFLICT"
    elif breach:
        status = "OVERUSE"
    elif protected_ids:
        status = "PROTECTED"
    else:
        status = "PASS"
    return {
        "product": product,
        "epochs": len(epochs),
        "capacity_changes": max(0, len(epochs) - 1),
        "protected_peak": max((e["protected_peak"] for e in epochs), default=0),
        "protected_seat_hours": sum((e["protected_seat_hours"] for e in epochs), ZERO),
        "protected_leases": len(protected_ids),
        "new_peak": max((e["new_peak"] for e in epochs), default=0),
        "remaining_min": min(remaining) if remaining else None,
        "new_excess_peak": max((e["new_excess_peak"] for e in epochs), default=0),
        "new_excess_seat_hours": sum((e["new_excess_seat_hours"] for e in epochs), ZERO),
        "breach": breach,
        "conflicts": conflicts,
        "status": status,
    }


def _summarize_all(products, epochs, segments):
    remaining = [e["remaining_min"] for e in epochs if e["remaining_min"] is not None]
    return {
        "products": len(products),
        "epochs": len(epochs),
        "segments": len(segments),
        "capacity_changes": sum(p["summary"]["capacity_changes"] for p in products),
        "protected_peak": max((p["summary"]["protected_peak"] for p in products), default=0),
        "protected_seat_hours": sum((p["summary"]["protected_seat_hours"] for p in products), ZERO),
        "protected_leases": sum(p["summary"]["protected_leases"] for p in products),
        "new_peak": max((p["summary"]["new_peak"] for p in products), default=0),
        "remaining_min": min(remaining) if remaining else None,
        "new_excess_peak": max((p["summary"]["new_excess_peak"] for p in products), default=0),
        "new_excess_seat_hours": sum((p["summary"]["new_excess_seat_hours"] for p in products), ZERO),
        "breach": any(p["summary"]["breach"] for p in products),
        "conflicts": sum(p["summary"]["conflicts"] for p in products),
    }


# ---------------------------------------------------------------------------
# 发布合并：容量恢复或再次修订时只更新受影响区间
# ---------------------------------------------------------------------------

def _business(value):
    if isinstance(value, Decimal):
        return canonical_seconds(value)
    if isinstance(value, dict):
        return {k: _business(v) for k, v in value.items() if k not in META_KEYS}
    if isinstance(value, list):
        return [_business(v) for v in value]
    return value


def _epoch_key(epoch):
    return (str(epoch.get("product", "")), canonical_seconds(epoch["anchor"]))


def _signature(value):
    return json.dumps(_business(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _previous_revision(previous):
    if not previous:
        return None
    publication = previous.get("publication") or {}
    if publication.get("revision") is not None:
        return publication.get("revision")
    return previous.get("revision")


def _identity_fields(segment):
    """用于判定“结论是否相同”的字段：不含起止与由时长推导的席位小时。"""
    return {
        "product": segment.get("product"),
        "anchor": segment.get("anchor"),
        "capacity": segment.get("capacity"),
        "capacity_version": segment.get("capacity_version"),
        "conflict": segment.get("conflict"),
        "concurrent": segment.get("concurrent"),
        "protected": segment.get("protected"),
        "protected_over": segment.get("protected_over"),
        "new_seats": segment.get("new_seats"),
        "remaining": segment.get("remaining"),
        "new_excess": segment.get("new_excess"),
        "protected_ids": segment.get("protected_ids"),
        "offenders": segment.get("offenders"),
        "status": segment.get("status"),
    }


def _identity_signature(segment):
    return json.dumps(_business(_identity_fields(segment)), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _retime(segment, a, b):
    piece = dict(segment)
    piece["a"] = canonical_seconds(a)
    piece["b"] = canonical_seconds(b)
    duration = to_seconds(b) - to_seconds(a)
    if "protected_seat_hours" in piece:
        piece["protected_seat_hours"] = _seat_hours(duration, piece.get("protected", 0))
    if "new_excess_seat_hours" in piece:
        piece["new_excess_seat_hours"] = _seat_hours(duration, piece.get("new_excess", 0))
    return piece


def _covering(segments, product, x, y):
    for segment in segments:
        if str(segment.get("product", "")) != product:
            continue
        if canonical_seconds(segment["a"]) <= x and y <= canonical_seconds(segment["b"]):
            return segment
    return None


def _splice_segments(previous_segments, computed_segments, prior_revision):
    """按两版区间端点的并集切分，只在内容真正变化的子区间标记为重算。"""
    products = sorted({str(s.get("product", "")) for s in list(previous_segments) + list(computed_segments)})
    pieces, removed = [], []
    for product in products:
        old_rows = [s for s in previous_segments if str(s.get("product", "")) == product]
        new_rows = [s for s in computed_segments if str(s.get("product", "")) == product]
        cuts = set()
        for segment in old_rows + new_rows:
            cuts.add(canonical_seconds(segment["a"]))
            cuts.add(canonical_seconds(segment["b"]))
        ordered = sorted(cuts, key=to_seconds)
        product_pieces = []
        for x, y in zip(ordered, ordered[1:]):
            old = _covering(old_rows, product, x, y)
            new = _covering(new_rows, product, x, y)
            if new is None:
                if old is not None:
                    removed.append(_retime(old, x, y))
                continue
            piece = _retime(new, x, y)
            if old is not None and _identity_signature(old) == _identity_signature(new):
                piece["carried"] = True
                piece["source_revision"] = prior_revision
            else:
                piece["carried"] = False
            product_pieces.append(piece)
        # 合并相邻且同来源、同身份的碎片，避免输出被无意义地切碎
        merged_rows = []
        for piece in product_pieces:
            last = merged_rows[-1] if merged_rows else None
            if (
                last is not None
                and last["carried"] == piece["carried"]
                and last.get("source_revision") == piece.get("source_revision")
                and _identity_signature(last) == _identity_signature(piece)
                and canonical_seconds(last["b"]) == canonical_seconds(piece["a"])
            ):
                merged_rows[-1] = _retime(piece, last["a"], piece["b"])
            else:
                merged_rows.append(piece)
        pieces.extend(merged_rows)
    pieces.sort(key=lambda s: (str(s.get("product", "")), to_seconds(s["a"])))
    return pieces, removed


def merge_publication(previous, computed):
    """把新结论与上一版结论合并：内容未变的区间沿用上一版，变化区间重新计算。"""
    computed = deepcopy(computed)
    prior_revision = _previous_revision(previous)
    if not previous:
        for segment in computed.get("segments", []):
            segment["carried"] = False
        for epoch in computed.get("epochs", []):
            epoch["carried"] = False
        computed["merge"] = {
            "previous_revision": None,
            "recomputed_segments": len(computed.get("segments", [])),
            "carried_segments": 0,
            "removed_segments": [],
            "recomputed_epochs": len(computed.get("epochs", [])),
            "carried_epochs": 0,
            "removed_epochs": [],
            "affected_from": min((s["a"] for s in computed.get("segments", [])), default=None),
            "affected_until": max((s["b"] for s in computed.get("segments", [])), default=None),
        }
        return plain(computed)

    new_segments, removed = _splice_segments(
        previous.get("segments", []), computed.get("segments", []), prior_revision
    )
    for segment in new_segments:
        segment.setdefault("carried", False)
    changed = [s for s in new_segments if not s["carried"]]

    old_epochs = {_epoch_key(e): e for e in previous.get("epochs", [])}
    new_epochs, changed_epochs, seen_epochs = [], [], set()
    for epoch in computed.get("epochs", []):
        key = _epoch_key(epoch)
        seen_epochs.add(key)
        item = dict(epoch)
        old = old_epochs.get(key)
        if old is not None and _signature(old) == _signature(epoch):
            item["carried"] = True
            item["source_revision"] = prior_revision
        else:
            item["carried"] = False
            changed_epochs.append(item)
        new_epochs.append(item)
    removed_epochs = [old_epochs[k] for k in old_epochs if k not in seen_epochs]

    affected_rows = changed + removed
    computed["segments"] = new_segments
    computed["epochs"] = new_epochs
    computed["merge"] = {
        "previous_revision": prior_revision,
        "previous_fingerprint": (previous.get("publication") or {}).get("fingerprint"),
        "recomputed_segments": len(changed),
        "carried_segments": len(new_segments) - len(changed),
        "removed_segments": [
            {"product": r.get("product"), "a": r["a"], "b": r["b"], "anchor": r.get("anchor")} for r in removed
        ],
        "recomputed_epochs": len(changed_epochs),
        "carried_epochs": len(new_epochs) - len(changed_epochs),
        "removed_epochs": [
            {"product": e.get("product"), "anchor": e["anchor"], "until": e["until"]} for e in removed_epochs
        ],
        "affected_from": min((canonical_seconds(r["a"]) for r in affected_rows), default=None),
        "affected_until": max((canonical_seconds(r["b"]) for r in affected_rows), default=None),
    }
    return plain(computed)


def boundary_fingerprint(conclusion):
    """业务指纹：忽略来源行号与发布元数据，同一修订重复发布时得到同一指纹。"""
    basis = conclusion.get("basis", {})
    capacity = [{k: v for k, v in row.items() if k != "line"} for row in basis.get("capacity_lines", [])]
    protected = [{k: v for k, v in row.items() if k != "line"} for row in conclusion.get("protected_basis", [])]
    payload = {
        "series": basis.get("series"),
        "rule_version": basis.get("rule_version"),
        "capacity": capacity,
        "protected": protected,
        "products": conclusion.get("products"),
        "summary": conclusion.get("summary"),
        "segments": conclusion.get("segments"),
        "epochs": conclusion.get("epochs"),
    }
    text = json.dumps(_business(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

