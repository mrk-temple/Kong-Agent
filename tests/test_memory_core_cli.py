"""Real CLI processes and HTTP transport; the local provider remains deterministic."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kong.memory import Authority, Capability, MemoryService
from kong.storage import RunStore
from test_cli import cli


def core_cli(workspace, db, profile, *args):
    result = cli(workspace, "--memory-core-db", str(db), "--kong-profile", profile, "memory-core", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_cli_cross_process_identity_memory_bottle_and_http_answer(tmp_path):
    requests = []
    milestone = "Cedar shipped its amber milestone"
    hidden = "Willow PRIVATE_BOTTLE_528"
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            authorized = any("AUTHORIZED MEMORY" in m["content"] and milestone in m["content"] for m in body["messages"])
            decision = {"kind": "respond", "content": milestone if authorized else "No authorized recollection"}
            data = json.dumps({"choices": [{"message": {"content": json.dumps(decision)}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "model.toml"
        config.write_text(f'default_profile="local"\n[profiles.local]\nbase_url="http://127.0.0.1:{server.server_port}/v1"\nmodel="offline-context-reader"\napi_key_env="KONG_TEST_UNUSED"', encoding="utf-8")
        db = tmp_path / "memory.db"
        core_cli(tmp_path, db, "cedar", "profile-create")
        identity = tmp_path / "identity.json"
        identity.write_text(json.dumps({"name": "Cedar Kong", "communication_style": "concise"}), encoding="utf-8")
        core_cli(tmp_path, db, "cedar", "identity-set", "--identity", str(identity), "--revision", "0")
        for bottle, text in [("A", milestone), ("B", hidden)]:
            core_cli(tmp_path, db, "cedar", "bottle-create", "--id", bottle)
            core_cli(tmp_path, db, "cedar", "remember", "--bottle", bottle, "--kind", "episodic", "--text", text)
        core_cli(tmp_path, db, "cedar", "bottle-on", "--id", "A", "--revision", "2")
        args = ["--config", str(config), "--memory-core-db", str(db), "--kong-profile", "cedar"]
        first = cli(tmp_path, *args, "run", "Cedar project", "--no-interactive")
        assert first.returncode == 0, first.stdout + first.stderr
        store = RunStore(tmp_path / ".kong/runs")
        first_snapshot = store.load(store.list_runs()[0]["run_id"])
        assert first_snapshot.state.final_output == milestone
        second = cli(tmp_path, *args, "run", "Cedar project", "--thread", first_snapshot.thread_id, "--no-interactive")
        assert second.returncode == 0, second.stdout + second.stderr
        assert len(requests) == 2
        # Inspect actual provider input separately from returned Runtime output.
        assert all(any("AUTHORIZED MEMORY" in m["content"] and milestone in m["content"] for m in r["messages"]) for r in requests)
        assert all("Cedar Kong" in json.dumps(r) and hidden not in json.dumps(r) for r in requests)
        assert all(s["status"] == "completed" for s in store.list_runs())
        context = cli(tmp_path, *args, "context", first_snapshot.state.run_id)
        assert context.returncode == 0 and milestone not in context.stdout
        core_cli(tmp_path, db, "cedar", "bottle-off", "--id", "A", "--revision", "3")
        third = cli(tmp_path, *args, "run", "Cedar project", "--thread", first_snapshot.thread_id, "--no-interactive")
        assert third.returncode == 0, third.stdout + third.stderr
        assert milestone not in json.dumps(requests[-1]) and hidden not in json.dumps(requests[-1])
        assert store.load(store.list_runs()[0]["run_id"]).state.final_output == "No authorized recollection"
        auth = Authority(owner_id="local-owner", profile_id="cedar", capabilities=frozenset(Capability))
        with MemoryService(db) as memory:
            assert len(memory.list_records(auth).records) == 2
            assert all(o["capture"] == "off" and o["payload"] is None for o in memory.list_observations(auth))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_selected_export_preview_import_independent_profile(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    core_cli(tmp_path, source, "source", "profile-create")
    core_cli(tmp_path, source, "source", "bottle-create", "--id", "A")
    first = core_cli(tmp_path, source, "source", "remember", "--bottle", "A", "--text", "Chosen shared experience")
    core_cli(tmp_path, source, "source", "remember", "--bottle", "A", "--text", "Unselected PRIVATE_327")
    exchange = tmp_path / "chosen.json"
    result = core_cli(tmp_path, source, "source", "export", "--id", "A", "--file", str(exchange), "--select", first["record_id"])
    assert result["records"] == 1 and "PRIVATE_327" not in exchange.read_text(encoding="utf-8")
    core_cli(tmp_path, target, "target", "profile-create")
    preview = core_cli(tmp_path, target, "target", "import-preview", "--file", str(exchange))
    assert preview["default_active"] is False and preview["identity_changes"] is False
    receipt = core_cli(tmp_path, target, "target", "import", "--file", str(exchange), "--approved-digest", preview["approved_digest"], "--id", "imported")
    assert receipt["bottle_id"] == "imported"
    bottles = core_cli(tmp_path, target, "target", "bottles")
    assert len(bottles) == 1 and bottles[0]["active"] is False
    memories = core_cli(tmp_path, target, "target", "bottle-memories", "--id", "imported")
    assert len(memories) == 1 and memories[0]["current"]["content"] == "Chosen shared experience"
    assert memories[0]["current"]["evidence"] == "imported"
    # Overwriting an earlier export is refused, not silently destructive.
    overwrite = cli(tmp_path, "--memory-core-db", str(source), "--kong-profile", "source", "memory-core", "export", "--id", "A", "--file", str(exchange))
    assert overwrite.returncode != 0 and "PRIVATE_327" not in exchange.read_text(encoding="utf-8")


def test_cli_relationship_and_reflection_review_in_separate_processes(tmp_path):
    from kong.memory import InteractionObservation
    db = tmp_path / "relationship.db"
    core_cli(tmp_path, db, "living", "profile-create")
    assert core_cli(tmp_path, db, "living", "relationship-show") == {"relationship": None}
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"role": "collaborator", "addressing": "respectful", "history_mentions": "on_request"}), encoding="utf-8")
    core_cli(tmp_path, db, "living", "relationship-set", "--relationship", str(rules), "--revision", "0")
    assert core_cli(tmp_path, db, "living", "relationship-show")["relationship"]["role"] == "collaborator"
    auth = Authority(owner_id="local-owner", profile_id="living", input_id="synthetic", capabilities=frozenset(Capability))
    with MemoryService(db) as core:
        event = InteractionObservation(event_id="boundary", capture="on", original_access=core.get_access_state(auth), role="user", text="希望你扮演导师。")
        core.observe_interaction(auth, event)
    core_cli(tmp_path, db, "living", "formation-queue", "--id", "boundary")
    assert core_cli(tmp_path, db, "living", "formation-run")["model_calls"] == 0
    item, = core_cli(tmp_path, db, "living", "candidates")
    result = core_cli(tmp_path, db, "living", "proposal-from-candidate", "--id", item["candidate_id"])
    proposal = core_cli(tmp_path, db, "living", "proposal-show", "--id", result["proposal_id"])
    assert proposal["base_version"] == 1 and proposal["proposed"]["role"] == "mentor"
    core_cli(tmp_path, db, "living", "proposal-approve", "--id", result["proposal_id"], "--revision", "1", "--approved-digest", proposal["approved_digest"])
    assert core_cli(tmp_path, db, "living", "relationship-show")["relationship"]["role"] == "mentor"
    rollback = core_cli(tmp_path, db, "living", "proposal-rollback-preview", "--id", result["proposal_id"])
    core_cli(tmp_path, db, "living", "proposal-rollback", "--id", result["proposal_id"], "--approved-digest", rollback["approved_digest"])
    assert core_cli(tmp_path, db, "living", "relationship-show")["relationship"]["role"] == "collaborator"
    reflected = core_cli(tmp_path, db, "living", "reflection-run", "--max-records", "1", "--max-proposals", "0")
    report = core_cli(tmp_path, db, "living", "reflection-report", "--id", reflected["reflection_id"])
    assert report["model_calls"] == 0 and report["parents"] == []

def test_cli_revise_requires_current_revision(tmp_path):
    database = tmp_path / "core.db"
    core_cli(tmp_path, database, "cedar", "profile-create")
    core_cli(tmp_path, database, "cedar", "bottle-create", "--id", "main")
    saved = core_cli(tmp_path, database, "cedar", "remember", "--bottle", "main", "--text", "Badge is BLUE-41")
    corrected = core_cli(tmp_path, database, "cedar", "revise", "--id", saved["record_id"],
                         "--text", "Badge is GREEN-42", "--revision", "1")
    assert corrected["revision"] == 2
    stale = cli(tmp_path, "--memory-core-db", str(database), "--kong-profile", "cedar",
                "memory-core", "revise", "--id", saved["record_id"], "--text", "Stale", "--revision", "1")
    assert stale.returncode != 0
    records = core_cli(tmp_path, database, "cedar", "records")
    current = next(record for record in records["records"] if record["record_id"] == saved["record_id"])
    assert current["current"]["content"] == "Badge is GREEN-42"
