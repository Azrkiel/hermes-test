"""Paired local benchmarks; no model calls and no weaker integrity checks.

Run from the source checkout with PYTHONPATH=. and a trusted baseline Git ref.
Optional --repo/--swarm measure a completed, unchanged synthetic live batch.
"""
import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

import bridge_core
import bridge_review
import bridge_swarm


def baseline_module(ref, name):
    source = subprocess.check_output(["git", "show", f"{ref}:{name}.py"])
    module = types.ModuleType("baseline_" + name)
    module.__file__ = str(Path(name).resolve())
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def paired(before, after, repeats):
    for call in (before, after):
        call()
    samples = {"baseline": [], "current": []}
    counts = {"baseline": [], "current": []}
    original = subprocess.Popen
    for i in range(repeats):
        order = [("baseline", before), ("current", after)]
        if i % 2:
            order.reverse()
        for label, call in order:
            launches = []

            def counted(argv, *args, **kwargs):
                if isinstance(argv, (tuple, list)) and argv[0] == "git":
                    launches.append(argv)
                return original(argv, *args, **kwargs)

            with patch.object(subprocess, "Popen", counted):
                start = time.perf_counter()
                call()
                samples[label].append(time.perf_counter() - start)
            counts[label].append(len(launches))
    result = {label: {"median_seconds": statistics.median(values), "samples_seconds": values,
                      "git_launches_per_call": counts[label]}
              for label, values in samples.items()}
    result["speedup_ratio"] = result["baseline"]["median_seconds"] / result["current"]["median_seconds"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="ece0b8738218433d5420df9884cab398a6d6957d")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--repo")
    parser.add_argument("--swarm")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.repeats < 3 or bool(args.repo) != bool(args.swarm):
        parser.error("use at least 3 repeats and provide both --repo and --swarm")
    old_review = baseline_module(args.baseline, "bridge_review")
    report = {"baseline_commit": args.baseline, "python": sys.version.split()[0],
              "repeats": args.repeats, "method": "warmup then alternating paired order; local work only"}
    with tempfile.TemporaryDirectory(prefix="bridge-benchmark-") as directory:
        root = Path(directory)
        bridge_core.git(root, "init")
        bridge_core.git(root, "config", "user.name", "Bridge Benchmark")
        bridge_core.git(root, "config", "user.email", "benchmark@invalid.local")
        for i in range(200):
            (root / f"source_{i:03}.txt").write_bytes(b"source\n" * 1024)
        (root / ".gitignore").write_text("ignored/\n")
        bridge_core.git(root, "add", ".")
        bridge_core.git(root, "commit", "-m", "synthetic benchmark")
        (root / "ignored").mkdir()
        (root / "ignored" / "evidence.txt").write_bytes(b"ignored\n" * 1024)
        head = bridge_core.git(root, "rev-parse", "HEAD")
        report["snapshot"] = paired(lambda: old_review._snapshot_fingerprint(root, head),
                                    lambda: bridge_review._snapshot_fingerprint(root, head), args.repeats)
    if args.repo:
        bridge = bridge_core.Bridge(roots=[args.repo])
        old_manager = baseline_module(args.baseline, "bridge_swarm").Swarm(bridge)
        new_manager = bridge_swarm.Swarm(bridge)

        def status(manager):
            value = manager.status(args.repo, args.swarm)
            assert value["status"] == "ready", value["status"]

        report["ready_status"] = paired(lambda: status(old_manager), lambda: status(new_manager), args.repeats)
    text = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
