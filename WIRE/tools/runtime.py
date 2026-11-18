"""Restore waveform artifacts for reused tests."""

from pathlib import Path


def restore_test_artifacts(ctx, frtg_result):
    """Rebuild missing FRTG waveform artifacts while preserving recorded checks and requirements."""
    restored = []
    for test in frtg_result.get("tests", []):
        runs = test.get("runs", {})
        usable = [
            run
            for run in runs.values()
            if run.get("status") == "ok" and run.get("vcd")
        ]
        if usable and all(Path(run["vcd"]).exists() for run in usable):
            continue
        tb = test.get("tb")
        if not isinstance(tb, dict) or not tb.get("code") or not tb.get("top"):
            continue
        result = ctx.simulate("reuse_" + test["id"], tb)
        if result.get("runs"):
            test["runs"] = result["runs"]
            restored.append(test["id"])
    return restored
