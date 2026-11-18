#!/usr/bin/env python3
"""Restore the complete upstream source for a WIRE benchmark task."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


BENCHMARK = Path(__file__).resolve().parent
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ALLOW_PROTOCOL": "https",
}


def git(root, *args, attempts=1):
    command = [
        "git", "-C", str(root),
        "-c", "url.https://github.com/.insteadOf=git@github.com:",
        "-c", "url.https://github.com/.insteadOf=git://github.com/",
        "-c", "url.https://github.com/.insteadOf=ssh://git@github.com/",
        *args,
    ]
    for attempt in range(attempts):
        try:
            subprocess.run(command, env=GIT_ENV, check=True)
            return
        except subprocess.CalledProcessError:
            if attempt + 1 == attempts:
                raise


def restore(task_id, output):
    if not re.fullmatch(r"[a-z0-9-]+/pr-\d+", task_id):
        raise ValueError("Expected a task ID such as cva6/pr-3107")
    task_path = BENCHMARK / task_id / "inputs" / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))["input"]
    repository = task["repo"]
    commit = task["commit_id"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository, flags=re.ASCII):
        raise ValueError(f"Expected a GitHub repository in task.json: {repository}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"Expected a full commit SHA in task.json: {commit}")

    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wire-source-", dir=output.parent) as temp:
        staging = Path(temp) / "task"
        source = staging / "repo"
        source.mkdir(parents=True)
        git(source, "init", "--quiet")
        git(source, "remote", "add", "origin", f"https://github.com/{repository}.git")
        git(source, "fetch", "--quiet", "--no-tags", "--depth", "1", "origin", commit, attempts=2)
        git(source, "checkout", "--quiet", "--detach", commit)
        git(
            source, "submodule", "update", "--init", "--recursive",
            "--depth", "1", "--jobs", "4", attempts=2,
        )
        for path in task["rtl_files"]:
            if not (source / path).is_file():
                raise ValueError(f"Target RTL is missing from {commit}: {path}")
        shutil.copy2(task_path, staging / "task.json")
        staging.rename(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help="Task ID, for example cva6/pr-3107")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(restore(args.task, args.output))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
