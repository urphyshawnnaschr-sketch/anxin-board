"""Range size is a local resource gate, never a single-model context limit."""

from app.analysis_lineages import _candidate_dict
from app.git_analysis import compute_range_candidate
from app.git_snapshots import _assert_candidate_can_freeze, _file_manifest_records
from fastapi import HTTPException
import pytest


class RangeClient:
    def __init__(self, count=151, lines=101, diff_bytes=80_000):
        self.rows = [(str(lines), "0", f"src/file-{index:04}.ts") for index in range(count)]
        self.diff_bytes = diff_bytes

    def get_remote_head_ref(self, *_args): return "b" * 40
    def is_ancestor(self, *_args): return True
    def list_commits_bounded(self, *_args, **_kwargs): return ["b" * 40], False
    def count_commits(self, *_args): return 1
    def get_numstat_bounded(self, *_args, line_limit):
        return self.rows[:line_limit], len(self.rows) > line_limit
    def measure_unified_diff_bytes(self, *_args, byte_limit):
        return min(self.diff_bytes, byte_limit), self.diff_bytes > byte_limit


def candidate(client, **limits):
    return compute_range_candidate(client, None, branch="main", baseline_commit="a" * 40, **limits)


def test_large_processable_range_retains_all_file_facts_and_can_freeze():
    result = candidate(RangeClient())
    assert result.capacity == "within"
    assert result.batch_required is True
    assert result.statistics_complete is True
    assert result.changed_file_count == len(result.files) == 151
    assert result.added_lines == 151 * 101
    _assert_candidate_can_freeze(result, "b" * 40)
    _hash, manifest = _file_manifest_records(result)
    assert len(manifest) == 151
    assert manifest[-1]["path"] == "src/file-0150.ts"
    public = _candidate_dict(result)
    assert public["batch_required"] is True
    assert public["capacity_reasons"] == []


def test_hard_file_limit_never_represents_truncated_inventory_as_complete():
    result = candidate(RangeClient(count=5), text_file_limit=4)
    assert result.capacity == "capacity_exceeded"
    assert result.statistics_complete is False
    assert "changed_files_hard_limit" in result.capacity_reasons
    with pytest.raises(HTTPException) as raised:
        _assert_candidate_can_freeze(result, "b" * 40)
    assert "4" in raised.value.detail["message"]
    assert "文件" in raised.value.detail["message"]


def test_hard_diff_limit_preserved_with_explicit_reason():
    result = candidate(RangeClient(diff_bytes=1025), diff_read_limit=1024)
    assert result.capacity == "capacity_exceeded"
    assert result.statistics_complete is False
    assert "diff_bytes_hard_limit" in result.capacity_reasons
    assert _candidate_dict(result)["capacity_limits"]["diff_bytes"] == 1024


def test_explicit_hard_line_limit_does_not_stop_counting_returned_files():
    result = candidate(RangeClient(count=3, lines=4), line_change_limit=5)
    assert result.capacity == "capacity_exceeded"
    assert result.added_lines == 12
    assert len(result.files) == 3
    assert result.statistics_complete is True
    assert "line_changes_hard_limit" in result.capacity_reasons


@pytest.mark.parametrize("count", [150, 151, 450])
def test_file_count_alone_is_not_a_model_window_gate(count):
    result = candidate(RangeClient(count=count, lines=1))
    assert result.capacity == "within"
    assert result.statistics_complete
    assert len(result.files) == count
    assert result.batch_required is (count > 150)


def test_hard_commit_limit_has_reason_and_incomplete_marker():
    client = RangeClient(count=1)
    client.list_commits_bounded = lambda *args, **kwargs: (["b" * 40], True)
    client.count_commits = lambda *args: 501
    result = candidate(client)
    assert result.capacity == "capacity_exceeded"
    assert not result.statistics_complete
    assert "commits_hard_limit" in result.capacity_reasons
