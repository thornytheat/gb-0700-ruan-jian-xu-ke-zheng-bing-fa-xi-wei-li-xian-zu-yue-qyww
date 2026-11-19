import csv,io,json

def _num(value):
    return 0 if value is None else value

def _line(run,kind,line,reason,evidence):
    return [run["id"],run["digest"],run["rule_version"],kind,line,reason,json.dumps(evidence,ensure_ascii=False,sort_keys=True)]

def evidence_csv(run):
    out=io.StringIO();writer=csv.writer(out)
    writer.writerow(["run_id","sha256","rule_version","kind","line","reason","evidence"])
    result=run["result"]
    publication=result.get("publication") or {}
    writer.writerow(_line(run,"manifest","","",{"balance":result.get("balance"),"decision":run["decision"],"revision":run["revision"],
        "boundary_status":result.get("boundary_status"),"boundary_error":result.get("boundary_error"),
        "series":publication.get("series"),"publication_revision":publication.get("revision"),
        "fingerprint":publication.get("fingerprint"),"idempotent":publication.get("idempotent")}))
    boundary=result.get("boundary")
    if isinstance(boundary,dict):
        summary=boundary.get("summary") or {}
        writer.writerow(_line(run,"boundary-summary","","",{
            "series":publication.get("series"),"publication_revision":publication.get("revision"),
            "products":summary.get("products"),"epochs":summary.get("epochs"),
            "capacity_changes":summary.get("capacity_changes"),
            "protected_peak":summary.get("protected_peak"),
            "protected_seat_hours":summary.get("protected_seat_hours"),
            "protected_leases":summary.get("protected_leases"),
            "new_peak":summary.get("new_peak"),
            "remaining_min":summary.get("remaining_min"),
            "new_excess_peak":summary.get("new_excess_peak"),
            "new_excess_seat_hours":summary.get("new_excess_seat_hours"),
            "conflicts":summary.get("conflicts"),"breach":summary.get("breach")}))
        merge=boundary.get("merge") or {}
        writer.writerow(_line(run,"boundary-merge","","",{
            "previous_revision":merge.get("previous_revision"),
            "revision":merge.get("revision"),
            "recomputed_segments":merge.get("recomputed_segments"),
            "carried_segments":merge.get("carried_segments"),
            "removed_segments":merge.get("removed_segments"),
            "recomputed_epochs":merge.get("recomputed_epochs"),
            "carried_epochs":merge.get("carried_epochs"),
            "removed_epochs":merge.get("removed_epochs"),
            "affected_from":merge.get("affected_from"),
            "affected_until":merge.get("affected_until")}))
        for segment in boundary.get("segments",[]):
            writer.writerow(_line(run,"boundary-segment",segment.get("a"),segment.get("status",""),{
                "product":segment.get("product"),"anchor":segment.get("anchor"),"a":segment.get("a"),"b":segment.get("b"),
                "capacity":segment.get("capacity"),"capacity_version":segment.get("capacity_version"),
                "protected":segment.get("protected"),"protected_over":segment.get("protected_over"),
                "remaining":segment.get("remaining"),"new_seats":segment.get("new_seats"),
                "new_excess":segment.get("new_excess"),
                "protected_seat_hours":segment.get("protected_seat_hours"),
                "new_excess_seat_hours":segment.get("new_excess_seat_hours"),
                "protected_ids":segment.get("protected_ids"),"offenders":segment.get("offenders"),
                "carried":segment.get("carried"),"source_revision":segment.get("source_revision")}))
        for epoch in boundary.get("epochs",[]):
            writer.writerow(_line(run,"boundary-epoch",epoch.get("anchor"),epoch.get("status",""),{
                "product":epoch.get("product"),"anchor":epoch.get("anchor"),"until":epoch.get("until"),
                "capacity":epoch.get("capacity"),"capacity_version":epoch.get("capacity_version"),
                "protected_peak":epoch.get("protected_peak"),"protected_over_peak":epoch.get("protected_over_peak"),
                "protected_seat_hours":epoch.get("protected_seat_hours"),"new_peak":epoch.get("new_peak"),
                "remaining_min":epoch.get("remaining_min"),"new_excess_peak":epoch.get("new_excess_peak"),
                "new_excess_seat_hours":epoch.get("new_excess_seat_hours"),
                "protected_ids":epoch.get("protected_ids"),"carried":epoch.get("carried"),
                "source_revision":epoch.get("source_revision")}))
        for entry in boundary.get("protected_basis",[])+boundary.get("retained_protected",[]):
            writer.writerow(_line(run,"boundary-protected",entry.get("line",entry.get("protected_from","")),
                "RETAINED" if entry.get("retained") else entry.get("reason",""),
                {"lease_id":entry.get("lease_id"),"product":entry.get("product"),"user":entry.get("user"),
                 "kind":entry.get("kind"),"start":entry.get("start"),"end":entry.get("end"),
                 "version":entry.get("version"),"seats":entry.get("seats"),
                 "protected_from":entry.get("protected_from"),"protected_until":entry.get("protected_until"),
                 "expiry_protected":entry.get("expiry_protected"),"retained":entry.get("retained"),
                 "retained_from_revision":entry.get("retained_from_revision")}))
        basis=boundary.get("basis") or {}
        for row in basis.get("capacity_lines",[]):
            writer.writerow(_line(run,"boundary-capacity-line",row.get("line",""),"CAPACITY_BASIS",{
                "lease_id":row.get("lease_id"),"product":row.get("product"),"version":row.get("version"),
                "seats":row.get("seats"),"a":row.get("a"),"b":row.get("b"),"start":row.get("start"),"end":row.get("end")}))
    for row in result.get("final",[]):
        writer.writerow(_line(run,"result",row.get("line",""),row.get("status",""),row))
    for row in result.get("quarantine",[]):
        writer.writerow(_line(run,"quarantine",row.get("line",""),row["reason"],row))
    return "﻿"+out.getvalue()
