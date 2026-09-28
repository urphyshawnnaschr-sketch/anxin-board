"""Build deterministic client-stage summaries for all formal Anxin Board modules."""

from __future__ import annotations

from collections.abc import Mapping

from app.anxin_board_report import MODULES
from app.client_stage_summary import build_client_stage_summary


class AnxinBoardModuleSummariesError(ValueError):
    """Stable fail-closed error for malformed batch input."""

    code = "ANXIN_BOARD_MODULE_SUMMARIES_INVALID"

    def __init__(self, message: str = code):
        super().__init__(message)


def _fail() -> AnxinBoardModuleSummariesError:
    return AnxinBoardModuleSummariesError()


def build_anxin_board_module_summaries(
    *,
    module_stages: Mapping[str, str],
) -> list[dict[str, object]]:
    """Project explicit formal module stages through the existing stage-summary Core."""

    if not isinstance(module_stages, Mapping):
        raise _fail()

    expected_ids = tuple(module_id for module_id, _module_name in MODULES)
    if any(type(module_id) is not str for module_id in module_stages):
        raise _fail()
    if set(module_stages) != set(expected_ids):
        raise _fail()

    return [
        build_client_stage_summary(
            module_name=module_name,
            stage=module_stages[module_id],
        )
        for module_id, module_name in MODULES
    ]
