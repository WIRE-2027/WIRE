import copy
from ..model import INPUT_VIEW_VERSION
import hashlib
import json
import re
from ..contracts import digest


def stable(text):

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def numbered(text):

    return "\n".join(
        (f"{i:04d} | {line}" for i, line in enumerate(text.splitlines(), 1))
    )


def hunks(row):

    result = set()
    for loc in [{"file": row["file"], "lines": row["lines"]}]:
        for lo, hi in loc["lines"]:
            if type(lo) is not int or type(hi) is not int or (not 1 <= lo <= hi):
                raise ValueError("Invalid original anchor")
            result.add((loc["file"], lo, hi))
    return result


def units_from_prefix(rows):

    components = []
    for row in rows:
        current = {"rows": [row], "hunks": hunks(row)}
        changed = True
        while changed:
            changed = False
            other = []
            for component in components:
                if current["hunks"] & component["hunks"]:
                    current["rows"] += component["rows"]
                    current["hunks"].update(component["hunks"])
                    changed = True
                else:
                    other.append(component)
            components = other
        components.append(current)
    units = []
    for component in components:
        locations = sorted(component["hunks"])
        uid = "H_" + stable(json.dumps(locations, separators=(",", ":")))[:14]
        units.append({"id": uid, "hunks": locations, "rows": component["rows"]})
    return sorted(units, key=lambda unit: stable("20261026/r10/" + unit["id"]))


def normalized_observations(m2):

    keys = (
        "id",
        "test",
        "backend",
        "run_status",
        "signal",
        "time",
        "cycle",
        "bits",
        "expected",
        "actual",
        "requirements",
        "observations",
        "source_id",
        "test_source_id",
        "run_id",
        "trial_id",
        "kind",
    )
    values = [
        {key: entry[key] for key in keys if key in entry}
        for entry in m2.get("all_entries", [])
    ]
    ids = [value["id"] for value in values]
    if len(ids) != len(set(ids)):
        raise ValueError("Ambiguous original observation IDs")
    return values


def validate_order(value, prepared, challenge=False):

    if not isinstance(value, dict):
        raise ValueError("Need JSON object")
    ids = {unit["id"] for unit in prepared["units"]}
    groups = value.get("rank_groups")
    if not isinstance(groups, list) or any(
        (not isinstance(group, list) or not group for group in groups)
    ):
        raise ValueError("Need nonempty rank groups")
    flattened = [uid for group in groups for uid in group]
    if (
        any((not isinstance(uid, str) for uid in flattened))
        or len(flattened) != len(ids)
        or set(flattened) != ids
    ):
        raise ValueError("Every supplied unit must appear exactly once")
    assessments = value.get("assessments")
    if not isinstance(assessments, list) or len(assessments) != len(ids):
        raise ValueError("Assess every supplied unit")
    seen = set()
    available = set(prepared["observation_ids"])
    for row in assessments:
        if not isinstance(row, dict) or row.get("id") not in ids or row["id"] in seen:
            raise ValueError("Unknown/duplicate assessment ID")
        seen.add(row["id"])
        if row.get("status") not in {
            "source_conflict",
            "needs_test",
            "no_conflict",
            "insufficient",
        }:
            raise ValueError("Invalid status")
        if row.get("ownership") not in {
            "error_source",
            "faithful_propagation",
            "compensation",
            "uncertain",
        }:
            raise ValueError("Invalid ownership")
        spans = row.get("spec_lines")
        if not isinstance(spans, list):
            raise ValueError("Need original Spec intervals")
        count = 0
        for span in spans:
            if (
                not isinstance(span, list)
                or len(span) != 2
                or any((type(x) is not int for x in span))
            ):
                raise ValueError("Invalid Spec span")
            lo, hi = span
            if not 1 <= lo <= hi <= prepared["spec_line_count"]:
                raise ValueError("Spec span outside original")
            count += hi - lo + 1
        if count > 24:
            raise ValueError("Cite at most24 Spec lines per unit")
        obs = row.get("observation_ids")
        if not isinstance(obs, list) or any(
            (not isinstance(x, str) or x not in available for x in obs)
        ):
            raise ValueError("Unknown observation citation")
        for key in ("trigger", "mechanism", "strongest_alternative"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError("Need " + key)
        if row["status"] == "source_conflict" and (not spans):
            raise ValueError("Source conflict needs applicable original Spec")
    if (
        not isinstance(value.get("first_group_basis"), str)
        or not value["first_group_basis"].strip()
    ):
        raise ValueError("Need first-group basis")
    if not isinstance(value.get("limitations"), list) or any(
        (not isinstance(x, str) for x in value["limitations"])
    ):
        raise ValueError("Need limitations array")
    if challenge and value.get("challenge_decision") not in {
        "uphold",
        "revise",
        "uncertain",
    }:
        raise ValueError("Need challenge decision")
    return value


def compact_result(result):

    run = result["runs"]["verilator"]
    keys = (
        "id",
        "requirement",
        "signal",
        "time",
        "cycle",
        "expected",
        "actual",
        "passed",
        "valid",
        "expected_valid",
        "waveform_valid",
        "validation_error",
    )
    return {
        "source_id": result.get("source_id"),
        "status": run["status"],
        "execution": run.get("execution"),
        "checks": [
            {key: row[key] for key in keys if key in row}
            for row in run.get("checks", [])
        ],
        "checker_errors": run.get("checker_errors", []),
        "log_tail": (
            run.get("log", "")[-6000:] if run["status"] == "compile_error" else ""
        ),
    }


def columnar_result(result, include_scalar_fields=False):

    value = compact_result(result)
    checks = value.pop("checks")
    if include_scalar_fields:
        original = result.get("runs", {}).get("verilator", {}).get("checks", [])
        for row, source in zip(checks, original):
            row.update({key: item for key, item in source.items()
                        if item is None or isinstance(item, (str, int, float, bool))})
    columns = sorted({key for row in checks for key in row})
    absent = {
        str(index): [i for i, key in enumerate(columns) if key not in row]
        for index, row in enumerate(checks)
    }
    value["checks_table"] = {
        "columns": columns,
        "rows": [[row.get(key) for key in columns] for row in checks],
        "absent_columns_by_row": {
            index: missing for index, missing in absent.items() if missing
        },
    }
    return value


def public_context(prepared):

    case = prepared["comparison"]
    payload = case["payload"]
    return {
        "original_spec_lines": payload["original_spec_lines"],
        "original_sources": {
            name: numbered(code)
            for name, code in prepared["original"]["sources"].items()
        },
        "fallible_M1_requirements": [
            {
                key: row[key]
                for key in (
                    "id",
                    "requirement",
                    "text",
                    "loc",
                    "trigger",
                    "expected",
                    "reason",
                )
                if key in row
            }
            for row in prepared["m1"].get("requirements", [])
        ],
        "original_M2_observations": payload["existing_observations"],
        "original_testbench_context": payload["original_testbench_context"],
        "saved_TB_runs": {
            tid: columnar_result(test) for tid, test in case["baseline_replays"].items()
        },
    }


def contract_input(prepared, ledger):

    return {
        **public_context(prepared),
        "fixed_target_ledger": ledger,
        "candidate_units": prepared["comparison"]["payload"]["candidate_units"],
    }


def source_input(original):

    return {
        "spec": original["spec"],
        "original_rtl": {
            name: numbered(code) for name, code in original["sources"].items()
        },
        "build": {
            key: copy.deepcopy(original["build"][key])
            for key in (
                "target", "targets", "top", "compile_files", "include_dirs",
                "defines", "verilator_flags",
            )
            if key in original.get("build", {})
        },
    }


def text_field(row, key):
    if not isinstance(row.get(key), str) or not row[key].strip():
        raise ValueError("Need nonempty " + key)


def string_list(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError("Need string array: " + name)


def quote_text(text):

    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return " ".join(text.replace("\u2010", "-").replace("\u2011", "-").split())


def validate_findings(value, original):

    if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
        raise ValueError("Need findings array")
    string_list(value.get("limitations"), "limitations")
    seen = set()
    spec = " ".join(original["spec"].split())
    for row in value["findings"]:
        if not isinstance(row, dict):
            raise ValueError("Finding must be an object")
        for key in (
            "id", "file", "operation", "spec_quote", "condition", "expected",
            "actual", "defense",
        ):
            text_field(row, key)
        if row["id"] in seen:
            raise ValueError("Duplicate finding ID")
        seen.add(row["id"])
        if row["file"] not in original["sources"]:
            raise ValueError("Finding must reference an original source file")
        source = original["sources"][row["file"]]
        count = len(source.splitlines())
        lines = row.get("lines")
        if (
            not isinstance(lines, list) or not lines
            or any(type(line) is not int or not 1 <= line <= count for line in lines)
            or len(set(lines)) != len(lines)
        ):
            raise ValueError("Finding needs distinct valid original line numbers")
        if " ".join(row["operation"].split()) not in " ".join(source.split()):
            segment = "\n".join(source.splitlines()[min(lines) - 1:max(lines)])
            normalized_segment = " ".join(segment.split())
            offset = 0
            for fragment in row["operation"].splitlines():
                fragment = " ".join(fragment.split())
                if not fragment:
                    continue
                start = normalized_segment.find(fragment, offset)
                if start < 0:
                    raise ValueError("operation must quote the original source text")
                offset = start + len(fragment)
            row["operation"] = segment
        if " ".join(row["spec_quote"].split()) not in spec:
            quotation = quote_text(row["spec_quote"])
            matches = {line.strip() for line in original["spec"].splitlines()
                       if quotation and quotation in quote_text(line)}
            if len(matches) != 1:
                raise ValueError("spec_quote must quote the supplied specification")
            row["spec_quote"] = matches.pop()
        if row.get("status") not in {"supported", "unresolved"}:
            raise ValueError("Finding status must be supported or unresolved")
        string_list(row.get("missing"), "missing")
        if row["status"] == "supported" and row["missing"]:
            raise ValueError("Supported finding cannot have missing material premises")
    return value


def validate_mapping(value, findings, rows):

    if not isinstance(value, dict):
        raise ValueError("Need mapping JSON object")
    prior_order = [row["candidate_id"] for row in rows]
    catalog = {row["candidate_id"]: row for row in rows}
    aliases = {}
    for cid, candidate in catalog.items():
        aliases.setdefault(frozenset(hunks(candidate)), set()).add(cid)
    order = value.get("ranked_candidate_ids")
    string_list(order, "ranked_candidate_ids")
    if len(order) != len(prior_order) or set(order) != set(prior_order):
        raise ValueError("Ranking must contain every original candidate ID exactly once")
    mappings = value.get("mappings")
    if not isinstance(mappings, list) or len(mappings) != len(findings):
        raise ValueError("Return one mapping per supplied finding")
    finding_map = {row["id"]: row for row in findings}
    seen, verified = set(), set()
    for row in mappings:
        if not isinstance(row, dict):
            raise ValueError("Mapping must be an object")
        text_field(row, "finding_id")
        fid = row["finding_id"]
        if fid not in finding_map or fid in seen:
            raise ValueError("Unknown or duplicate finding mapping")
        seen.add(fid)
        text_field(row, "reason")
        ids = row.get("candidate_ids")
        string_list(ids, "candidate_ids")
        if len(ids) != len(set(ids)) or not set(ids) <= set(catalog):
            raise ValueError("Unknown or duplicate mapped candidate ID")
        status = row.get("status")
        if status not in {"verified", "unmapped", "rejected"}:
            raise ValueError("Mapping status must be verified, unmapped or rejected")
        if status != "verified":
            if ids:
                raise ValueError("Unmapped or rejected findings need empty candidate_ids")
            continue
        if not ids:
            raise ValueError("Verified finding needs matched original candidates")
        finding = finding_map[fid]
        for cid in ids:
            candidate = catalog[cid]
            if candidate["file"] != finding["file"] or not any(
                lo <= line <= hi
                for lo, hi in candidate["lines"] for line in finding["lines"]
            ):
                raise ValueError("Verified mapping must contain a decisive line in the same file")
            if not aliases[frozenset(hunks(candidate))] <= set(ids):
                raise ValueError("Verified mapping must include all IDs at the identical original anchor")
        verified.update(ids)
    if set(order[:len(verified)]) != verified:
        raise ValueError("Place verified mapped candidates before all other candidates")
    if order[len(verified):] != [cid for cid in prior_order if cid not in verified]:
        raise ValueError("Unverified candidates must keep their prior relative order")
    top = value.get("top_choice")
    if order:
        if not isinstance(top, dict) or top.get("candidate_id") != order[0]:
            raise ValueError("top_choice must reference the first ranked candidate")
        text_field(top, "reason")
    elif top is not None:
        raise ValueError("Empty candidate domain needs top_choice=null")
    string_list(value.get("limitations"), "limitations")
    return value


def ranking_result(prepared, order, ranking, comparison):

    if len(order) != len(set(order)) or set(order) != set(prepared["input_order"]):
        raise ValueError("Final ranking must preserve every candidate ID exactly once")
    catalog = {row["id"]: row for row in prepared["catalog"]}
    scores = {row["candidate_id"]: row for row in ranking["ranked_candidates"]}
    rows = []
    for rank, cid in enumerate(order, 1):
        original = copy.deepcopy(catalog[cid])
        score = copy.deepcopy(scores.get(cid, {}))
        rows.append(
            {
                **original,
                **score,
                "id": cid,
                "candidate_id": cid,
                "rank": rank,
                "ranked_by_model": cid in scores,
                "file": original["file"],
                "lines": original["lines"],
            }
        )
    return {
        "method_version": "CV-common-observations-heuristic-v1",
        "candidate_ranking": rows,
        "ranked_candidates": rows,
        "comparison": comparison,
        "unresolved": copy.deepcopy(prepared["m2"].get("unresolved", [])),
        "uncertain": bool(ranking.get("uncertain") or comparison.get("uncertain")),
        "limitations": list(
            dict.fromkeys(
                ranking.get("limitations", []) + comparison.get("limitations", [])
            )
        ),
    }


def ask_stage(ctx, name, data, normalize, prompt_name=None, tools=None):

    from ..model import load_prompt

    prompt_name = prompt_name or ("discriminate" if name.startswith("discriminate_") else name)
    binding = digest(
        {
            "data": data,
            "input_view_version": INPUT_VIEW_VERSION,
            "tools": [tool["schema"] for tool in tools.values()] if tools else None,
            "prompt": load_prompt(
                "cv/" + prompt_name
            ),
            "model": {
                key: getattr(ctx.model, key, None)
                for key in ("base_url", "model", "options")
            },
        }
    )
    arguments = ("cv_" + name + "_" + binding[:16], "cv/" + prompt_name, data)
    if tools is None:
        result = ctx.ask(*arguments, normalize=normalize)
    else:

        result = ctx.agent(*arguments, tools=tools, normalize=normalize)
    ctx.save("cv/" + name + ".json", {"input_id": binding, "output": result})
    return result


def prepare_inputs(original, m1, m2):

    original, m1, m2 = map(copy.deepcopy, (original, m1, m2))
    if not isinstance(original.get("spec"), str) or not isinstance(
        original.get("sources"), dict
    ):
        raise ValueError("Original input needs Spec and source mapping")
    sources = original["sources"]
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in sources.items()):
        raise ValueError("Source names and contents must be strings")
    catalog = m2.get("final_candidates", m2.get("candidates", []))
    if not isinstance(catalog, list) or any(
        not isinstance(row, dict) for row in catalog
    ):
        raise ValueError("M2 candidates must be objects")
    ids = [row.get("id") for row in catalog]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(ids) != len(
        set(ids)
    ):
        raise ValueError("Candidates need unique nonempty string IDs")
    for row in catalog:
        if (
            row.get("file") not in sources
            or not isinstance(row.get("lines"), list)
            or not row["lines"]
        ):
            raise ValueError("Candidate needs original file and line spans")
        for span in row["lines"]:
            if (
                not isinstance(span, (list, tuple))
                or len(span) != 2
                or any(type(n) is not int for n in span)
            ):
                raise ValueError("Invalid candidate line span")
            if not 1 <= span[0] <= span[1] <= len(sources[row["file"]].splitlines()):
                raise ValueError("Candidate anchor outside original source")
    tests = m2.get("tests", [])
    test_ids = [row.get("id") for row in tests]
    if any(not isinstance(tid, str) or not tid for tid in test_ids) or len(
        test_ids
    ) != len(set(test_ids)):
        raise ValueError("Tests need unique nonempty IDs")
    for test in tests:
        test.setdefault("runs", {}).setdefault(
            "verilator", {"status": "not_acquired", "checks": []}
        )
    units = units_from_prefix(catalog)
    cards = []
    for unit in units:
        claims = [
            {"text": reason, "declared_trigger": None, "declared_spec_lines": []}
            for row in unit["rows"]
            for reason in row.get("reasons", [])
        ]
        unique = {
            json.dumps(claim, sort_keys=True, ensure_ascii=False): claim
            for claim in claims
        }
        cards.append(
            {
                "id": unit["id"],
                "anchors": [
                    {
                        "file": name,
                        "lines": [lo, hi],
                        "original_text": "\n".join(
                            sources[name].splitlines()[lo - 1 : hi]
                        ),
                    }
                    for name, lo, hi in unit["hunks"]
                ],
                "fallible_claims": [
                    unique[key] for key in sorted(unique, key=stable)[:3]
                ],
                "linked_existing_observation_ids": sorted(
                    {eid for row in unit["rows"] for eid in row.get("entries", [])}
                ),
            }
        )
    observations = normalized_observations(m2)

    testbenches = [
        {
            "test_id": test["id"],
            "declared_source_id": test.get("source_id"),
            "original_tb_code_sha256": stable(test.get("tb", {}).get("code", "")),
            "top": test.get("tb", {}).get("top"),
            "original_tb_lines": numbered(test.get("tb", {}).get("code", "")),
            "original_build": test.get("build"),
            "defines": test.get("tb", {}).get(
                "defines", original.get("build", {}).get("defines", [])
            ),
            "assigned_requirements": test.get("assigned_requirements", []),
        }
        for test in tests
    ]
    comparison = {
        "units": units,
        "observation_ids": [row["id"] for row in observations],
        "spec_line_count": len(original["spec"].splitlines()),
        "baseline_replays": {test["id"]: test for test in tests},
        "payload": {
            "original_spec_lines": numbered(original["spec"]),
            "candidate_units": cards,
            "existing_observations": observations,
            "original_testbench_context": testbenches,
        },
    }
    return {
        "comparison": comparison,
        "catalog": catalog,
        "original": original,
        "m1": m1,
        "m2": m2,
        "input_order": ids,
        "unit_candidates": {
            unit["id"]: [
                row["id"]
                for row in sorted(unit["rows"], key=lambda row: ids.index(row["id"]))
            ]
            for unit in units
        },
    }
