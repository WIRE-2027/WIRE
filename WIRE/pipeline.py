"""Coordinate FRTG, TBA and CV."""

from . import frtg, tba, cv
from .model import Session
from .tools.runtime import restore_test_artifacts
from .tools.stages import load_stage, validate_original_snapshot


def run(ctx: Session, *, stop_after="cv", m1_result=None, m2_result=None):

    second = load_stage(ctx, m2_result, "m2") if m2_result else None
    first = load_stage(ctx, m1_result, "m1") if m1_result else None

    if first is None and second is not None:
        first = {"requirements": second["requirements"]}
        ctx.save("reuse/m1_requirements.json", {"source": "m2.requirements"})
    elif first is None:
        first = frtg.run(ctx)
    if second is not None and first["requirements"] != second["requirements"]:
        raise ValueError("Reused M1 and M2 have different requirements")
    ctx.save("frtg.json", first)
    if stop_after == "m1":
        return first

    if second is None:
        if restored := restore_test_artifacts(ctx, first):
            ctx.save("frtg/reused_artifacts.json", {"tests": restored})
        ctx.save("frtg/result.json", first)
        second = tba.run(ctx, first, augment=stop_after != "m2-original")
    if stop_after == "m2-original":
        validate_original_snapshot(second)
    elif second.get("scope") == "tba-original" and stop_after != "cv":
        raise ValueError("Requested full M2 but supplied an original-only snapshot")
    ctx.save("tba.json", second)
    if stop_after in {"m2-original", "m2"}:
        return second

    third = cv.run(ctx, first, second)
    ctx.save("cv.json", third)
    return third
