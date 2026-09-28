"""Frozen DeepSeek report-model catalog: the single binding point for identity.

Every report-chain layer (current authority, transport, gateways, adapters, and
operator-facing settings) must import the qualified report model identity from
here instead of carrying its own literal. Updating the qualified model is a
reviewed catalog change in exactly one place.

Current qualification basis: DeepSeek official Models & Pricing page (checked
2026-09-19) lists ``deepseek-flash`` served by ``DeepSeek-V4.1-Flash`` with a
1M context window and a 384K maximum output. The retired ``deepseek-v4-flash``
name is no longer listed and must never be reintroduced as a report identity.
"""

from __future__ import annotations

from typing import Final

REPORT_PROVIDER: Final = "deepseek"
REPORT_MODEL_ID: Final = "deepseek-flash"
REPORT_MODEL_VERSION: Final = "DeepSeek-V4.1-Flash"
REPORT_CONTEXT_WINDOW_TOKENS: Final = 1_000_000
REPORT_MAX_OUTPUT_TOKENS: Final = 384_000

# Models the operator may select for the daily-report chain. Only models whose
# exact identity is qualified for report generation belong here; the settings
# API must not offer or accept anything else, even when the account /models
# list contains more.
REPORT_SELECTABLE_MODEL_IDS: Final = ("deepseek-flash",)

__all__ = (
    "REPORT_PROVIDER",
    "REPORT_MODEL_ID",
    "REPORT_MODEL_VERSION",
    "REPORT_CONTEXT_WINDOW_TOKENS",
    "REPORT_MAX_OUTPUT_TOKENS",
    "REPORT_SELECTABLE_MODEL_IDS",
)
