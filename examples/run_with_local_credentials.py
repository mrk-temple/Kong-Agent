"""Explicit local launcher for the user's api.txt format; never prints credentials.

python examples/run_with_local_credentials.py --web live run "..."
Model/search secrets live only in this process's environment. Kong's child-process
environment allowlist excludes them. api.txt remains outside test workspaces.
"""
import os
from pathlib import Path
import sys

from live_evaluation import credentials
from kong.cli import main


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    model_key, search_key = credentials(root / "api.txt")
    os.environ["KONG_API_KEY"] = model_key
    os.environ["TAVILY_API_KEY"] = search_key
    sys.argv = [sys.argv[0], "--config", str(root / "kong.local.toml"), "--profile", "relay", *sys.argv[1:]]
    main()
