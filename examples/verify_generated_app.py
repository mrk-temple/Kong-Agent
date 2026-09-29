"""Independent HTTP checks for the fixed evaluation's generated todo app.

Copies app/index to a fresh sandbox; never changes the delivered app's tasks.json.
"""
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from kong.environments.process import process_environment


def main(folder):
    sandbox = folder / ("independent-" + uuid4().hex[:8])
    sandbox.mkdir()
    for name in ("app.py", "index.html"):
        shutil.copy2(folder / name, sandbox / name)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    results = {}
    process = None
    def stop():
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable,"app.py","--port",str(port)],cwd=sandbox,
            env=process_environment(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        for _ in range(50):
            try:
                if client.get(base + "/").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                break
            time.sleep(.1)
        raise RuntimeError("Generated server failed to start")
    def send(method,path,body):
        return client.request(method,base+path,content=json.dumps(body),headers={"Content-Type":"application/json"})
    try:
        with httpx.Client(trust_env=False,timeout=5) as client:
            start()
            results["html"] = "待办" in client.get(base+"/").text
            results["initial_array"] = client.get(base+"/api/tasks").json() == []
            for index,body in enumerate([[],None,1,"text",True,{"title":" "},{"title":3}]):
                try:
                    results[f"post_bad_{index}"] = send("POST","/api/tasks",body).status_code == 400
                except httpx.HTTPError:
                    results[f"post_bad_{index}"] = False
            created = send("POST","/api/tasks",{"title":"独立验证"})
            task = created.json()
            results["create"] = created.status_code == 201 and task["done"] is False and task["title"] == "独立验证"
            path = "/api/tasks/" + str(task["id"])
            for index,body in enumerate([[],None,1,"text",True,{"done":"yes"}]):
                try:
                    results[f"patch_bad_{index}"] = send("PATCH",path,body).status_code == 400
                except httpx.HTTPError:
                    results[f"patch_bad_{index}"] = False
            patched = send("PATCH",path,{"done":True})
            results["update"] = patched.status_code == 200 and patched.json()["done"] is True
            results["missing"] = send("PATCH","/api/tasks/999999",{"done":True}).status_code == 404
            stop()
            start()
            results["restart_persistence"] = any(t["id"]==task["id"] and t["done"] for t in client.get(base+"/api/tasks").json())
    finally:
        stop()
        report = {"checks":results,"passed":sum(results.values()),"total":len(results),"sandbox":str(sandbox)}
        (folder / "independent-http-final.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False))
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("folder",type=Path)
    raise SystemExit(main(parser.parse_args().folder.resolve()))
