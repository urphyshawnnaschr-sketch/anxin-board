"""Source contract: local preparation/warning review has no redundant Human checkbox."""
from pathlib import Path


def test_task_execution_keeps_only_real_send_boundary_as_explicit_gate():
    root = Path(__file__).resolve().parents[2]
    source = (root / 'apps/frontend/src/views/TaskExecutionView.vue').read_text(encoding='utf-8')
    assert 'preparationAuthorized' not in source
    assert 'sendConfirmed' not in source
    assert 'risksReviewed' not in source
    assert '允许准备这次 AI 报告调用' not in source
    assert '我已查看提示' not in source
    assert '授权本次发送（尚不调用 AI）' in source
    assert '开始 AI 分析 · 会调用模型' in source
    assert '不需要额外确认' in source
