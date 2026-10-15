"""Frozen test contracts: discover files, capture immutable checkout bytes, verify."""

import fnmatch
import hashlib
from pathlib import Path, PurePosixPath

from bridge_core import BridgeError, git


def test_path(name):
    p = PurePosixPath(name.rstrip("/").lower())
    return (
        any(x in ("test", "tests", "spec", "specs", "__tests__") for x in p.parts)
        or p.name.startswith(("test_", "test-"))
        or any(fnmatch.fnmatch(p.name, x) for x in ("*_test.*", "*.test.*", "*.spec.*"))
    )


def contract_paths(repo, tasks):
    tracked = set(git(repo, "ls-files", "-z", strip=False).split("\x00")) - {""}
    result = {name for name in tracked if test_path(name)}
    for spec in tasks:
        explicit = spec.get("frozen_tests", [])
        if not isinstance(explicit, list) or any(not isinstance(x, str) or not x for x in explicit):
            raise BridgeError("frozen_tests must be a path list")
        for value in explicit:
            p = PurePosixPath(value.rstrip("/"))
            if p.is_absolute() or ".." in p.parts or "\\" in value or ":" in value:
                raise BridgeError("invalid frozen test path")
            matches = {name for name in tracked if name == str(p) or name.startswith(str(p) + "/")}
            if not matches:
                raise BridgeError("frozen test path has no baseline files")
            result.update(matches)
        for arg in spec["test_cmd"]:
            p = Path(arg)
            if not p.is_absolute() and (Path(repo) / p).is_file():
                name = p.as_posix()
                if name in tracked:
                    result.add(name)
    return sorted(result)


def capture(work, paths):
    from bridge_review import _safe_path

    result = {}
    for name in paths:
        p = _safe_path(Path(work), name)
        if p is None:
            raise BridgeError("missing frozen test file")
        h = hashlib.sha256()
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        result[name] = h.hexdigest()
    return result


def capture_modes(worktree, paths):
    from bridge_review import _safe_path
    result = {}
    for name in paths:
        path = _safe_path(Path(worktree), name)
        if path is None:
            raise BridgeError('baseline file missing: ' + name)
        result[name] = path.stat().st_mode & 0o777
    return result


def verify(task):
    expected = task.get("frozen_test_hashes", {})
    if expected and capture(task["worktree"], list(expected)) != expected:
        raise BridgeError("frozen test contract changed; tests/approval refused")
