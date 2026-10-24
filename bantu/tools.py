"""Workspace jail, action parser and sandboxed command runner."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
SANDBOX_IGNORES = shutil.ignore_patterns(
    ".git", ".env", ".env.*", ".venv", "__pycache__", ".pytest_cache", "node_modules"
)


class ToolError(Exception):
    pass


def truncate(text: str, limit: int = 4000, keep_tail: bool = True) -> str:
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return f"[...{cut} chars cut...]\n" + text[-limit:] if keep_tail else text[:limit] + f"\n[...{cut} chars cut...]"


def parse_actions(text: str) -> list[dict]:
    """Parse @@write/@@run/@@read/@@finish blocks. Robust to models that wrap code in fences."""
    actions, lines, i = [], text.splitlines(), 0

    def block(start: int) -> tuple[list[str], int]:
        buf, j = [], start
        while j < len(lines) and lines[j].strip() != "@@end":
            buf.append(lines[j])
            j += 1
        return buf, j

    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("@@write "):
            buf, i = block(i + 1)
            if buf and buf[0].strip().startswith("```"):
                buf = buf[1:]
            if buf and buf[-1].strip() == "```":
                buf = buf[:-1]
            actions.append({"a": "write", "path": ln[8:].strip(), "content": "\n".join(buf) + ("\n" if buf else "")})
        elif ln == "@@run":
            buf, i = block(i + 1)
            cmd = next((b.strip() for b in buf if b.strip() and not b.strip().startswith("```")), "")
            actions.append({"a": "run", "cmd": cmd})
        elif ln.startswith("@@read "):
            actions.append({"a": "read", "path": ln[7:].strip()})
        elif ln == "@@finish":
            buf, i = block(i + 1)
            actions.append({"a": "finish", "summary": "\n".join(buf).strip()})
        i += 1
    return actions


class Workspace:
    def __init__(self, root: Path, cfg: dict, protected: list[str] | None = None, writable: list[str] | None = None):
        self.root = root.resolve()
        self.cfg = cfg
        self.protected = protected or []
        self.writable = writable

    def _resolve(self, rel: str) -> tuple[Path, str]:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path outside workspace: {rel}")
        posix = p.relative_to(self.root).as_posix()
        if any(
            part == ".git" or part == ".env" or part.startswith(".env.")
            for part in p.relative_to(self.root).parts
        ):
            raise ToolError(f"path not allowed: {rel}")
        return p, posix

    @staticmethod
    def _match(posix: str, pattern: str) -> bool:
        return posix == pattern.rstrip("/") or (pattern.endswith("/") and posix.startswith(pattern))

    def read(self, rel: str, limit: int = 12000) -> str:
        p, _ = self._resolve(rel)
        if not p.is_file():
            raise ToolError(f"no such file: {rel}")
        return truncate(p.read_text(encoding="utf-8", errors="replace"), limit, keep_tail=False)

    def write(self, rel: str, content: str) -> None:
        p, posix = self._resolve(rel)
        if any(self._match(posix, pat) for pat in self.protected):
            raise ToolError(f"{rel} is protected and cannot be changed")
        if self.writable is not None and not any(self._match(posix, pat) for pat in self.writable):
            raise ToolError(f"this role may only write: {', '.join(self.writable)}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def tree(self, max_files: int = 60) -> str:
        out: list[str] = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                if f == ".env" or f.startswith(".env."):
                    continue
                out.append(Path(dirpath, f).relative_to(self.root).as_posix())
        more = f"\n(+{len(out) - max_files} more)" if len(out) > max_files else ""
        return "\n".join(out[:max_files]) + more if out else "(empty)"

    def run(self, cmd: str) -> tuple[int, str]:
        sb = self.cfg["sandbox"]
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            return 2, f"cannot parse command: {exc}"
        if not argv:
            return 2, "empty command"
        if argv[0] not in sb["allow"]:
            return 2, f"command not allowed: {argv[0]} (allowed: {', '.join(sb['allow'])})"
        if sb["mode"] != "docker":
            for a in argv[1:]:
                if a == "-c" or ".." in a or a.startswith("/") or (len(a) > 1 and a[1] == ":"):
                    return 2, f"argument not allowed in local mode: {a}"

        with tempfile.TemporaryDirectory(prefix="bantu-check-") as tempdir:
            work_root = Path(tempdir) / "work"
            shutil.copytree(self.root, work_root, ignore=SANDBOX_IGNORES, symlinks=True)
            if sb["mode"] == "docker":
                full = [
                    "docker", "run", "--rm", "--network", sb.get("network", "none"),
                    "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
                    "-v", f"{work_root}:/work", "-w", "/work",
                    "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "-e", "PYTEST_ADDOPTS=-p no:cacheprovider",
                    "-e", "HOME=/tmp", sb["image"], *argv,
                ]
                if hasattr(os, "getuid"):
                    full[full.index(sb["image"]):full.index(sb["image"])] = [
                        "--user", f"{os.getuid()}:{os.getgid()}"
                    ]
                env = None
            else:
                full = argv
                env = {
                    k: v for k, v in os.environ.items()
                    if not any(h in k.upper() for h in SECRET_HINTS)
                }
                env["PYTHONDONTWRITEBYTECODE"] = "1"
            try:
                r = subprocess.run(
                    full, cwd=work_root, capture_output=True, text=True,
                    timeout=sb.get("timeout_sec", 120), env=env,
                )
            except subprocess.TimeoutExpired:
                return 124, "command timed out"
            except FileNotFoundError as exc:
                return 127, f"cannot start command: {exc}"
            return r.returncode, truncate((r.stdout + r.stderr).strip() or "(no output)")
