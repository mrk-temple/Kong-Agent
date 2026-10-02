import os

import pytest

from kong.storage import atomic_replace


@pytest.mark.skipif(os.name != "nt", reason="Windows file lock recovery")
def test_transient_windows_lock_retries_replacement_without_reexecuting_work(tmp_path, monkeypatch):
    source, target = tmp_path / "pending", tmp_path / "snapshot"
    source.write_text("new")
    target.write_text("old")
    replace = os.replace
    attempts = []
    def locked(src, dst):
        attempts.append(True)
        assert target.read_text() == "old"
        if len(attempts) < 3:
            error = PermissionError("synthetic lock")
            error.winerror = 5
            raise error
        replace(src, dst)
    monkeypatch.setattr("kong.storage.os.replace", locked)
    monkeypatch.setattr("kong.storage.time.sleep", lambda delay: None)
    atomic_replace(source, target)
    assert len(attempts) == 3 and target.read_text() == "new"


@pytest.mark.skipif(os.name != "nt", reason="Windows file lock recovery")
def test_permanent_denial_is_bounded_and_preserves_old_snapshot(tmp_path, monkeypatch):
    source, target = tmp_path / "pending", tmp_path / "snapshot"
    source.write_text("new")
    target.write_text("old")
    attempts = []
    def denied(src, dst):
        attempts.append(True)
        error = PermissionError("synthetic persistent denial")
        error.winerror = 5
        raise error
    monkeypatch.setattr("kong.storage.os.replace", denied)
    monkeypatch.setattr("kong.storage.time.sleep", lambda delay: None)
    with pytest.raises(PermissionError):
        atomic_replace(source, target)
    assert len(attempts) == 4 and target.read_text() == "old" and source.read_text() == "new"
