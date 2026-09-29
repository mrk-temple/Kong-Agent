"""Regression cases for redaction and deleted historical credential discovery."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

spec = importlib.util.spec_from_file_location("release_audit", Path(__file__).resolve().parents[1] / "examples/audit_release_safety.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_deleted_secret_and_commit_message_remain_detectable(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.DEVNULL)

    git("init")
    git("config", "user.name", "Audit Test")
    git("config", "user.email", "audit@example.invalid")
    secret = b"synthetic-credential-for-history-regression"
    (tmp_path / "old.txt").write_bytes(secret)
    git("add", "old.txt")
    git("commit", "-m", "fixture " + secret.decode())
    git("rm", "old.txt")
    git("commit", "-m", "remove fixture")
    report = audit.audit_history(tmp_path, {secret})
    assert {item["type"] for item in report["findings"]} == {"blob", "commit"}
    assert report["skipped"] == []
    assert secret.decode() not in json.dumps(report)


def test_workspace_expands_archives_and_reports_no_matching_values(tmp_path):
    secret = b"synthetic-credential-for-archive-regression"
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/run.log").write_bytes(secret)
    with zipfile.ZipFile(tmp_path / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/config.toml", secret)
    report = audit.audit_workspace(tmp_path, {secret}, tmp_path / "report.json")
    assert {item["path"] for item in report["findings"]} == {"logs/run.log", "source.zip!nested/config.toml"}
    assert secret.decode() not in json.dumps(report)


def test_env_names_are_not_literal_credentials():
    assert audit.categories(b'api_key_env = "DEEPSEEK_API_KEY"', set()) == []
    value = b"ghp_" + b"x" * 36
    assert "provider_token" in audit.categories(value, set())


def test_location_containing_a_known_secret_is_redacted(tmp_path):
    secret = b"synthetic-credential-in-filename"
    (tmp_path / (secret.decode() + ".log")).write_bytes(b"safe content")
    report = audit.audit_workspace(tmp_path, {secret}, tmp_path / "report.json")
    assert report["findings"][0]["path"] == "[REDACTED].log"
    assert report["findings"][0]["categories"] == ["credential_in_path"]
    assert secret.decode() not in json.dumps(report)


def test_reused_blob_with_deleted_credential_filename_is_found_in_tree(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.DEVNULL)

    git("init")
    git("config", "user.name", "Audit Test")
    git("config", "user.email", "audit@example.invalid")
    (tmp_path / "README.md").write_bytes(b"same safe content")
    git("add", ".")
    git("commit", "-m", "safe name")
    token = "ghp_" + "w" * 36
    (tmp_path / token).write_bytes(b"same safe content")
    git("add", ".")
    git("commit", "-m", "second name")
    git("rm", token)
    git("commit", "-m", "remove second name")
    report = audit.audit_history(tmp_path, set())
    assert any(f["type"] == "tree" and "credential_in_path" in f["categories"] for f in report["findings"])
    assert token not in json.dumps(report)


def test_empty_directory_names_in_archives_are_audited(tmp_path):
    token = "ghp_" + "v" * 36
    with tarfile.open(tmp_path / "logs.tar.gz", "w:gz") as archive:
        item = tarfile.TarInfo(token + "/")
        item.type = tarfile.DIRTYPE
        archive.addfile(item)
    report = audit.audit_workspace(tmp_path, set(), tmp_path / "report.json")
    assert any("credential_in_path" in f["categories"] for f in report["findings"])
    assert token not in json.dumps(report)


def test_virtualenv_marker_directories_are_skipped(tmp_path):
    secret = b"synthetic-credential-inside-local-venv"
    venv = tmp_path / "rehearsal-venv"
    (venv / "Lib" / "site-packages").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (venv / "Lib" / "site-packages" / "third_party.py").write_bytes(secret)
    (tmp_path / "first_party.py").write_bytes(secret)
    report = audit.audit_workspace(tmp_path, {secret}, tmp_path / "report.json")
    paths = {item["path"] for item in report["findings"]}
    assert "first_party.py" in paths
    assert not any(p.startswith("rehearsal-venv") for p in paths)
    assert any(item["reason"] for item in report["skipped"]
               if item["path"] == "rehearsal-venv")
    assert secret.decode() not in json.dumps(report)
