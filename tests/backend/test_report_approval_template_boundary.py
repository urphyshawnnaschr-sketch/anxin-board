from pathlib import Path

from app import report_approval


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_page07_approval_snapshot_does_not_own_anxin_board_template():
    assert "template_version" not in report_approval._COLUMNS
    assert "template_source_filename" not in report_approval._COLUMNS
    assert "template_sha256" not in report_approval._COLUMNS
    assert "disclaimer_version" not in report_approval._COLUMNS
    assert not hasattr(report_approval, "get_anxin_board_current_template_authority")
    assert not hasattr(report_approval, "get_anxin_board_v5_5_template_authority")


def test_page07_frontend_does_not_read_or_name_board_template_authority():
    source = (REPO_ROOT / "apps" / "frontend" / "src" / "views" / "ReportReviewView.vue").read_text(
        encoding="utf-8"
    )
    assert "V5.5" not in source
    assert "approvalSnapshot.template_version" not in source
    assert "template_sha256" not in source
    assert "template_source_filename" not in source
