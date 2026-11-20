"""窗外拒收口径：夹具复现 → 双影失败/同键争用红测 → 乙路径全绿。

口径选定：乙路径（先写 reject_draft，校验窗外条件，再原子出示；
失败则草稿作废、事件表零改）。两路不得混用，本文件即判官。
"""
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _raw(tmp_path, sql, params=()):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _auth(c, username="admin", password="123456"):
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _plant_orphans(tmp_path):
    """把复现夹具灌进库：窗外开启却无凭证 + 凭证有事件无。"""
    data = json.loads((FIXTURES / "reject_orphans.json").read_text(encoding="utf-8"))
    for e in data["orphan_events"]:
        _raw(
            tmp_path,
            """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                e.get("window_id"),
                e["biz_key"],
                e["event_ts"],
                e["amount"],
                e["disposition"],
                e["created_at"],
            ),
        )
    for s in data["orphan_slips"]:
        _raw(
            tmp_path,
            """INSERT INTO reject_slips(window_name,biz_key,event_ts,amount,reason,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                s["window_name"],
                s["biz_key"],
                s["event_ts"],
                s["amount"],
                s["reason"],
                s["created_at"],
            ),
        )
    return data


def _out_of_window(c, h, biz_key, amount=9):
    return c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": biz_key,
            "event_ts": "2026-10-01T00:00:00",
            "amount": amount,
        },
    )


# ---------- 夹具复现 ----------


def test_orphan_fixture_reproduces_inconsistency(tmp_path):
    """夹具必须真能造出两种孤儿：窗外开启却无凭证、凭证有事件无。"""
    _plant_orphans(tmp_path)
    ev = _raw(tmp_path, "SELECT * FROM events WHERE biz_key='ORPH-EVT'")
    assert len(ev) == 1 and ev[0]["disposition"] == "拒收"  # 事件有
    assert _raw(tmp_path, "SELECT * FROM reject_slips WHERE biz_key='ORPH-EVT'") == []  # 凭证无
    slip = _raw(tmp_path, "SELECT * FROM reject_slips WHERE biz_key='ORPH-SLIP'")
    assert len(slip) == 1  # 凭证有
    assert _raw(tmp_path, "SELECT * FROM events WHERE biz_key='ORPH-SLIP'") == []  # 事件无


# ---------- 红测一：拒收双影失败 ----------


def test_reject_double_write_failure_zero_increment(tmp_path):
    """出示失败必须正式零增：事件表零改、凭证零增，草稿作废留痕。"""
    c = TestClient(app, raise_server_exceptions=False)
    h = _auth(c)
    _raw(
        tmp_path,
        """CREATE TRIGGER boom_slip BEFORE INSERT ON reject_slips
           WHEN NEW.biz_key='BOOM-SLIP'
           BEGIN SELECT RAISE(ABORT, 'slip boom'); END""",
    )
    before_ev = len(_raw(tmp_path, "SELECT * FROM events"))
    before_slip = len(_raw(tmp_path, "SELECT * FROM reject_slips"))
    r = _out_of_window(c, h, "BOOM-SLIP")
    assert r.status_code in (409, 500)
    # 正式零增：两侧都不许多出半点影子
    assert _raw(tmp_path, "SELECT * FROM events WHERE biz_key='BOOM-SLIP'") == []
    assert len(_raw(tmp_path, "SELECT * FROM events")) == before_ev
    assert len(_raw(tmp_path, "SELECT * FROM reject_slips")) == before_slip
    # 乙路径留痕：影子拒收单作废，可核证
    drafts = _raw(tmp_path, "SELECT * FROM reject_drafts WHERE biz_key='BOOM-SLIP'")
    assert len(drafts) == 1
    assert drafts[0]["status"] == "作废"


# ---------- 红测二：双人同时打同一键 ----------


def test_same_biz_key_contention_only_records(tmp_path):
    """同一 biz_key 须占入账锁；后到只记争用，不写业务。"""
    c = TestClient(app)
    h1 = _auth(c, "admin", "123456")
    h2 = _auth(c, "closer", "close123")
    r = c.post(
        "/api/locks/acquire",
        headers=h1,
        json={"lock_key": "ingest:W-1102:ORD-CONT"},
    )
    assert r.status_code == 200
    r2 = c.post(
        "/api/events",
        headers=h2,
        json={
            "window_name": "W-1102",
            "biz_key": "ORD-CONT",
            "event_ts": "2026-11-02T10:00:00",
            "amount": 5,
        },
    )
    assert r2.status_code == 409
    cont = _raw(
        tmp_path,
        "SELECT * FROM contention_log WHERE lock_key='ingest:W-1102:ORD-CONT'",
    )
    assert len(cont) == 1
    assert cont[0]["actor"] == "closer"
    assert _raw(tmp_path, "SELECT * FROM events WHERE biz_key='ORD-CONT'") == []


# ---------- 其余口径红测（齐备后才准改写路径） ----------


def test_startup_leaves_no_dirty_rows(tmp_path):
    """全绿后正式无窗外脏行：启动修复须清掉种子/历史拒收脏行。"""
    assert _raw(tmp_path, "SELECT * FROM events WHERE disposition='拒收'") == []


def test_repair_orphans_via_reconcile(tmp_path):
    """对账写路径修复孤儿：事件有凭证无 → 补出示 + 删脏行；凭证有事件无不动。"""
    _plant_orphans(tmp_path)
    c = TestClient(app)
    h = _auth(c)
    r = c.post("/api/reconcile/repair-orphans", headers=h)
    assert r.status_code == 200
    assert r.json()["repaired_events"] >= 1
    assert _raw(tmp_path, "SELECT * FROM events WHERE disposition='拒收'") == []
    slips = _raw(tmp_path, "SELECT * FROM reject_slips WHERE biz_key='ORPH-EVT'")
    assert len(slips) == 1
    assert slips[0]["window_name"] == "W-1102"
    assert len(_raw(tmp_path, "SELECT * FROM reject_slips WHERE biz_key='ORPH-SLIP'")) == 1


def test_register_deviation_refused(tmp_path):
    """偏离窗外拒收册：拒写并记拒收偏了；当天禁止改口。"""
    _raw(
        tmp_path,
        "INSERT INTO reject_register(day,path,decided_by,created_at) VALUES (?,?,?,?)",
        (date.today().isoformat(), "甲", "admin", "2026-11-20T00:00:00"),
    )
    c = TestClient(app)
    h = _auth(c)
    r = _out_of_window(c, h, "ORD-DEVIATE")
    assert r.status_code == 409
    audits = _raw(tmp_path, "SELECT * FROM audit_trail WHERE action='拒收偏了'")
    assert len(audits) == 1
    assert _raw(tmp_path, "SELECT * FROM reject_slips WHERE biz_key='ORD-DEVIATE'") == []
    assert _raw(tmp_path, "SELECT * FROM reject_drafts WHERE biz_key='ORD-DEVIATE'") == []
    assert _raw(tmp_path, "SELECT * FROM events WHERE biz_key='ORD-DEVIATE'") == []


def test_five_surfaces_consistent_after_reject(tmp_path):
    """拒收凭证、事件列表、窗口条带、对账拒收计数、审计五处同态。"""
    c = TestClient(app)
    h = _auth(c)
    r = _out_of_window(c, h, "ORD-REJ")
    assert r.status_code == 409
    # 1) 拒收凭证可查
    slips = c.get("/api/rejects", headers=h).json()["items"]
    assert len([s for s in slips if s["biz_key"] == "ORD-REJ"]) == 1
    # 2) 事件列表：正式无窗外脏行
    evs = c.get("/api/events", headers=h).json()["items"]
    assert [e for e in evs if e["biz_key"] == "ORD-REJ"] == []
    assert [e for e in evs if e["disposition"] == "拒收"] == []
    # 3) 窗口条带
    wins = {w["name"]: w for w in c.get("/api/windows", headers=h).json()["items"]}
    assert wins["W-1102"]["reject_count"] == 1
    # 4) 对账拒收计数（单窗与全量同口径）
    assert c.get("/api/reconcile/2", headers=h).json()["rejects"] == 1
    rec_all = {x["name"]: x for x in c.get("/api/reconcile", headers=h).json()["items"]}
    assert rec_all["W-1102"]["rejects"] == 1
    # 5) 审计
    audits = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "ingest_reject" and a["target"] == "ORD-REJ" for a in audits)
    # 核验端点总判
    cons = c.get("/api/audit/reject-consistency", headers=h).json()
    assert cons["consistent"] is True
    assert cons["dirty_events"] == 0
