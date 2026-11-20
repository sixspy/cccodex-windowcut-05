"""审计与互证只读旁路。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def audit_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/reject-consistency")
def reject_consistency(authorization: AuthHeader = None):
    """窗外拒收五处同态核验：凭证 / 事件列表 / 窗口条带 / 对账计数 / 审计。"""
    require_user(authorization)
    with connect() as conn:
        slips = conn.execute("SELECT COUNT(*) c FROM reject_slips").fetchone()["c"]
        dirty = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE disposition='拒收'"
        ).fetchone()["c"]
        band = conn.execute(
            """SELECT COALESCE(SUM(cnt),0) s FROM (
                 SELECT COUNT(*) cnt FROM reject_slips rs
                 JOIN windows w ON w.name = rs.window_name
                 GROUP BY w.name
               )"""
        ).fetchone()["s"]
        recon = conn.execute(
            "SELECT COUNT(*) c FROM reject_slips WHERE window_name IN (SELECT name FROM windows)"
        ).fetchone()["c"]
        audit_n = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='ingest_reject'"
        ).fetchone()["c"]
        drafts = {
            r["status"]: r["c"]
            for r in conn.execute(
                "SELECT status, COUNT(*) c FROM reject_drafts GROUP BY status"
            ).fetchall()
        }
        consistent = dirty == 0 and slips == band == recon == audit_n
        return {
            "slips": slips,
            "dirty_events": dirty,
            "window_band_total": band,
            "reconcile_total": recon,
            "audit_presentations": audit_n,
            "drafts": drafts,
            "consistent": consistent,
        }


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='开启'"
        ).fetchone()["c"]
        closed_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='已关闭'"
        ).fetchone()["c"]
        late_n = conn.execute("SELECT COUNT(*) c FROM late_bucket").fetchone()["c"]
        reject_n = conn.execute("SELECT COUNT(*) c FROM reject_slips").fetchone()["c"]
        hold_n = conn.execute(
            "SELECT COUNT(*) c FROM hold_staging WHERE status='暂存'"
        ).fetchone()["c"]
        mats = conn.execute("SELECT COUNT(*) c FROM materializations").fetchone()["c"]
        contention = conn.execute("SELECT COUNT(*) c FROM contention_log").fetchone()["c"]
        return {
            "open_windows": open_n,
            "closed_windows": closed_n,
            "late_bucket": late_n,
            "rejects": reject_n,
            "holds": hold_n,
            "materializations": mats,
            "contention": contention,
        }
