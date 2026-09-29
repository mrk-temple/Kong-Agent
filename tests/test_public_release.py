"""Release boundaries: scan actual bytes and exclude local/private material."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("public_release", Path(__file__).resolve().parents[1]/"examples/prepare_public_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_snapshot_excludes_nested_private_files_and_preserves_hashes(tmp_path):
    root = tmp_path / "input"
    (root / "src/kong/skills/builtin/coding/references").mkdir(parents=True)
    (root / "src/kong/docs").mkdir(parents=True)
    (root / "tests/docs").mkdir(parents=True)
    (root / "examples/docs").mkdir(parents=True)
    (root / ".github/workflows").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("safe")
    (root / ".env.example").write_text("KONG_API_KEY=\n")
    (root / ".github/workflows/ci.yml").write_text("name: CI")
    (root / "src/kong/skills/builtin/coding/SKILL.md").write_text("public skill")
    (root / "src/kong/skills/builtin/coding/references/checks.md").write_text("public reference")
    (root / "docs/public.md").write_text("internal even if named public")
    (root / "docs/.env.production").write_text("private")
    (root / "docs/.ENV.SECRET").write_text("private")
    (root / "docs/API.TXT").write_text("private")
    (root / "src/kong/docs/notes.md").write_text("private")
    (root / "tests/docs/notes.md").write_text("private")
    (root / "examples/docs/notes.md").write_text("private")
    (root / "examples/run.log").write_text("private")
    (root / "examples/kong.local.toml").write_text("private")
    (root / "DESIGN.md").write_text("private")
    (root / "UPGRADE_PLAN.md").write_text("private")
    (root / "api.txt").write_text("private")
    report = release.prepare(root, tmp_path / "out")
    names = [item["path"] for item in report["manifest"]]
    assert set(names) == {
        ".env.example", ".github/workflows/ci.yml", "README.md",
        "src/kong/skills/builtin/coding/SKILL.md",
        "src/kong/skills/builtin/coding/references/checks.md",
    }
    assert report["publication_blockers"] and not report["published"]
    assert "copyright" in report["publication_blockers"][0]
    assert report["artifacts"][0]["members"] == len(names)
    with zipfile.ZipFile(tmp_path / "out/kong-agent-public-candidate.zip") as archive:
        assert set(archive.namelist()) == set(names)
    for item in report["manifest"]:
        copied = (tmp_path / "out/source" / item["path"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(copied).hexdigest()
    assert json.loads((tmp_path/"out/manifest.json").read_text())["manifest"] == report["manifest"]


def test_known_secret_blocks_source_and_package_without_echo(tmp_path):
    secret = b"a-private-test-credential-value"
    root = tmp_path / "input"
    root.mkdir()
    (root / "README.md").write_bytes(secret)
    with pytest.raises(ValueError) as error:
        release.prepare(root, tmp_path / "out", [secret])
    assert secret.decode() not in str(error.value)
    assert not (tmp_path / "out").exists()
    (root / "README.md").write_text("safe")
    archive = tmp_path / "package.whl"
    with zipfile.ZipFile(archive,"w") as target:
        target.writestr("kong/config.py",secret)
        target.writestr("api.txt","private")
    with pytest.raises(ValueError) as error:
        release.prepare(root, tmp_path / "out", [secret], [archive])
    assert secret.decode() not in str(error.value)
    assert "private_or_unsafe_member" in str(error.value)
    assert not (tmp_path / "out").exists()


def test_tar_cannot_hide_internal_directory_as_its_root(tmp_path):
    package = tmp_path / "kong_agent-0.3.0.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        for name in ("docs/README.md", "unrelated/README.md"):
            item = tarfile.TarInfo(name)
            item.size = 4
            archive.addfile(item, io.BytesIO(b"safe"))
    findings = release.scan_archive(package, set())["findings"]
    assert sum(f["kind"] == "invalid_sdist_root" for f in findings) == 2
    assert any(f["path"] == "docs/README.md" and f["kind"] == "private_or_unsafe_member" for f in findings)


def test_archive_empty_directories_are_checked(tmp_path):
    token = "ghp_" + "z" * 36
    package = tmp_path / "candidate.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("src/kong/" + token + "/", b"")
    report = release.scan_archive(package, set())
    assert any(f["kind"] == "credential_in_path" for f in report["findings"])
    assert token not in json.dumps(report)
    package = tmp_path / "kong_agent-0.3.0.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        item = tarfile.TarInfo("kong_agent-0.3.0/docs/")
        item.type = tarfile.DIRTYPE
        archive.addfile(item)
    report = release.scan_archive(package, set())
    assert any(f["kind"] == "private_or_unsafe_member" for f in report["findings"])


def test_sdist_root_configuration_files_are_allowed_but_nested_ones_are_not(tmp_path):
    package = tmp_path / "kong_agent-0.3.0.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        for name in (".env.example", "kong.example.toml", "pyproject.toml", "examples/.env.example", "examples/pyproject.toml"):
            item = tarfile.TarInfo("kong_agent-0.3.0/" + name)
            item.size = 0
            archive.addfile(item, io.BytesIO(b""))
    findings = release.scan_archive(package, set())["findings"]
    assert {f["path"] for f in findings} == {"kong_agent-0.3.0/examples/.env.example", "kong_agent-0.3.0/examples/pyproject.toml"}


@pytest.mark.parametrize("secret", [b"ghp_" + b"x" * 36, b"custom-known-credential-for-path-test"])
def test_source_and_archive_credential_names_block_and_redact(tmp_path, secret):
    secrets = [] if secret.startswith(b"ghp_") else [secret]
    root = tmp_path / "input"
    (root / "src/kong").mkdir(parents=True)
    (root / "src/kong" / (secret.decode() + ".py")).write_text("safe")
    with pytest.raises(ValueError) as error:
        release.prepare(root, tmp_path / "out", secrets)
    assert secret.decode() not in str(error.value)
    assert "credential_in_path" in str(error.value)
    assert not (tmp_path / "out").exists()
    package = tmp_path / "kong_agent-0.3.0.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        item = tarfile.TarInfo("kong_agent-0.3.0/src/kong/" + secret.decode() + ".py")
        item.size = 4
        archive.addfile(item, io.BytesIO(b"safe"))
    report = release.scan_archive(package, secrets)
    assert secret.decode() not in json.dumps(report)
    assert any(f["kind"] == "credential_in_path" for f in report["findings"])


def test_github_token_pattern_blocks_candidate_without_echo(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    token = b"ghp_" + b"A" * 36
    (root / "README.md").write_bytes(b"sample " + token)
    with pytest.raises(ValueError) as error:
        release.prepare(root, tmp_path / "out")
    assert "provider_token" in str(error.value)
    assert token.decode() not in str(error.value)
    assert not (tmp_path / "out").exists()


def test_supplied_archives_reject_internal_documents_and_unlisted_members(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    (root / "README.md").write_text("safe")
    sdist = tmp_path / "kong_agent-0.3.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in (
            "kong_agent-0.3.0/README.md", "kong_agent-0.3.0/PKG-INFO",
            "kong_agent-0.3.0/docs/internal.md", "kong_agent-0.3.0/DESIGN.md",
        ):
            data = b"safe"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    findings = release.scan_archive(sdist, set())["findings"]
    assert ("kong_agent-0.3.0/docs/internal.md", "private_or_unsafe_member") in {
        (finding["path"], finding["kind"]) for finding in findings
    }
    assert ("kong_agent-0.3.0/DESIGN.md", "private_or_unsafe_member") in {
        (finding["path"], finding["kind"]) for finding in findings
    }
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("kong/skills/builtin/coding/SKILL.md", "safe")
        archive.writestr("kong/docs/internal.md", "private")
        archive.writestr("tests/test_something.py", "unlisted in wheel")
    findings = release.scan_archive(wheel, set())["findings"]
    assert ("kong/docs/internal.md", "private_or_unsafe_member") in {
        (finding["path"], finding["kind"]) for finding in findings
    }
    assert ("tests/test_something.py", "non_public_member") in {
        (finding["path"], finding["kind"]) for finding in findings
    }
    with pytest.raises(ValueError):
        release.prepare(root, tmp_path / "out", artifacts=[sdist, wheel])
    assert not (tmp_path / "out").exists()
