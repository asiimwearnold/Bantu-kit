from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

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


def projects_root() -> Path:
    """Return the configured projects directory, or BANTU's bundled projects directory."""
    configured = os.environ.get("BANTU_PROJECTS_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else ROOT / "projects"
    return root.resolve()


def project_dir(name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"invalid project name: {name!r}")
    projects = projects_root()
    pdir = (projects / name).resolve()
    if projects not in pdir.parents:
        raise ValueError(f"project path is outside the configured projects root: {name!r}")
    return pdir


def _repository_url(value: str) -> str:
    url = value.strip()
    if not url or any(char in url for char in "\r\n") or "://" not in url and not url.startswith("git@"):
        raise ValueError("repository must be an HTTPS, SSH, or file URL")
    if url.startswith("git@"):
        if not re.fullmatch(r"git@[A-Za-z0-9.-]+:.+", url):
            raise ValueError("repository must be a valid SSH Git URL")
        return url
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "ssh", "file"}:
        raise ValueError("repository must be an HTTPS, SSH, or file URL")
    if parsed.query or parsed.fragment or parsed.password:
        raise ValueError("repository URLs cannot contain passwords, query strings, or fragments")
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path:
            raise ValueError("file repository URLs must point to a local path")
    elif not parsed.hostname or (parsed.username and not (parsed.scheme == "ssh" and parsed.username == "git")):
        raise ValueError("repository URLs must have a host and must not embed credentials")
    return url


def _canonical_repository(value: str) -> tuple[str, str]:
    url = _repository_url(value)
    if url.startswith("git@"):
        host, path = url[4:].split(":", 1)
        scheme = "ssh"
    else:
        parsed = urlsplit(url)
        host, path, scheme = parsed.hostname or "", unquote(parsed.path), parsed.scheme
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if scheme == "file":
        path = str(Path(path).resolve())
    return host.lower(), path


def ensure_project(name: str, repository: str | None = None) -> Path:
    """Resolve an existing project or clone its requested remote into the configured root."""
    pdir = project_dir(name)
    root = projects_root()
    bundled_root = (ROOT / "projects").resolve()
    if repository and root == bundled_root:
        raise SystemExit("Set BANTU_PROJECTS_ROOT before cloning a product repository.")
    try:
        requested = _repository_url(repository) if repository else None
    except ValueError as exc:
        raise SystemExit(f"Invalid repository URL: {exc}") from exc
    if pdir.exists():
        if not pdir.is_dir():
            raise SystemExit(f"Project path is not a directory: {pdir}")
        if requested:
            result = subprocess.run(
                ["git", "-C", str(pdir), "remote", "get-url", "origin"],
                capture_output=True, text=True,
            )
            try:
                matches = (
                    result.returncode == 0
                    and _canonical_repository(result.stdout.strip()) == _canonical_repository(requested)
                )
            except ValueError:
                matches = False
            if not matches:
                raise SystemExit(f"Project '{name}' exists but its origin does not match the requested repository.")
        return pdir
    if not repository:
        raise SystemExit(f"Unknown project '{name}': provide a repository URL or create {pdir}.")
    assert requested is not None
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".bantu-clone-", dir=root) as tempdir:
            clone_path = Path(tempdir) / "repository"
            result = subprocess.run(
                ["git", "clone", "--", requested, str(clone_path)],
                capture_output=True, text=True,
            )
            if result.returncode:
                raise SystemExit(
                    f"Could not clone requested repository (git exit {result.returncode}); "
                    "check the URL, network, and Git authentication."
                )
            if pdir.exists():
                raise SystemExit(f"Project path appeared during clone; refusing to overwrite: {pdir}")
            shutil.move(str(clone_path), str(pdir))
    except OSError as exc:
        raise SystemExit(f"Could not prepare external project directory: {exc}") from exc
    return pdir


def load_project(name: str) -> dict:
    try:
        pdir = project_dir(name)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not pdir.is_dir():
        raise SystemExit(f"Unknown project '{name}': create {projects_root() / name}.")
    meta_path = pdir / "bantu.project.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.setdefault("test_cmd", "python -m pytest -q")
    meta.setdefault("protected", ["tests/", "bantu.project.json"])
    meta.setdefault("notes_file", "PROJECT.md")
    return meta
