"""Install a local wheel into a new environment and exercise it outside the source tree.

No API keys, model calls, or website visits. Optional extras are installed after base smoke.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main(wheel):
    wheel = wheel.resolve(strict=True)
    folder = Path(tempfile.mkdtemp(prefix="kong-release-install-"))
    workspace = folder/"workspace"
    workspace.mkdir()
    env = folder/"venv"
    python = env/"Scripts"/"python.exe" if sys.platform == "win32" else env/"bin"/"python"
    import hashlib
    report = {"wheel":str(wheel), "wheel_sha256":hashlib.sha256(wheel.read_bytes()).hexdigest(),
              "workspace_outside_source_tree":not workspace.is_relative_to(Path(__file__).resolve().parents[1]),
              "checks":[], "browser_note":"Uses the installed Chromium cache; not a fresh OS test."}
    def run(name, command):
        result = subprocess.run([str(x) for x in command],cwd=workspace,capture_output=True,encoding="utf-8",errors="replace",timeout=240)
        (folder/(name+".log")).write_text(result.stdout+result.stderr,encoding="utf-8")
        report["checks"].append({"name":name,"passed":result.returncode==0})
        (folder/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(name+": "+("passed" if result.returncode==0 else "FAILED"),flush=True)
        if result.returncode:
            raise RuntimeError("Clean install check failed: "+name+"; see "+str(folder))
        return result
    run("create",["uv","venv","--python",sys.executable,env])
    run("base-install",["uv","pip","install","--python",python,wheel])
    run("version",[python,"-I","-m","kong","--version"])
    run("import-origin",[python,"-I","-c","import kong,sys; from pathlib import Path; assert Path(kong.__file__).is_relative_to(Path(sys.prefix)); print(kong.__file__)"])
    run("base-demo",[python,"-I","-m","kong","--global-memory-db",folder/"global.db","demo","--auto-approve"])
    run("summary-artifact",[python,"-I","-c","import json; from pathlib import Path; r=json.loads(next(Path('.kong/reports').glob('*.json')).read_text(encoding='utf-8')); assert r['status']=='completed'; assert all(c['verified'] for c in r['criteria']); print('summary verified')"])
    run("extras-install",["uv","pip","install","--python",python,str(wheel)+"[skills,integrations]"])
    (workspace/"model.toml").write_text('default_profile="local"\n[profiles.local]\nbase_url="http://127.0.0.1:9/v1"\nmodel="offline-check"\n',encoding="utf-8")
    command = [python,"-I","-m","kong","--config","model.toml","--allow-process","--allow-browser","--allow-mcp"]
    if sys.platform == "win32":
        command += ["--allow-terminal"]
    run("doctor",[*command,"doctor","--json"])
    run("bundled-skills",[python,"-I","-c",
        "from kong.skills.catalog import SkillCatalog; from kong.tools.defaults import default_registry; from kong.workspace import Workspace; from pathlib import Path; r=default_registry(Workspace(Path.cwd()),allow_process=True); c=r.get('skill_list').catalog; d=c.listing(limit=100); assert len(d['skills'])>=8 and not d['diagnostics']; print('bundled skill catalog verified')"])
    print("Report: "+str(folder/"report.json"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel",type=Path)
    main(parser.parse_args().wheel)
