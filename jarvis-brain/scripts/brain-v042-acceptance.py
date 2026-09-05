#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone

ODYSSEUS = "odysseus-odysseus-1"
BRAIN = "jarvis-brain-shadow"

def run(*args: str, input_text: str | None = None):
    return subprocess.run(args, text=True, input=input_text, capture_output=True, check=True)

def post(path: str, payload: dict) -> dict:
    code = r"""
import json, os, sys
from urllib.request import Request, urlopen
payload=json.loads(sys.stdin.read())
path=payload.pop("_path")
req=Request(
    "http://jarvis-brain:8765"+path,
    data=json.dumps(payload).encode(),
    method="POST",
    headers={"Authorization":"Bearer "+os.environ["JARVIS_BRAIN_API_KEY"],"Content-Type":"application/json"},
)
with urlopen(req,timeout=10) as r:
    print(r.read().decode())
"""
    body=dict(payload); body["_path"]=path
    p=run("docker","exec","-i",ODYSSEUS,"python","-c",code,input_text=json.dumps(body))
    return json.loads(p.stdout)

def query(sql: str, params: list[str]) -> object:
    code = r"""
import json, os, sqlite3, sys
sql=sys.argv[1]; params=json.loads(sys.stdin.read())
db=sqlite3.connect(os.environ.get("BRAIN_DB_PATH","/data/brain.db")); db.row_factory=sqlite3.Row
rows=db.execute(sql,params).fetchall()
print(json.dumps([dict(r) for r in rows]))
"""
    p=run("docker","exec","-i",BRAIN,"python","-c",code,sql,input_text=json.dumps(params))
    return json.loads(p.stdout)

def wait_job(job_uuid: str, timeout: float = 240.0) -> dict:
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        rows=query("SELECT status,attempt_count,last_error FROM semantic_jobs WHERE uuid=?",[job_uuid])
        if rows and rows[0]["status"] in {"done","failed"}:
            if rows[0]["status"]!="done":
                raise RuntimeError(f"semantic job failed: {rows[0]}")
            return rows[0]
        time.sleep(2)
    raise TimeoutError(job_uuid)

def main() -> None:
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    owner=f"v042-acceptance-{stamp}"; session=f"v042-session-{stamp}"
    first=post("/v1/capture/observation",{
        "owner_id":owner,"raw_text":"My terminal accent preference is turquoise.",
        "external_source_ref":f"{stamp}-turquoise","session_id":session,"source_kind":"USER_MESSAGE",
    })
    wait_job(first["job_uuid"])
    memories=query("SELECT uuid,current_content,status FROM semantic_memories WHERE owner_id=? ORDER BY id",[owner])
    if len(memories)!=1 or "turquoise" not in memories[0]["current_content"].casefold():
        raise RuntimeError(f"CREATE acceptance failed: {memories}")
    memory_uuid=memories[0]["uuid"]
    first_recall=post("/v1/recall",{
        "owner_id":owner,"query":"What is my terminal accent preference?","external_session_ref":session,
    })
    if first_recall["selection_mode"]!="semantic" or first_recall["selected"][0]["uuid"]!=memory_uuid:
        raise RuntimeError(f"first semantic Recall failed: {first_recall}")
    event_uuid=first_recall["recall_event_uuid"]
    post("/v1/recall/mark-injected",{"owner_id":owner,"recall_event_uuid":event_uuid})
    receipt=query("SELECT selection_mode,selected_count,injected,injected_at FROM recall_events WHERE owner_id=? AND uuid=?",[owner,event_uuid])
    if not receipt or not receipt[0]["injected"]:
        raise RuntimeError(f"Recall injection receipt failed: {receipt}")
    second=post("/v1/capture/observation",{
        "owner_id":owner,"raw_text":"I changed my terminal accent preference from turquoise to purple.",
        "external_source_ref":f"{stamp}-purple","session_id":session,"source_kind":"USER_MESSAGE",
    })
    wait_job(second["job_uuid"])
    memories=query("SELECT uuid,current_content,status FROM semantic_memories WHERE owner_id=? ORDER BY id",[owner])
    if len(memories)!=1 or memories[0]["uuid"]!=memory_uuid or "purple" not in memories[0]["current_content"].casefold():
        raise RuntimeError(f"STATE_CHANGE acceptance failed: {memories}")
    revs=query(
        "SELECT r.revision_no,r.operation,r.content FROM memory_revisions r JOIN semantic_memories m ON m.id=r.memory_id WHERE m.owner_id=? AND m.uuid=? ORDER BY r.revision_no",
        [owner,memory_uuid],
    )
    if [r["operation"] for r in revs] != ["CREATE","UPDATE"]:
        raise RuntimeError(f"unexpected revision history: {revs}")
    second_recall=post("/v1/recall",{
        "owner_id":owner,"query":"What is my terminal accent preference now?","external_session_ref":session,
    })
    selected=second_recall["selected"][0]
    if second_recall["selection_mode"]!="semantic" or selected["uuid"]!=memory_uuid or selected["revision_no"]!=2 or "purple" not in selected["text"].casefold():
        raise RuntimeError(f"second semantic Recall failed: {second_recall}")
    print(json.dumps({
        "owner":owner,"memory_uuid":memory_uuid,"revision_count":len(revs),
        "first_recall_event_uuid":event_uuid,"first_recall_injected":True,
        "second_selection_mode":second_recall["selection_mode"],"second_revision_no":selected["revision_no"],
        "status":"PASS",
    },indent=2,sort_keys=True))

if __name__=="__main__":
    main()
