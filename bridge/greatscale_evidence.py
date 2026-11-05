"""Standalone v1 Great Scale evidence adapter; stdlib only.

SHA-256 is a tamper-evident content digest only when the expected digest is
retained independently. It is not a signature, identity proof, or authorization.
"""
from __future__ import annotations
import hashlib, hmac, json, math, re, unicodedata
from typing import Any

SCHEMA = "avrone.greatscale.evidence/v1"
MAX_RUNS, MAX_REFS, MAX_REF, MAX_DEPTH = 10**9, 32, 160, 4096
CONE = ((1 + math.sqrt(5)) / 2 - 1) ** 2
FIELDS = {"schema","source","kind","run_id","check_id","evidence","refs","capability_id","human_root","provenance_depth","digest"}

class EvidenceContractError(ValueError):
    """Malformed or inconsistent v1 evidence."""

def _text(v: Any, name: str, limit: int) -> str:
    if not isinstance(v,str) or not v or len(v)>limit or v!=v.strip():
        raise EvidenceContractError(f"{name} must be a canonical non-empty string (max {limit})")
    if any(unicodedata.category(c) in {"Cc","Cf","Cs"} for c in v):
        raise EvidenceContractError(f"{name} contains a control/format character")
    return v

def _canonical(v: Any) -> bytes:
    try: return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()
    except (TypeError,ValueError,RecursionError) as e: raise EvidenceContractError(f"not canonical JSON: {e}") from e

def _refs(v: Any) -> list[str]:
    if v is None: return []
    if not isinstance(v,(list,tuple)) or len(v)>MAX_REFS: raise EvidenceContractError("refs exceeds bound or is not a list")
    out=[]
    for item in v:
        item=_text(item,"ref",MAX_REF)
        if item not in out: out.append(item)
    return out

def evidence_snapshot(failures: int, runs: int) -> dict[str,Any]:
    if isinstance(failures,bool) or not isinstance(failures,int) or isinstance(runs,bool) or not isinstance(runs,int):
        raise EvidenceContractError("failures and runs must be integers")
    if not 1<=runs<=MAX_RUNS or not 0<=failures<=runs: raise EvidenceContractError("need 1<=runs<=1e9 and 0<=failures<=runs")
    p=failures/runs; s=p*(1-p); phi=(1+math.sqrt(5))/2; n=8-4*s
    n95=None if p==0 else (1 if p==1 else max(1,math.ceil(math.log(.05)/math.log1p(-p)-1e-12)))
    return {"failures":failures,"runs":runs,"p":p,"S":s,"variance":s,"logCoord":4*s,"psi":CONE+s,"cone":CONE,
            "coneOk":CONE+s>=CONE-1e-12,"onConeFloor":s==0.0,
            "orientation":"pass" if failures==0 else ("fail" if failures==runs else "mixed"),
            "n":n,"lambda":phi**(-n),"singleRunDetect":p,"detectInRuns":1-(1-p)**runs,"runsFor95":n95}

def build_evidence_event(*,source:str,run_id:str,failures:int,runs:int,refs:Any=None,
                         capability_id:str|None=None,human_root:str|None=None,
                         provenance_depth:int=0,check_id:str="default") -> dict[str,Any]:
    source=_text(source,"source",80); run_id=_text(run_id,"run_id",128); check_id=_text(check_id,"check_id",MAX_REF)
    if isinstance(provenance_depth,bool) or not isinstance(provenance_depth,int) or not 0<=provenance_depth<=MAX_DEPTH:
        raise EvidenceContractError("invalid provenance_depth")
    if capability_id is not None: capability_id=_text(capability_id,"capability_id",MAX_REF)
    if human_root is not None: human_root=_text(human_root,"human_root",MAX_REF)
    if provenance_depth>0 and human_root is None: raise EvidenceContractError("positive provenance_depth requires human_root")
    body={"schema":SCHEMA,"source":source,"kind":"greatscale_evidence","run_id":run_id,"check_id":check_id,
          "evidence":evidence_snapshot(failures,runs),"refs":_refs(refs),"capability_id":capability_id,
          "human_root":human_root,"provenance_depth":provenance_depth}
    return {**body,"digest":hashlib.sha256(_canonical(body)).hexdigest()}

def verify_evidence_event(event: Any) -> tuple[bool,str]:
    if not isinstance(event,dict): return False,"event must be an object"
    if set(event)!=FIELDS: return False,"event fields do not match schema v1"
    try:
        if event["schema"]!=SCHEMA or event["kind"]!="greatscale_evidence": raise EvidenceContractError("unsupported schema or kind")
        _text(event["source"],"source",80); _text(event["run_id"],"run_id",128); _text(event["check_id"],"check_id",MAX_REF)
        if _refs(event["refs"])!=event["refs"]: raise EvidenceContractError("refs are not canonical")
        depth=event["provenance_depth"]
        if isinstance(depth,bool) or not isinstance(depth,int) or not 0<=depth<=MAX_DEPTH: raise EvidenceContractError("invalid provenance_depth")
        if event["capability_id"] is not None: _text(event["capability_id"],"capability_id",MAX_REF)
        if event["human_root"] is not None: _text(event["human_root"],"human_root",MAX_REF)
        if depth>0 and event["human_root"] is None: raise EvidenceContractError("positive provenance_depth requires human_root")
        ev=event["evidence"]
        if not isinstance(ev,dict) or _canonical(evidence_snapshot(ev.get("failures"),ev.get("runs")))!=_canonical(ev):
            raise EvidenceContractError("Great Scale evidence does not match measured counts")
        digest=event["digest"]
        if not isinstance(digest,str) or re.fullmatch(r"[0-9a-f]{64}",digest) is None: raise EvidenceContractError("invalid digest format")
        body={k:event[k] for k in FIELDS if k!="digest"}
        if not hmac.compare_digest(hashlib.sha256(_canonical(body)).hexdigest(),digest): raise EvidenceContractError("digest mismatch")
    except (EvidenceContractError,ValueError,TypeError,OverflowError,RecursionError) as e: return False,str(e)
    return True,""
