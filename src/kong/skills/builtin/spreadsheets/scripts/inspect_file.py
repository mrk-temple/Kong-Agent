"""Read-only inspector; use a name that cannot shadow Python's inspect module."""
from kong.skills.inspection import main

if __name__ == "__main__":
    raise SystemExit(main("xlsx"))
