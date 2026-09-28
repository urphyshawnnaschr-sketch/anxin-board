import pytest
from fastapi import HTTPException

from app import project_profile_generation as subject


FEATURES = [
    {"name": "报告审批"},
    {"name": "邮件发送"},
]


def test_prompt_requires_prd_code_reconciliation_and_exact_path_binding():
    messages, allowed = subject._build_prompt(
        project={"name": "安心看板"},
        prd_text="PRD evidence",
        prd_evidence_id="prd-1",
        repo_items=[
            {
                "evidence_id": "repo_file:apps/backend/app/report.py:abc",
                "path": "apps/backend/app/report.py",
                "content": "def approve_report(): pass",
            }
        ],
    )

    system = messages[0]["content"]
    assert "真实源码逐项对账" in system
    assert "而不是照抄 PRD" in system
    assert "仅 PRD 提到但源码中找不到实现证据的需求" in system
    assert "不得放进 features 冒充已实现" in system
    assert "源码中确实存在但 PRD 未覆盖的真实功能可以进入 features" in system
    assert "[feature:<功能精确名称>]" in system
    assert "必须逐字等于 features 中唯一一项的 name" in system
    assert allowed == {"prd-1", "repo_file:apps/backend/app/report.py:abc"}


def test_exact_feature_marker_binds_without_fuzzy_name_repetition():
    description = "[feature:报告审批] 该路径实现状态流转与确认写入。"

    assert subject._unique_feature_match(description, FEATURES) == 0
    assert subject._path_description_body(description) == "该路径实现状态流转与确认写入。"


def test_invalid_explicit_marker_fails_closed_even_if_body_mentions_valid_feature():
    description = "[feature:不存在的功能] 这里虽然提到了报告审批，但不能猜测归组。"

    assert subject._unique_feature_match(description, FEATURES) is None


def test_legacy_unmarked_description_keeps_unambiguous_backward_compatibility():
    assert subject._unique_feature_match("报告审批的后端实现", FEATURES) == 0


def test_duplicate_exact_feature_names_are_not_silently_assigned():
    duplicate_features = [{"name": "报告审批"}, {"name": "报告审批"}]

    assert subject._unique_feature_match("[feature:报告审批] 路径说明", duplicate_features) is None


def _repo_items():
    return [
        {
            "evidence_id": "repo-tree-aaaaaaaaaaaaaaaaaaaaaaaa",
            "path": "<exact-head-tracked-tree>",
            "content": "tree",
        },
        {
            "evidence_id": "repo-code-bbbbbbbbbbbbbbbbbbbbbbbb",
            "path": "apps/backend/app/report.py",
            "content": "def approve_report(): pass",
        },
    ]


def _validated_shape(*, feature_evidence, path_evidence):
    return {
        "features": [
            {
                "name": "报告审批",
                "description": "报告审批已由真实代码实现",
                "source_type": "ai_analysis",
                "evidence_ids": feature_evidence,
                "candidate": True,
            }
        ],
        "module_path_candidates": [
            {
                "path": "apps/backend/app/report.py",
                "description": "[feature:报告审批] 报告审批后端实现",
                "implementation_scope": "后端",
                "source_type": "git_fact",
                "evidence_ids": path_evidence,
                "candidate": True,
            }
        ],
    }


def test_repository_backing_accepts_real_repo_code_evidence():
    code_id = "repo-code-bbbbbbbbbbbbbbbbbbbbbbbb"
    result = _validated_shape(feature_evidence=["prd-1", code_id], path_evidence=[code_id])

    subject._validate_repository_backing(result, _repo_items())


def test_repository_backing_rejects_prd_only_feature_claim():
    code_id = "repo-code-bbbbbbbbbbbbbbbbbbbbbbbb"
    result = _validated_shape(feature_evidence=["prd-1"], path_evidence=[code_id])

    with pytest.raises(HTTPException) as exc_info:
        subject._validate_repository_backing(result, _repo_items())

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "PROFILE_GENERATION_AI_RESULT_INVALID"
    assert "缺少真实仓库代码证据" in exc_info.value.detail["message"]


def test_repository_backing_rejects_tree_only_path_claim():
    code_id = "repo-code-bbbbbbbbbbbbbbbbbbbbbbbb"
    tree_id = "repo-tree-aaaaaaaaaaaaaaaaaaaaaaaa"
    result = _validated_shape(feature_evidence=[code_id], path_evidence=[tree_id])

    with pytest.raises(HTTPException) as exc_info:
        subject._validate_repository_backing(result, _repo_items())

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "PROFILE_GENERATION_AI_RESULT_INVALID"
    assert "代码路径候选" in exc_info.value.detail["message"]
    assert "缺少真实仓库代码证据" in exc_info.value.detail["message"]
