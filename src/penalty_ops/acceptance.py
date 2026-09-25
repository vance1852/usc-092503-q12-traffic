"""离线命令行验收入口。"""
from __future__ import annotations
import argparse,json,sqlite3
from datetime import datetime, timezone
from .models import ViolationRecord,CaseRecord
from .service import PenaltyService

def _appeal_acceptance():
    """申诉复核全流程验收:登记→补正→受理→变更→送达。"""
    from .appeal_service import AppealService
    from .clock import FrozenClock
    connection=sqlite3.connect(":memory:",isolation_level=None); connection.row_factory=sqlite3.Row
    clock=FrozenClock(datetime(2026,9,25,8,0,tzinfo=timezone.utc)); service=AppealService(connection,clock)
    for user_id,role in (("officer1","officer"),("party1","party"),("handler1","handler"),("reviewer1","reviewer"),("auditor1","auditor")):
        service.create_user(user_id,user_id,role)
    service.register_decision("officer1",{"decision_id":"D-DEMO","case_record_id":"CASE-DEMO","party_id":"party1","violation_summary":"违反交通信号灯","legal_basis":"道路交通安全法第三十八条","fine_amount_cny":"20000","evidence":[{"evidence_id":"EV-1","evidence_version":"v2"}]})
    appeal=service.file_appeal("party1",{"decision_id":"D-DEMO","legal_ground":"disproportionate_punishment","statement":"量罚过重","materials":[{"kind":"身份证明","content":"身份证复印件"}]})
    appeal_id=appeal["appeal_id"]
    service.request_material_correction("handler1",appeal_id,"需补充行驶记录","材料不齐全")
    service.submit_supplement("party1",appeal_id,[{"kind":"行驶记录","content":"当日行车记录仪数据"}])
    service.review_acceptance("reviewer1",appeal_id,True,"材料齐全,予以受理")
    suspended=len(service.dunning_queue("officer1")["suspended"])
    service.decide_reconsideration("reviewer1",appeal_id,{"conclusion":"modify","reason":"量罚过重,变更罚款","new_fine_amount_cny":"10000","evidence_citations":[{"evidence_id":"EV-1","evidence_version":"v2"}]})
    service.record_delivery("handler1",appeal_id,"electronic","party1")
    detail=service.appeal_detail("auditor1",appeal_id); decision=service.decision("officer1","D-DEMO")
    connection.close()
    return {"appeal_id":appeal_id,"state":detail["appeal"]["state"],"suspended_actions":suspended,"decision_status":decision["decision"]["status"],"ledger_statuses":[e["status"] for e in decision["ledger"]],"delivered":detail["delivery"] is not None}

def run():
    s=PenaltyService(); s.bootstrap(); t=s.auth.login("admin","enforcement-admin"); s.register_case_record(t,CaseRecord("CASE-DEMO","north","water",680,5)); r=s.ingest_violation_record(t,ViolationRecord("RD-DEMO","CASE-DEMO","evidence_source-01",160,230,88,"2026-09-24T10:00:00+00:00")); report=s.risk_report(t,"CASE-DEMO"); order=s.create_case_ticket(t,"CASE-DEMO",r["alert_id"],"crew-north",1); s.add_response_resource(t,"PUMP-01","mobile-pump","north",2); allocation=s.allocate(t,"PUMP-01",order["case_ticket_id"],1); return {"status":"ok","case_record":"CASE-DEMO","severity":r["risk"]["severity"],"probability":report["violation_probability"],"allocation":allocation["plan_id"],"appeal":_appeal_acceptance()}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()
