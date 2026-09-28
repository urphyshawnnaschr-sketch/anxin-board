from __future__ import annotations

import ast
from pathlib import Path


MODULE = Path(__file__).resolve().parents[2] / "apps" / "backend" / "app" / "mail_admitted_execution.py"


def _tree() -> ast.Module:
    return ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))


def test_boundary_01_admitted_execution_has_no_secret_or_public_http_imports():
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

    assert "app.secret_store" not in imported_modules
    assert "fastapi" not in imported_modules
    assert "fastapi.routing" not in imported_modules
    assert "StdlibSmtpMailGateway" not in imported_names
    assert "SecretStore" not in imported_names
    assert "APIRouter" not in imported_names
    assert "FastAPI" not in imported_names


def test_boundary_02_only_validation_execution_function_can_reach_mailworkflow():
    tree = _tree()
    callers: list[str] = []

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                if child.func.id == "execute_send_attempt_once":
                    callers.append(node.name)

    assert callers == ["execute_admitted_send_attempt_once_for_validation"]


def test_boundary_03_validation_execution_requires_exact_concrete_fake_gateway_type():
    tree = _tree()
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "execute_admitted_send_attempt_once_for_validation"
    )

    exact_type_guards = [
        node
        for node in ast.walk(target)
        if isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.IsNot)
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == "type"
        and len(node.left.args) == 1
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id == "FakeMailGateway"
    ]
    assert len(exact_type_guards) == 1
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        for node in ast.walk(target)
    )
    assert any(
        isinstance(node, ast.Constant)
        and node.value == "MAIL_EXECUTION_REAL_GATEWAY_NOT_AUTHORIZED"
        for node in ast.walk(target)
    )


def test_boundary_04_module_contains_no_network_or_secret_constructor_symbols():
    source = MODULE.read_text(encoding="utf-8")

    forbidden = (
        "StdlibSmtpMailGateway(",
        "SecretStore(",
        ".get_secret(",
        "secret_store.get(",
        "smtplib.",
        "socket.",
        "requests.",
        "httpx.",
        "@app.",
        "@router.",
    )
    for marker in forbidden:
        assert marker not in source
