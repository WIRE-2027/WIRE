"""FRTG: extract and audit requirements, then generate and run testbenches."""

from __future__ import annotations

from .contracts import (
    measured_test,
    provided_modules,
    provided_constants,
    apply_review,
    normalize_requirements,
    normalize_review,
    normalize_groups,
    normalize_tb,
)
from copy import deepcopy
from .model import Session
from .tools.analysis import test_progress
from .tools.execution import execute_testbench
from .tools.source import provided_interface


def extract_requirements(ctx: Session):
    """Extract requirements and apply one audit while retaining Spec passages and source locations."""
    inputs = ctx.model_input()
    initial = ctx.ask("frtg_p1", "frtg/extract_requirements", inputs, normalize=normalize_requirements)
    review = ctx.ask(
        "frtg_p2",
        "frtg/audit_requirements",
        {**inputs, "requirements": initial},
        normalize=lambda value: normalize_review(value, initial),
    )
    requirements = ctx.compute(
        "frtg_requirements",
        {"initial": initial, "review": review},
        lambda: apply_review(initial, review),
    )
    ctx.save("frtg/requirements.json", requirements)
    return requirements


def group_requirements(ctx: Session, requirements):
    """Group requirements by compatible interfaces and build settings."""
    if not requirements:
        return []
    inputs = {**ctx.model_input(), "requirements": requirements}
    if ctx.build.get("defines"):
        inputs["defines"] = ctx.build["defines"]
    groups = ctx.ask(
        "frtg_p3",
        "frtg/group_requirements",
        inputs,
        normalize=lambda value: normalize_groups(value, requirements),
    )
    ctx.save("frtg/groups.json", groups)
    return groups


def testbench_input(ctx: Session, group, requirements):
    """Assemble complete requirements, interfaces and build settings for testbench generation."""
    by_id = {row["id"]: row for row in requirements}
    assigned = group["requirements"]
    data = {**ctx.model_input(), "requirements": [by_id[key] for key in assigned]}
    data["plan"] = group["plan"]
    data["provided_modules"] = provided_modules(ctx.sources, ctx.build)
    if constants := provided_constants(ctx.sources, ctx.build):
        data["interface_constants"] = constants
    if declarations := provided_interface(ctx.sources, ctx.build):
        data["interface_declarations"] = declarations
    if "defines" in group or ctx.build.get("defines"):
        data["defines"] = group.get("defines", ctx.build.get("defines", []))
    return data


def generate_tb(ctx: Session, name, data):
    return ctx.ask(name, "frtg/generate_testbench", data, normalize=normalize_tb)


def run(ctx: Session):
    requirements = extract_requirements(ctx)
    groups = group_requirements(ctx, requirements)
    results = generate_tests(ctx, requirements, groups)
    data = {
        "requirements": requirements,
        "groups": groups,
        "group_results": results,
        "tests": [test for group in results for test in group["tests"]],
    }
    ctx.save("frtg/result.json", data)
    return data


def tb_group(ctx: Session, group, requirements, index):
    """Generate one testbench and revise it up to three times using execution feedback."""
    group_id = f"g{index}"
    assigned = group["requirements"]

    data = testbench_input(ctx, group, requirements)
    observed, attempts, tests = set(), [], []
    previous, feedback = None, None
    conditions, covered_conditions = {}, set()
    stop_reason = "revision_budget_exhausted"
    for revision in range(1, 5):
        name = f"frtg_{group_id}_r{revision}"
        request = deepcopy(data)
        if feedback is not None:
            request["feedback"] = feedback
            if previous is not None:
                request["tb"] = {
                    key: previous[key] for key in ("top", "code", "conditions") if key in previous
                }
        previous, result = execute_testbench(ctx, name, request, generate_tb, previous, group)
        result = deepcopy(result)
        result["group_id"] = group_id
        result["assigned_requirements"] = list(assigned)
        progress = test_progress(
            result, assigned, observed, conditions, covered_conditions, revision
        )
        observed.update(progress['observed'])
        attempts.append(result)
        if progress['observed']:
            tests.append(measured_test(result, progress['accepted']))
        feedback = progress['feedback']
        pending_conditions = progress['pending_conditions']
        undeclared = progress['undeclared']
        ctx.save(f"frtg/{group_id}/attempt-{revision}.json", result)
        if any(run.get("status") == "toolchain_error" for run in result.get("runs", {}).values()):
            stop_reason = "toolchain_error"
            break
        # Valid passes and mismatches both establish execution; scenario coverage is tracked separately.
        if progress['complete']:
            stop_reason = progress['stop_reason']
            break
    outcome = {
        "id": group_id,
        "group": deepcopy(group),
        "attempts": attempts,
        "tests": tests,
        "observed_requirements": [key for key in assigned if key in observed],
        "pending_requirements": [key for key in assigned if key not in observed],
        "pending_conditions": pending_conditions,
        "pending_condition_requirements": undeclared,
        "coverage_scope": "observed_ids_only",
        "stop_reason": stop_reason,
    }
    ctx.save(f"frtg/{group_id}/result.json", outcome)
    return outcome


def generate_tests(ctx: Session, requirements, groups):
    return [tb_group(ctx, group, requirements, index) for index, group in enumerate(groups, 1)]
