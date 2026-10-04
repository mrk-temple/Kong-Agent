"""The default public CLI must preserve the API's business-acceptance boundary."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_cli import cli


def test_default_cli_weak_json_does_not_claim_business_completion(tmp_path):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            decision = ({"kind":"act", "actions":[{"name":"write_file", "arguments":{
                "path":"report.json", "content":'{"included_statuses":2}'}}]} if len(calls) == 1 else
                {"kind":"respond", "content":"business report complete", "evidence_ids":["a1"]})
            data = json.dumps({"choices":[{"message":{"content":json.dumps(decision)}, "finish_reason":"stop"}]}).encode()
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
        config.write_text(f'default_profile="test"\n[profiles.test]\nbase_url="http://127.0.0.1:{server.server_port}/v1"\nmodel="fixture"\n', encoding="utf-8")
        criteria = tmp_path / "criteria.json"
        criteria.write_text(json.dumps([{"id":"out", "description":"report exists", "kind":"file_exists", "path":"report.json"}]))
        result = cli(tmp_path, "--config", str(config), "--global-memory-db", str(tmp_path / "global.db"),
                     "run", "create the correct business report", "--criteria", str(criteria), "--no-interactive")
        assert result.returncode == 2, result.stdout + result.stderr  # Paused acceptance is not CLI success.
        saved = json.loads(next((tmp_path / ".kong/runs").glob("*.json")).read_text(encoding="utf-8"))
        assert saved["state"]["status"] == "waiting_user"
        assert saved["state"]["stop_reason"] == "acceptance_criteria_required"
        assert saved["state"]["final_output"] is None
        assert len(calls) == 2 and len(saved["history"]["observations"]) == 1
        report = json.loads(next((tmp_path / ".kong/reports").glob("*.json")).read_text(encoding="utf-8"))
        assert report["verification"]["business_accepted"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
