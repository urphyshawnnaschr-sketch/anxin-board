from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "apps" / "backend" / "app" / "smtp_connection_test.py"


def _tree() -> ast.AST:
    return ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))


def test_boundary_smtp_preflight_has_no_message_submission_call_surface():
    tree = _tree()
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    for forbidden in {
        "send_message",
        "sendmail",
        "mail",
        "rcpt",
        "data",
    }:
        assert forbidden not in called_attributes
        assert forbidden not in called_names


def test_boundary_smtp_preflight_does_not_import_business_send_authority():
    tree = _tree()
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)

    forbidden_modules = {
        "app.mail_workflow",
        "app.mail_send_attempts",
        "app.recipient_config",
        "app.mail_durable_admission",
        "app.mail_admitted_execution",
    }
    forbidden_names = {
        "MailMessage",
        "MailWorkflow",
        "SendAttempt",
        "RecipientConfig",
    }

    assert forbidden_modules.isdisjoint(imported_modules)
    assert forbidden_names.isdisjoint(imported_names)


def test_boundary_smtp_preflight_allowed_protocol_actions_are_connection_only():
    tree = _tree()
    protocol_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
    }

    assert protocol_calls <= {
        "ehlo",
        "starttls",
        "login",
        "noop",
        "quit",
        "close",
    }
    assert {"login", "noop"} <= protocol_calls
