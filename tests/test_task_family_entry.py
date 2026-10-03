"""Explicit code/data entry requires a host-owned reviewable contract."""
import json

import pytest

from kong.completion_contracts import CompletionContract
from test_cli import cli


def test_task_family_without_contract_fails_before_model_or_action(tmp_path):
    result = cli(tmp_path, "run", "summarize data", "--task-family", "data", "--no-interactive")
    assert result.returncode != 0
    assert "completion-contract" in result.stdout + result.stderr
    assert not list((tmp_path / ".kong/runs").glob("*.json"))


def test_task_family_mismatch_fails_before_model_or_action(tmp_path):
    contract = {"family": "data", "artifacts": [{"id": "out", "path": "out.txt", "format": "text", "expected_text": "OK"}]}
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    result = cli(tmp_path, "run", "repair code", "--task-family", "code", "--completion-contract", str(path), "--no-interactive")
    assert result.returncode != 0
    assert "family must match" in result.stdout + result.stderr
    assert not list((tmp_path / ".kong/runs").glob("*.json"))


def test_declared_data_entry_rejects_existence_only_contract():
    from kong.completion_contracts import contract_for_task_family
    contract = CompletionContract.model_validate({"family": "data", "artifacts": [{"id": "out", "path": "out.txt"}]})
    with pytest.raises(ValueError, match="structure|value|business"):
        contract_for_task_family("data", contract)


def test_legacy_optional_contract_api_and_declared_typed_contract_remain_available():
    from kong.completion_contracts import contract_for_task_family
    assert contract_for_task_family(None, None) is None
    contract = CompletionContract.model_validate({"family": "data", "artifacts": [{"id": "out", "path": "out.json",
        "format": "json", "json_fields": [{"name": "count", "type": "integer"}]}]})
    assert contract_for_task_family("data", contract) is contract
