import sqlite3
import pytest
from app import report_progress_corrections as corrections


def item(**changes):
    return dict(module_id="login", stage="开发中", reason="本次删除了登录入口，需要重新开发", evidence_ids=["git:1"], **changes)


def test_corrections_are_bounded_explicit_and_deduplicated():
    assert corrections.normalize(None) == []
    assert corrections.normalize([item()]) == [item()]
    for payload in ([item(), item()], [dict(item(), reason="")], [dict(item(), evidence_ids=[])], [dict(item(), stage="done")]):
        with pytest.raises(corrections.ProgressCorrectionError):
            corrections.normalize(payload)


def test_correction_replay_cannot_change_approved_states():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    approval = {"approval_snapshot_id": 3, "approval_snapshot_hash": "a" * 64}
    conn.execute("BEGIN")
    corrections.persist(conn, approval=approval, values=[item()])
    corrections.assert_replay(conn, approval=approval, values=[item()])
    for changed in ([], [dict(item(), reason="另一个原因")]):
        with pytest.raises(corrections.ProgressCorrectionError):
            corrections.assert_replay(conn, approval=approval, values=changed)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM report_progress_corrections")
    conn.rollback()
    corrections.assert_replay(conn, approval=approval, values=[])


def test_historical_approval_replay_without_corrections_stays_read_only():
    conn = sqlite3.connect(":memory:")
    approval = {"approval_snapshot_id": 3, "approval_snapshot_hash": "a" * 64}
    corrections.assert_replay(conn, approval=approval, values=[])
    with pytest.raises(corrections.ProgressCorrectionError):
        corrections.assert_replay(conn, approval=approval, values=[item()])
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
