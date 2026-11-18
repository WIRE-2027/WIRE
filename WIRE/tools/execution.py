"""Testbench generation, revision and augmentation replay support."""

from __future__ import annotations

import json
from ..contracts import (
    ContractError,
    provided_modules,
    provided_constants,
    normalize_tb,
    digest,
)
from ..model import Session
from copy import deepcopy
from .source import provided_interface, related_dependency_context
from .analysis import (
    repaired_test_record,
    merge_entries,
    analysis_payload,
    initial_test_issues,
)


# After three test revisions, retain unresolved issues for source analysis.
MAX_TBA_REPAIRS = 3


def execute_testbench(ctx, name, request, generate_tb, previous, group):
    """Generate and run one testbench, retaining output-format and execution errors."""
    try:
        # Malformed model output still consumes a generation attempt.
        proposed = generate_tb(ctx, name + "_tb", request)
    except (ContractError, json.JSONDecodeError) as error:
        result = {
            "id": name,
            "tb": previous,
            "runs": {},
            "error": str(error),
            "status": "invalid_tb_reply",
        }
    else:
        previous = {"id": name, **proposed}
        if "defines" in group:
            previous["defines"] = group["defines"]
        try:
            result = ctx.simulate(name, previous)
        except ValueError as error:
            result = {
                "id": name,
                "tb": previous,
                "runs": {},
                "error": str(error),
                "status": "invalid_tb",
            }
    return previous, result


def execute_repaired_test(ctx, name, data, previous, defines, generate_tb):
    try:
        tb = generate_tb(ctx, name + "_tb", data)
        result = ctx.simulate(
            name,
            {"id": name, **tb, "defines": defines},
        )
    except (ContractError, json.JSONDecodeError, ValueError) as error:
        result = {
            "id": name,
            "tb": previous,
            "runs": {},
            "error": str(error),
            "status": "tb_repair_failed",
        }
    return result


def replay_result(box, proposal):
    try:
        outcome = box.augment_and_replay(**proposal)
    except (ValueError, KeyError, TypeError) as error:
        outcome = {"proposal": proposal, "error": str(error)}
    return outcome


def repair_test(ctx: Session, test, requirements, errors, name):
    """Prepare a shared TBA testbench revision and return its executed checks."""

    assigned = test.get("assigned_requirements") or [r["id"] for r in requirements]
    previous = test.get("tb") or {}
    data = ctx.model_input()
    data.update(
        requirements=[deepcopy(r) for r in requirements if r["id"] in assigned],
        provided_modules=provided_modules(ctx.sources, ctx.build),
        feedback={
            "log": "Review these saved execution or coverage issues against the Spec and input history. Correct the TB only where needed, preserve correct checks and genuine DUT failures. Use the current observations to revise the shared test.\n"
            + json.dumps(errors, ensure_ascii=False),
            "pending": [],
        },
    )
    if previous.get("code"):
        data["tb"] = {
            k: previous[k] for k in ("top", "code", "conditions") if k in previous
        }
    data["feedback"]["pending"] = list(
        dict.fromkeys(rid for issue in errors for rid in issue.get("requirements", []))
    )
    data["related_dependency_context"] = related_dependency_context(
        ctx.sources, ctx.build, data["requirements"], supplied_files=data["rtl"]
    )
    data["plan"] = (
        "Complete the listed pending checks and correct diagnosed TB/CHECK issues in the shared runnable TB. "
        "For the affected check, establish the required state through legal stimulus, drive the trigger, and compare at the Spec-defined time. "
        "Preserve useful checks and genuine DUT mismatches. Other requirements may remain pending; "
        "continue with the available observations after this repair."
    )
    if constants := provided_constants(ctx.sources, ctx.build):
        data["interface_constants"] = constants
    if declarations := provided_interface(ctx.sources, ctx.build):
        data["interface_declarations"] = declarations
    defines = previous.get("defines", ctx.build.get("defines", []))
    data["defines"] = defines
    result = execute_repaired_test(
        ctx,
        name,
        data,
        previous,
        defines,
        lambda c, step, values: c.ask(
            step, "frtg/generate_testbench", values, normalize=normalize_tb
        ),
    )
    return repaired_test_record(ctx, name, result, test, assigned)


def repaired_batch(ctx, box, payload, test, issues, requirements, index):
    """Attach new mismatches from a testbench revision to the current tracing context."""
    name = f"tba_tb_repair_{index}"
    issues = [
        {
            **issue,
            "requirements": sorted(
                {
                    *issue.get("requirements", []),
                    *(
                        r
                        for e in issue.get("entries", [])
                        if e in box.entries
                        for r in box.entries[e].get("requirements", [])
                    ),
                }
            ),
        }
        for issue in issues
    ]
    repaired = repair_test(ctx, test, requirements, issues, name)
    box.variants["original"]["tests"][repaired["id"]] = repaired
    members = merge_entries([repaired])
    for entry in members:
        entry["id"] = f"E{len(box.entries)+1}"
        for observation in entry["observations"]:
            observation["mismatch_id"] = (
                f"M_repair_{index}_{observation['mismatch_id']}"
            )
        box.entries[entry["id"]] = entry
    fresh = None
    if members:
        fresh = analysis_payload(ctx, box, requirements, members)
        fresh["group"] = {
            **payload["group"],
            "id": payload["group"]["id"] + "_repair",
            "representative": members[0]["id"],
            "members": [e["id"] for e in members],
        }
        fresh["repair_context"] = {"source_test": test["id"], "issues": issues}
    return repaired, fresh


def refine_test_batches(
    ctx, box, payload, test, issues, requirements, index, attempted
):
    """Address executable test gaps until resolved or the same test state recurs."""
    revisions, batches = [], []
    while issues and len(revisions) < MAX_TBA_REPAIRS:
        signature = digest(
            {
                "family": payload["group"]["family"],
                "tb": {
                    key: (test.get("tb") or {}).get(key)
                    for key in ("top", "code", "defines")
                },
                "issues": [
                    {
                        "requirements": sorted(issue.get("requirements", [])),
                        "condition": issue.get("condition", ""),
                        "reason": issue.get("reason", ""),
                        "observations": sorted(
                            (box.entries[e]["signal"], box.entries[e]["time"])
                            for e in issue.get("entries", [])
                            if e in box.entries
                        ),
                    }
                    for issue in issues
                ],
            }
        )
        if signature in attempted:
            break
        attempted.add(signature)
        step = index if not revisions else f"{index}_{len(revisions) + 1}"
        revised, fresh = repaired_batch(
            ctx, box, payload, test, issues, requirements, step
        )
        revisions.append(revised)
        if fresh:
            batches.append(fresh)
        pending = list(revised.get("pending_conditions", []))
        represented = {row["requirement"] for row in pending}
        pending.extend(
            {"requirement": rid}
            for rid in revised.get("pending_requirements", [])
            if rid not in represented
        )
        issues = initial_test_issues(
            revised, requirements, pending, review_passes=False
        )
        test = revised
        # After a complete validated run, retain uncovered requirements for source analysis.
        usable = any(
            run.get("status") == "ok" and run.get("valid_checks")
            for run in revised.get("runs", {}).values()
        )
        if usable and not any(run.get("checker_errors") for run in revised.get("runs", {}).values()):
            if issues and revisions:
                revisions[-1]["pending_review_issues"] = issues
            break
    if issues and revisions:
        revisions[-1]["repair_stop_reason"] = "repair_budget_exhausted"
        revisions[-1]["pending_review_issues"] = issues
    return revisions, batches
