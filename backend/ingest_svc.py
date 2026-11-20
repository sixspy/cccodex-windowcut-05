"""事件入账：窗内 / 拒收 / 迟到桶分流。

窗外拒收口径（选定后写入窗外拒收册，当天禁止改口）：
乙路径 —— 先写 reject_draft 影子拒收单，校验窗外条件，再原子出示；
失败则草稿作废、事件表零改。两路不得混用，偏离窗外拒收册拒写并记拒收偏了。
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect, write_audit
from locks import release, require_lock
from models import EventIn

router = APIRouter(tags=["ingest"])

# 本仓选定的窗外拒收口径：乙路径（影子拒收单原子出示）。禁止与甲路径混用。
REJECT_PATH = "乙"


class _VoidDraft(Exception):
    """窗外条件在出示前已不成立：草稿作废。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_ts(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(400, f"事件时间格式非法: {raw}") from exc


def _ensure_register(conn, user: str) -> None:
    """窗外拒收册：首次拒收选定口径落册；偏离已登记口径则拒写并记拒收偏了。"""
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT * FROM reject_register WHERE day=?", (today,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO reject_register(day,path,decided_by,created_at) VALUES (?,?,?,?)",
            (today, REJECT_PATH, user, _now()),
        )
        conn.commit()
        return
    if row["path"] != REJECT_PATH:
        write_audit(
            conn,
            user,
            "拒收偏了",
            None,
            {"registered": row["path"], "attempted": REJECT_PATH, "day": today},
        )
        conn.commit()
        raise HTTPException(409, "偏离窗外拒收册：拒写（已记拒收偏了）")


def _reject_outside_open_window(conn, user: str, body: EventIn, ts: str) -> None:
    """乙路径：草稿 → 校验窗外条件 → 原子出示；失败草稿作废、事件表零改。"""
    _ensure_register(conn, user)
    created = _now()
    # T1：影子拒收单落库（可核证的起点）
    cur = conn.execute(
        """INSERT INTO reject_drafts(window_name,biz_key,event_ts,amount,reason,status,created_at)
           VALUES (?,?,?,?,?,'草稿',?)""",
        (body.window_name, body.biz_key, ts, body.amount, "窗外且窗仍开启", created),
    )
    draft_id = cur.lastrowid
    conn.commit()
    # T2：校验窗外条件 + 原子出示（凭证、草稿状态、审计同一事务）
    try:
        w = conn.execute(
            "SELECT * FROM windows WHERE name=?", (body.window_name,)
        ).fetchone()
        open_at = _parse_ts(w["open_at"]) if w else None
        close_at = _parse_ts(w["close_at"]) if w else None
        if (
            not w
            or w["status"] != "开启"
            or open_at <= _parse_ts(ts) <= close_at
        ):
            raise _VoidDraft("窗外条件已不成立")
        cur = conn.execute(
            """INSERT INTO reject_slips(window_name,biz_key,event_ts,amount,reason,created_at)
               VALUES (?,?,?,?,?,?)""",
            (body.window_name, body.biz_key, ts, body.amount, "窗外且窗仍开启", created),
        )
        slip_id = cur.lastrowid
        conn.execute(
            "UPDATE reject_drafts SET status='已出示', slip_id=?, presented_at=? WHERE id=?",
            (slip_id, _now(), draft_id),
        )
        write_audit(
            conn,
            user,
            "ingest_reject",
            body.biz_key,
            {"window": body.window_name, "draft_id": draft_id, "slip_id": slip_id},
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        # T3：出示失败 —— 草稿作废，事件表零改，凭证零增
        conn.rollback()
        conn.execute(
            "UPDATE reject_drafts SET status='作废', void_reason=? WHERE id=?",
            (str(exc), draft_id),
        )
        write_audit(
            conn,
            user,
            "reject_void",
            body.biz_key,
            {"draft_id": draft_id, "reason": str(exc)},
        )
        conn.commit()
        if isinstance(exc, _VoidDraft):
            raise HTTPException(409, f"{exc}：草稿已作废") from exc
        raise HTTPException(500, "拒收出示失败：草稿已作废，事件表零改") from exc
    raise HTTPException(409, f"事件时间落在窗外且窗仍开启：已拒收（凭证#{slip_id}）")


@router.post("/api/events")
def ingest_event(body: EventIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    # 入账锁按 biz_key 占：两路同时写入同一键，后到只记争用。
    lock_key = f"ingest:{body.window_name}:{body.biz_key}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute(
                "SELECT * FROM windows WHERE name=?", (body.window_name,)
            ).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            ts = _parse_ts(body.event_ts).isoformat(timespec="seconds")
            created = _now()
            if w["status"] == "已关闭":
                cur = conn.execute(
                    """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (None, body.biz_key, ts, body.amount, "迟到桶", created),
                )
                eid = cur.lastrowid
                conn.execute(
                    """INSERT INTO late_bucket(event_id,window_name,biz_key,event_ts,amount,reason,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        eid,
                        body.window_name,
                        body.biz_key,
                        ts,
                        body.amount,
                        "关窗后到达",
                        created,
                    ),
                )
                write_audit(
                    conn,
                    user,
                    "ingest_late",
                    body.biz_key,
                    {"window": body.window_name},
                )
                conn.commit()
                return {"disposition": "迟到桶", "event_id": eid}

            open_at = _parse_ts(w["open_at"])
            close_at = _parse_ts(w["close_at"])
            if not (open_at <= _parse_ts(ts) <= close_at):
                _reject_outside_open_window(conn, user, body, ts)

            cur = conn.execute(
                """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (w["id"], body.biz_key, ts, body.amount, "窗内", created),
            )
            write_audit(
                conn,
                user,
                "ingest_in_window",
                body.biz_key,
                {"window_id": w["id"]},
            )
            conn.commit()
            return {"disposition": "窗内", "window_id": w["id"], "event_id": cur.lastrowid}
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            raise HTTPException(500, f"入账失败: {exc}") from exc
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/events")
def list_events(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/rejects")
def list_rejects(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reject_slips ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/reject-drafts")
def list_reject_drafts(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reject_drafts ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/reject-register")
def get_reject_register(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reject_register ORDER BY id DESC LIMIT 30"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
