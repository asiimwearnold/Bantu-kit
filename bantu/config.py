from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Tiny .env loader (no dependencies). Existing environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> dict:
    load_env()
    cfg = json.loads((ROOT / "bantu.config.json").read_text(encoding="utf-8"))
    mode = os.environ.get("BANTU_SANDBOX")
    if mode:
        cfg["sandbox"]["mode"] = mode
    return cfg


def project_dir(name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"invalid project name: {name!r}")
    projects = (ROOT / "projects").resolve()
    pdir = (projects / name).resolve()
    if projects not in pdir.parents:
        raise ValueError(f"project path is outside projects/: {name!r}")
    return pdir


def load_project(name: str) -> dict:
    try:
        pdir = project_dir(name)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not pdir.is_dir():
        raise SystemExit(f"Unknown project '{name}': create projects/{name}/ first.")
    meta_path = pdir / "bantu.project.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.setdefault("test_cmd", "python -m pytest -q")
    meta.setdefault("protected", ["tests/", "bantu.project.json"])
    meta.setdefault("notes_file", "PROJECT.md")
    return meta
