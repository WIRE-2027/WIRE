"""TBA: trace observed mismatches, augment missing dependencies and return original-source candidates."""

from __future__ import annotations

from .contracts import (
    entry_input,
    normalize_entries,
    normalize_group_analysis,
    normalize_proposals,
    original_groups,
)
from copy import deepcopy
import json
from .tools.ast import TBATools
from .tools.analysis import (
    active_test_revisions,
    merge_entries,
    grouping_tests,
    analysis_payload,
    actual_driver_context,
    collect_candidates,
    compact_guard_view,
    family,
    joined,
    proposal_pool,
    set_requirement_context,
    tool_view,
    initial_test_issues,
    repair_inputs,
    confirm_test_issues,
    update_requirement_context,
    save_tba_result,
)

from .tools.recovery import (
    recovery_jobs, proposal_input, prepare_proposal, replay_analysis_input,
    normalize_replay_analysis, normalize_recovery_proposals,
)

from .tools.execution import (
    replay_result,
    refine_test_batches,
)


def run(ctx, frtg_result, *, augment=True):
    set_requirement_context(ctx, frtg_result)
    prepared = prepare(ctx, frtg_result)
    state = analyze_original(ctx, frtg_result, prepared)
    # Freeze the pre-augmentation snapshot before later test revisions change entries or tests.
    original = finalize_candidates(ctx, frtg_result, state, state["originals"], save=not augment)
    original["scope"] = "tba-original"
    ctx.save("tba/original_result.json", original)
    if not augment:
        ctx.save("tba/result.json", original)
        return original
    recovered = recover_relations(ctx, frtg_result, state)
    return finalize_candidates(ctx, frtg_result, state, recovered)


def prepare(ctx, frtg_result):
    """Prepare mismatch groups and shared AST and waveform tools."""
    tests = active_test_revisions(frtg_result["tests"])
    entries = merge_entries(tests)
    box = TBATools(ctx, frtg_result["tests"], entries)
    families, groups, batches = {}, [], []
    for entry in entries:
        families.setdefault(family(box, entry), []).append(entry)
    for index, (key, members) in enumerate(families.items(), 1):
        data = {
            **ctx.model_input(),
            "requirements": frtg_result["requirements"],
            "entries": [entry_input(e, box._record("entry", e)) for e in members],
            "tests": grouping_tests(
                box, [box._test(t) for t in dict.fromkeys(e["test"] for e in members)]
            ),
        }
        if drivers := actual_driver_context(ctx, box, members):
            data["actual_drivers"] = drivers
        if len(members) == 1:
            grouped = normalize_entries(
                {
                    "groups": [
                        {
                            "representative": members[0]["id"],
                            "members": [members[0]["id"]],
                            "reason": "Single observed entry",
                        }
                    ]
                },
                members,
            )
        else:
            grouped = ctx.ask(
                f"tba_groups_{index}",
                "tba/entries",
                data,
                normalize=lambda value: normalize_entries(value, members),
            )
        for group in grouped:
            group = {**group, "id": f"G{len(groups)+1}", "family": key}
            groups.append(group)
            ids = [
                group["representative"],
                *[e for e in group["members"] if e != group["representative"]],
            ]
            payload = analysis_payload(
                ctx, box, frtg_result["requirements"], [box.entries[e] for e in ids]
            )
            payload["group"] = group
            batches.append(payload)
    membership = {e: g["id"] for g in groups for e in g["members"]}
    mapping = [
        {
            "mismatch_id": o["mismatch_id"],
            "check": o["check"]["id"],
            "entry": e["id"],
            "group": membership[e["id"]],
            "test": e["test"],
        }
        for e in entries
        for o in e["observations"]
    ]
    ctx.save("tba/groups.json", groups)
    ctx.save("tba/mismatch-map.json", mapping)
    ctx.save("tba/all_entries.json", entries)
    ctx.save(
        "tba/active-tests.json",
        {
            "selected": [t["id"] for t in tests],
            "history": [t["id"] for t in frtg_result["tests"]],
        },
    )
    return box, batches


def analyze_original(ctx, frtg_result, prepared):
    """Trace original mismatches and revise tests only for reported testbench errors."""

    box, batches = prepared
    tests, repairs, originals, by_family = list(frtg_result["tests"]), [], [], {}
    attempted = set()
    analyzed_states = set()
    repaired_tests = set()
    repair_outcomes = {}
    pending_batches = list(batches)
    index = 0
    while index < len(pending_batches):
        payload = pending_batches[index]
        index += 1
        members = [e["id"] for e in payload["entries"]]
        # Analyze each test revision once, even if several entries enqueue it.
        state_key = (
            payload["group"].get("family"),
            tuple(members),
            tuple(
                sorted(
                    (
                        test.get("id"),
                        (test.get("tb") or {}).get("code", ""),
                    )
                    for test in tests
                    if test.get("id") in {box.entries[e]["test"] for e in members}
                )
            ),
        )
        state_digest = json.dumps(state_key, ensure_ascii=False, sort_keys=True)
        if state_digest in analyzed_states:
            continue
        analyzed_states.add(state_digest)
        normalize = lambda value, p=payload: normalize_group_analysis(
            value, ctx.sources, p
        )
        bindings = {
            k: {**v, "present": tool_view}
            for k, v in box.model_bindings(members=members).items()
        }
        analysis = analyze_original_group(ctx, index, payload, bindings, normalize)
        key = payload["group"]["family"]
        if analysis.get("tb_errors"):
            known_issues = analysis["tb_errors"]
            affected = {
                box.entries[e]["test"]
                for issue in known_issues
                for e in issue["entries"]
            }
            if not affected:
                affected = {
                    box.entries[e]["test"]
                    for e in payload["entries"]
                    if e in box.entries
                }
            if not affected:
                continue
            test = next(t for t in reversed(tests) if t["id"] in affected)
            issues = [
                issue
                for issue in known_issues
                if any(box.entries[e]["test"] == test["id"] for e in issue["entries"])
            ]
            # Share one revision of a testbench across groups that report errors in that version.
            if test["id"] not in repaired_tests:
                revisions, fresh = refine_test_batches(
                    ctx,
                    box,
                    payload,
                    test,
                    issues,
                    frtg_result["requirements"],
                    index,
                    attempted,
                )
                repaired_tests.add(test["id"])
                repaired_tests.update(revision["id"] for revision in revisions)
                if revisions:
                    repair_outcomes[key] = revisions[-1]
                repairs.extend(revisions)
                tests.extend(revisions)
                pending_batches.extend(fresh)
        confirm_test_issues(analysis, repair_outcomes.get(key, {}), box)
        analysis["group"] = payload["group"]
        ctx.save(f"tba/analyses/initial_{index}.json", analysis)
        originals.append(deepcopy(analysis))
        by_family.setdefault(key, []).append((payload, analysis))
    excluded = {
        e
        for a in originals
        for issue in a.get("tb_errors", [])
        for e in issue["entries"]
    }
    initial_candidates = collect_candidates(box, originals, excluded=excluded)
    ctx.save("tba/initial_candidates.json", initial_candidates)
    update_requirement_context(ctx, frtg_result, tests, repairs)
    return {
        "box": box,
        "tests": tests,
        "repairs": repairs,
        "batches": pending_batches,
        "originals": originals,
        "by_family": by_family,
        "excluded": excluded,
        "initial_candidates": initial_candidates,
    }


def analyze_original_group(ctx, index, payload, bindings, normalize):
    data = {k: v for k, v in payload.items() if k != "path_coverage_version"}
    data["group"] = {k: payload["group"][k] for k in ("id", "representative")}
    return ctx.agent(
        f"tba_analysis_{index}",
        "tba/analyze",
        compact_guard_view(data),
        bindings,
        normalize=normalize,
    )


def recover_relations(ctx, frtg_result, state):
    """Propose independent augmentations, replay the same tests and analyze augmented paths."""
    box, requirements = state["box"], frtg_result["requirements"]
    finals = [deepcopy(a) for a in state["originals"]]
    prepared_tests, seen = {}, set()
    for index, job in enumerate(recovery_jobs(ctx, state), 1):
        data = proposal_input(ctx, box, job, requirements, augmentation_input(ctx))
        edits = propose_relations(ctx, f"tba_proposals_{index}", data,
            normalize=lambda value, j=job: normalize_recovery_proposals(value, j, requirements))
        report = {**joined([]), "proposals": edits, "replays": []}
        report["unresolved"].extend(
            {"entries": job["members"], "requirements": p.get("requirements", []),
             "reason": p.get("reason", "Augmentation needs further information")}
            for p in edits.get("deferred", []) if not p.get("edits")
        )
        for number, proposal in enumerate(proposal_pool(edits), 1):
            name = f"tba_recovery_{index}_{number}"
            try:
                bound = prepare_proposal(ctx, box, proposal, job, requirements,
                                         state, prepared_tests, name)
                identity = json.dumps(bound, sort_keys=True)
                if identity in seen:
                    continue
                seen.add(identity)
                outcome = replay_result(box, bound)
                report["replays"].append(outcome)
                ctx.save(f"tba/replays/{index}_{number}.json", outcome)
                if outcome.get("error"):
                    raise ValueError(outcome["error"])
                # A remaining mismatch may reveal an upstream error through the added dependency.
                payload, bindings = replay_analysis_input(ctx, box, outcome, requirements)
                review = ctx.agent(
                    name + "_analysis", "tba/analyze", compact_guard_view(payload), bindings,
                    normalize=lambda value, p=payload: normalize_replay_analysis(
                        value, ctx, p["entries"], requirements),
                )
                review["variant"] = outcome["variant"]
                ctx.save(f"tba/analyses/augmented_{index}_{number}.json", review)
                finals.append(review)
            except (ValueError, KeyError, TypeError) as error:
                report["unresolved"].append({
                    "entries": proposal.get("entries") or ([proposal["entry"]] if proposal.get("entry") else []),
                    "requirements": proposal.get("requirements", []), "reason": str(error),
                })
        finals.append(report)
    return finals


def augmentation_input(ctx):
    """Provide the complete Spec and RTL with original source line numbers."""
    data = ctx.model_input()
    files = set(data["rtl"]) | {
        file
        for file in ctx.sources
        if file.lower().endswith((".v", ".sv", ".vh", ".svh"))
    }
    data["rtl"] = {
        file: "\n".join(
            f"{i}: {line}" for i, line in enumerate(ctx.sources[file].splitlines(), 1)
        )
        for file in sorted(files)
    }
    return data


def propose_relations(ctx, name, data, normalize=normalize_proposals):
    return ctx.ask(
        name,
        "tba/propose",
        compact_guard_view(data),
        normalize=normalize,
    )


def finalize_candidates(ctx, frtg_result, state, finals, *, save=True):
    """Merge candidates from original and augmented analyses at original source locations."""

    box, tests, repairs = state["box"], state["tests"], state["repairs"]
    batches = state["batches"]
    excluded, initial_candidates = state["excluded"], state["initial_candidates"]
    final_candidates = collect_candidates(
        box, finals, excluded=excluded
    )
    if save:
        ctx.save("tba/final_candidates.json", final_candidates)
    result = {
        "requirements": frtg_result["requirements"],
        "tests": tests,
        "tb_repairs": repairs,
        "all_entries": list(box.entries.values()),
        "analyses": finals,
        "initial_candidates": initial_candidates,
        "final_candidates": final_candidates,
        "candidates": final_candidates,
        "augmentations": box.augmentations,
        "unresolved": [x for a in finals for x in a["unresolved"]],
        "tb_errors": [x for a in finals for x in a.get("tb_errors", [])],
        "records": list(box.materials),
        "groups": [g for p in batches for g in original_groups(p)],
        "scope": "tba",
        "entries": [
            {
                **box.entries[p["group"]["representative"]],
                "members": p["group"]["members"],
            }
            for p in batches
        ],
    }
    if save:
        save_tba_result(ctx, box, batches, result)
    return result
