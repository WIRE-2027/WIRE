"""Counterfactual validation: shared observations, heuristic ranking and LLM refinement."""

from .contracts import ContractError
from .model import APIError

from .tools.cv_context import prepare_inputs, ask_stage, ranking_result, validate_findings
from .tools.cv_counterfactual import (
    context, review_context, validate_plans, validate_ranking,
    replay, observations, trial, history_view, public_view, share_run_records,
    sample_probes,
)
from .tools.cv_scoring import flatten_observations, heuristic_ranking


def run(ctx, m1_result, m2_result, *, interventions=None):
    original = {"spec": ctx.spec, "sources": ctx.sources, "build": ctx.build}
    prepared = prepare_inputs(original, m1_result, m2_result)
    ids = prepared["input_order"]
    public = context(prepared)
    tests = prepared["m2"].get("tests", [])
    histories = []
    initial_ranking = []
    if ids:
        baseline = replay(ctx, "baseline", ctx.sources, tests)
        public["baseline"] = observations(baseline, declarations=True)
        presented = public_view(ctx, public, inline=True)
        linked = {entry for candidate in public["candidates"] for entry in candidate.get("entries", [])}
        entries = [entry for entry in public["entries"] if entry["id"] in linked]
        if linked - {entry["id"] for entry in entries}:
            raise ValueError("Candidate refers to an unknown trace entry")
        presented["observation_traces"] = entries
        if interventions is None:
            plans = ask_stage(ctx, "intervene", presented,
                               lambda v: validate_plans(v, ids, tests, entries))
        else:
            plans = validate_plans(interventions, ids, tests, entries)
            ctx.save("cv/reused_interventions.json", plans)
        observation_sets = plans["observation_sets"]
        baseline_samples = sample_probes(baseline, flatten_observations(observation_sets))
        unresolved = {row["candidate"]: row for row in plans["unresolved"]}
        proposals = {row["candidate"]: row for row in plans["interventions"]}

        def inspect_candidate(index, cid):
            history = {"candidate": cid, "attempts": [], "status": "unresolved"}
            if cid in unresolved:
                history["need"] = unresolved[cid]["need"]
            else:
                record = trial(ctx, f"candidate_{index}/attempt_0", proposals[cid], tests, baseline,
                               observation_sets=observation_sets, baseline_samples=baseline_samples)
                history.update(attempts=[record], status=record["status"])
            ctx.save(f"cv/candidate_{index}/execution.json", history)
            return history

        histories = [inspect_candidate(index, cid) for index, cid in enumerate(ids, 1)]
        ctx.save("cv/counterfactual.json", histories)
        initial_ranking = heuristic_ranking(ids, histories, observation_sets)
        ranking_input = share_run_records({
            **presented,
            "observation_sets": observation_sets,
            "observation_limitations": plans["observation_limitations"],
            "initial_ranking": initial_ranking,
            "counterfactual": [history_view(ctx, h, inline=True) for h in histories],
        })
        try:
            diagnosis = ask_stage(
                ctx, "source_diagnose", {"spec": public["spec"], "original_rtl": public["rtl"]},
                lambda value: validate_findings(value, original),
            )
        except (APIError, ContractError) as error:
            diagnosis = {"findings": [], "limitations": ["Source diagnosis unavailable: " + str(error)]}
            ctx.save("cv/source_diagnose_error.json", {"error": str(error)})
        ranking = ask_stage(
            ctx, "review", review_context(prepared, ranking_input, diagnosis),
            lambda value: validate_ranking(value, ids, public["requirements"]),
        )
    else:
        diagnosis = {"findings": [], "limitations": []}
        ranking = {"ranked_candidates": [], "limitations": []}
    order = [row["candidate_id"] for row in ranking["ranked_candidates"]]
    result = ranking_result(prepared, order, ranking, {
        "counterfactual": histories, "source_diagnosis": diagnosis,
    })
    result["method_version"] = "CV-common-observations-heuristic-v1"
    result["reused_initial_interventions"] = interventions is not None
    ctx.save("cv/result.json", result)
    return result
