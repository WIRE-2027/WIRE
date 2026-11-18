"""Prepare checks, temporal traces, augmentation results and original-source candidates."""

from __future__ import annotations

from ..contracts import (
    ContractError,
    digest,
    location_ranges,
    entry_input,
    provided_constants,
    observations,
    measured_test,
    tb_feedback,
    original_groups,
)
from copy import deepcopy
from difflib import unified_diff
from hashlib import sha256
import json
from .source import related_dependency_context


def pack_traces(materials):
    """Share repeated statements and diagnostics while preserving each event and observed value."""
    tables = {key: {} for key in ("statements", "events", "diagnostics")}
    indices = {key: {} for key in tables}

    def intern(table, value):
        key = digest(value)
        if key not in indices[table]:
            identity = {"statements": "ST", "events": "EV", "diagnostics": "DG"}[
                table
            ] + str(len(indices[table]) + 1)
            indices[table][key] = identity
            tables[table][identity] = value
        return indices[table][key]

    event_fields = {
        "observed_time",
        "source_time",
        "active_clock_edge",
        "activity",
        "active_sources",
        "values",
    }
    traces = []
    for mid, material in materials:
        trace = {
            k: deepcopy(v)
            for k, v in material.items()
            if k
            not in {"relations", "suppressed", "uncertain", "visited", "unexpanded"}
        }
        trace["material_id"] = mid
        trace["events"] = []
        for row in material.get("relations", []):
            statement = intern(
                "statements", {k: v for k, v in row.items() if k not in event_fields}
            )
            event = {
                "statement": statement,
                **{k: v for k, v in row.items() if k in event_fields},
            }
            trace["events"].append(intern("events", event))
        for field in ("suppressed", "uncertain", "visited", "unexpanded"):
            if field in material:
                trace[field] = [intern("diagnostics", row) for row in material[field]]
        traces.append(trace)
    return {**tables, "traces": traces}


def compact_diagnostic_times(material):
    """Group equivalent diagnostics and encode regular timestamp sequences losslessly."""
    value = dict(material)
    for field in ("suppressed", "uncertain", "visited", "unexpanded"):
        if field not in material:
            continue
        grouped, compact = {}, []
        for row in material[field]:
            if not isinstance(row, dict) or "time" not in row or "times" in row:
                compact.append(deepcopy(row))
                continue
            base = {k: v for k, v in row.items() if k != "time"}
            key = digest(base)
            if key not in grouped:
                grouped[key] = {**base, "times": []}
                compact.append(grouped[key])
            grouped[key]["times"].append(row["time"])
        for row in grouped.values():
            times = row["times"]
            if (
                len(times) >= 3
                and all(type(t) is int for t in times)
                and all(b - a == times[1] - times[0] for a, b in zip(times, times[1:]))
            ):
                row["times"] = {
                    "start": times[0],
                    "step": times[1] - times[0],
                    "count": len(times),
                }
        value[field] = compact
    return value


def trace_view(page):
    """Expand paginated trace records while retaining material identity and continuation offsets."""
    event_fields = {
        "observed_time",
        "source_time",
        "active_clock_edge",
        "activity",
        "active_sources",
        "values",
    }
    value = {
        k: deepcopy(v)
        for k, v in page.items()
        if k not in {"material_id", "items", "statements"}
    }
    value["relations"] = [
        {
            **page["statements"][event["statement"]],
            **{k: v for k, v in event.items() if k in event_fields},
        }
        for event in page["items"]
    ]
    return page["material_id"], compact_diagnostic_times(value)


def observation_sources(box, entries):
    """Follow testbench dependencies to RTL producers when direct trace paths are missing."""
    records, origins = {}, []
    for entry in entries:
        graph = box.graph(entry["test"])
        drivers = {}
        for row in graph.get("relations", []):
            drivers.setdefault(row["target"], []).append(row)
        frontier, seen, rtl, relations = [entry["signal"]], set(), set(), []
        while frontier:
            signal = frontier.pop()
            if signal in seen:
                continue
            seen.add(signal)
            rows = drivers.get(signal, [])
            if any(row["file"] in box.ctx.sources for row in rows):
                rtl.add(signal)
                continue
            for row in rows:
                if row["file"] != "__static_tb.sv":
                    continue
                identity = entry["test"] + ":" + row["id"]
                records[identity] = {
                    "test": entry["test"],
                    **{
                        k: row[k]
                        for k in (
                            "target",
                            "sources",
                            "guards",
                            "clock_senses",
                            "expression",
                            "line",
                        )
                    },
                }
                relations.append(identity)
                frontier.extend(row["sources"])
                frontier.extend(s for g in row["guards"] for s in g["signals"])
        origins.append(
            {
                "entry": entry["id"],
                "tb_relations": list(dict.fromkeys(relations)),
                "rtl_frontier": sorted(rtl),
                "unmatched_start": entry["signal"] not in drivers,
            }
        )
    scope = "Static TB observation dependencies using exact compiler names. These identify signals to inspect; values, array-element identity and event order require waveform/TB observations."
    return {"scope": scope, "relations": records, "origins": origins}


def relation_brief(row):
    fields = (
        "id",
        "test",
        "target",
        "sources",
        "index_sources",
        "guards",
        "clock_senses",
        "sequential",
        "port_bridge",
        "file",
        "original_line",
        "source",
        "context_roles",
        "requirements",
    )
    value = {k: row[k] for k in fields if k in row}
    # Preserve branch polarity and compound conditions in summaries.
    if not row.get("source") and "expression" in row:
        value["expression"] = row["expression"]
    return value


def compact_packet(packet):
    """Summarize repeated AST structure while retaining every trace event."""
    return {
        **packet,
        "statements": {k: relation_brief(v) for k, v in packet["statements"].items()},
    }


def compact_guard_view(value):
    """Replace repeated guards with references to their complete definitions."""
    definitions = {}

    def visit(item):
        if isinstance(item, list):
            return [visit(v) for v in item]
        if not isinstance(item, dict):
            return item
        result = {}
        for key, rows in item.items():
            if key == "guards" and isinstance(rows, list):
                refs = []
                for guard in rows:
                    identity = "H_" + digest(guard)[:20]
                    definitions.setdefault(identity, guard)
                    refs.append({"ref": identity})
                result[key] = refs
            else:
                result[key] = visit(rows)
        return result

    result = visit(value)
    if definitions:
        result["guard_definitions"] = definitions
    return result


def tool_view(value):
    if not isinstance(value, dict):
        return value
    value = dict(value)
    if "statements" in value:
        value["statements"] = {
            k: relation_brief(r) for k, r in value["statements"].items()
        }
    if isinstance(value.get("items"), list):
        value["items"] = [
            (
                relation_brief(r)
                if isinstance(r, dict) and "target" in r and "guards" in r
                else r
            )
            for r in value["items"]
        ]
    return compact_guard_view(value)


def relation_signals(row):
    return {
        row["target"],
        *row.get("sources", []),
        *row.get("index_sources", []),
        *row.get("clocks", []),
        *(s for g in row.get("guards", []) for s in g["signals"]),
    }


def actual_driver_context(ctx, box, entries):
    """Validate observed driver locations against source digests and the compiler AST."""
    locations = {}
    hashes = {}
    for entry in entries:
        for observation in entry.get("observations", []):
            check = observation["check"]
            for loc in check.get("actual_driver_locations", []):
                file = loc.get("file")
                if file not in ctx.sources:
                    continue
                if file not in hashes:
                    hashes[file] = sha256(ctx.sources[file].encode()).hexdigest()
                spans = location_ranges(loc.get("lines", []))
                if (
                    loc.get("source_sha256") != hashes[file]
                    or not spans
                    or any(b > len(ctx.sources[file].splitlines()) for a, b in spans)
                    or loc.get("resolution")
                    not in ("static_driver", "waveform_confirmed")
                ):
                    continue
                signal = loc.get("signal")
                rows = [
                    row
                    for row in box.graph(entry["test"]).get("relations", [])
                    if row.get("file") == file
                    and row.get("target") == signal
                    and any(a <= row.get("original_line", -1) <= b for a, b in spans)
                ]
                if not rows:
                    continue
                key = digest([entry["id"], file, spans, signal])
                item = locations.setdefault(
                    key,
                    {
                        "entry": entry["id"],
                        "checks": [],
                        "signal": signal,
                        "file": file,
                        "lines": [list(s) for s in spans],
                        "source_sha256": hashes[file],
                        "resolution": loc["resolution"],
                        "relations": [relation_brief(row) for row in rows],
                    },
                )
                if check["id"] not in item["checks"]:
                    item["checks"].append(check["id"])
    return list(locations.values())


def local_context(ctx, box, requirements, members, origins, traces, actual_drivers=()):
    """Prepare upstream AST context and a signal directory around entries and requirement locations."""
    from .ast import backtrace
    from .waveform import list_signals

    static, directories = {}, []
    visible = set(ctx.build.get("model_files", []))
    for test in dict.fromkeys(e["test"] for e in members):
        graph = box.graph(test)
        related = {
            rid for e in members if e["test"] == test for rid in e["requirements"]
        }
        hints = [r for r in requirements if r["id"] in related]
        starts = [e["signal"] for e in members if e["test"] == test]
        member_ids = {e["id"] for e in members if e["test"] == test}
        starts += [d["signal"] for d in actual_drivers if d["entry"] in member_ids]
        starts += [
            s
            for origin in origins["origins"]
            if box.entries[origin["entry"]]["test"] == test
            for s in origin.get("rtl_frontier", [])
        ]

        def add(row, role, ids=()):
            if row.get("file") not in ctx.sources:
                return
            key = digest([test, row])
            value = static.setdefault(
                key, {"test": test, **row, "context_roles": [], "requirements": []}
            )
            value["context_roles"] = list(
                dict.fromkeys([*value["context_roles"], role])
            )
            value["requirements"] = list(dict.fromkeys([*value["requirements"], *ids]))

        for row in backtrace(graph, list(dict.fromkeys(starts)), depth=None)[
            "relations"
        ]:
            add(row, "observed_fan_in")
        inputs = {}
        for row in graph.get("relations", []):
            locations = [row, *row.get("guards", [])]
            ids = [
                r["id"]
                for r in hints
                if any(
                    any(
                        a <= location.get("original_line", -1) <= b
                        for a, b in location_ranges(
                            r.get("loc", {}).get(location.get("file"), [])
                        )
                    )
                    for location in locations
                )
            ]
            if ids:
                add(row, "requirement_location", ids)
                for signal in relation_signals(row) - {row["target"]}:
                    inputs.setdefault(signal, set()).update(ids)
        for row in graph.get("relations", []):
            if row["target"] in inputs:
                add(row, "requirement_input", sorted(inputs[row["target"]]))

        names = set(starts)
        for row in static.values():
            if row["test"] == test:
                names.update(relation_signals(row))
        for _, trace in traces:
            if trace.get("test") == test:
                for row in trace.get("relations", []):
                    names.update(relation_signals(row))
        wave = list_signals(box._run(test, "verilator").get("vcd", ""))
        available = {
            s["name"]: {"name": s["name"], "width": s["width"]}
            for s in wave["signals"]
            if s["name"] in names
        }
        # The initial directory is sampled; query_waveform retains access to all declared signals.
        priority = list(dict.fromkeys(
            signal
            for signal in [*starts, *names]
            if signal in available
        ))
        signal_limit = 4000
        selected = priority[:signal_limit]
        if len(selected) < signal_limit:
            selected.extend(
                signal
                for signal in sorted(available)
                if signal not in set(selected)
            )
            selected = selected[:signal_limit]
        signals = [available[signal] for signal in selected]
        mid = box._record(
            "relevant_signals",
            {
                "test": test,
                "signals": signals,
                "timescale": wave.get("timescale"),
                "error": wave.get("error"),
            },
        )
        directories.append(
            {
                "test": test,
                "material_id": mid,
                "signals": signals,
                "timescale": wave.get("timescale"),
                "error": wave.get("error"),
                "total": len(signals),
                "next_offset": None,
            }
        )
    ordered = sorted(
        static.values(),
        key=lambda r: (
            "requirement_location" not in r["context_roles"],
            r["file"] not in visible,
            r["file"],
            r["original_line"],
            r["target"],
            r["test"],
        ),
    )
    relation_limit = 240
    mid = box._record("static_context", {"relations": ordered})
    return {
        "material_id": mid,
        "relations": [relation_brief(r) for r in ordered[:relation_limit]],
        "total": len(ordered),
        "next_offset": relation_limit if len(ordered) > relation_limit else None,
        "scope": "Static fan-in and related requirement locations with one-hop inputs. "
        "These select operations to inspect; activity and fault candidacy are established separately.",
    }, directories


def analysis_payload(ctx, box, requirements, members):
    """Assemble requirements, paths, testbenches and source context for original or revised tests."""

    traces, gaps, path_summary = [], [], []
    for entry in members:
        page = original_page(box, entry)
        path_summary.append(
            {
                "entry": entry["id"],
                "material_id": page["material_id"],
                "total": page["total"],
                "shown": len(page.get("items", [])),
                "gap": not page["total"],
            }
        )
        traces.append(trace_view(page))
        if not page["total"]:
            gaps.append(
                {
                    "entry": entry["id"],
                    "signal": entry["signal"],
                    "reason": "No temporal RTL path returned; follow the supplied TB observation dependencies.",
                }
            )
    # When testbench intermediates hide RTL drivers, inspect related state writes once per test and requirement.
    hints, state_starts = {r["id"]: r.get("loc", {}) for r in requirements}, []
    gap_ids, seen_starts = {g["entry"] for g in gaps}, set()
    for entry in members:
        if entry["id"] not in gap_ids:
            continue
        for rid in entry["requirements"]:
            key = (entry["test"], rid)
            if key in seen_starts:
                continue
            seen_starts.add(key)
            loc = hints.get(rid, {})
            targets = sorted(
                {
                    r["target"]
                    for r in box.graph(entry["test"])["relations"]
                    if r.get("sequential")
                    and r.get("file") in loc
                    and any(
                        a <= r.get("original_line", -1) <= b
                        for a, b in location_ranges(loc[r["file"]])
                    )
                }
            )
            if targets:
                page = original_page(box, entry, signals=targets)
                traces.append(trace_view(page))
                state_starts.append(
                    {
                        "entry": entry["id"],
                        "requirement": rid,
                        "signals": targets,
                        "material_id": page["material_id"],
                    }
                )
    packet = pack_traces(traces)
    origins = observation_sources(
        box, [e for e in members if e["id"] in {g["entry"] for g in gaps}]
    )
    drivers = actual_driver_context(ctx, box, members)
    static, signals = local_context(
        ctx, box, requirements, members, origins, traces, drivers
    )
    payload = {
        **ctx.model_input(),
        "requirements": requirements,
        "entries": [entry_input(e, box._record("entry", e)) for e in members],
        "tests": grouping_tests(
            box, [box._test(t) for t in dict.fromkeys(e["test"] for e in members)]
        ),
        "trace_context": compact_packet(packet),
        "state_path_starts": state_starts,
        "observation_sources": origins,
        "local_ast": static,
        "relevant_signals": signals,
        "entry_path_summary": path_summary,
    }
    if constants := provided_constants(ctx.sources, ctx.build):
        payload["interface_constants"] = constants
    if drivers:
        payload["actual_drivers"] = drivers
    return payload


def boundary_context(box, boundaries):
    """Read assignments, controls and scope at the proposed missing-dependency anchor."""
    contexts = []
    for boundary in boundaries:
        entry = box.entries[boundary["entry"]]
        file, line = boundary["file"], boundary["line"]
        spans = location_ranges(boundary.get("lines", [])) or [(line, line)]

        def matches(node):
            number = node.get("original_line")
            return (
                node.get("file") == file
                and isinstance(number, int)
                and any(a <= number <= z for a, z in spans)
            )

        rows = [
            row
            for row in box.graph(entry["test"])["relations"]
            if matches(row) or any(matches(g) for g in row["guards"])
        ]
        direct = [
            row
            for row in box.graph(entry["test"])["relations"]
            if row["target"] == entry["signal"] and row.get("file") == file
        ]
        if direct:
            scope = entry["signal"].rsplit(".", 1)[0] + "."
            rows = [row for row in rows if row["target"].startswith(scope)]
        material = box._record(
            "augmentation_anchor",
            {"entry": entry["id"], "file": file, "line": line, "relations": rows},
        )
        contexts.append(
            {
                "entry": entry["id"],
                "file": file,
                "line": line,
                "material_id": material,
                "relations": rows,
                "total": len(rows),
                "next_offset": None,
            }
        )
    return contexts


def accepted_mismatch_subset(run, backend):
    """Verify saved mismatch subsets against their original checked records."""
    marker = run["accepted_mismatch_subset"]
    if (
        run.get("status") != "invalid_checker"
        or marker.get("scope") != "mismatches_only"
        or marker.get("individual_records_verified") is not True
        or marker.get("backend") != backend
    ):
        raise ContractError("Invalid accepted mismatch subset binding")
    expected = set(marker.get("check_sha256s", []))
    original = {digest(c) for c in run.get("checks", [])}
    records = run.get("mismatches", [])
    actual = [
        digest({k: v for k, v in c.items() if k != "actual_driver_locations"})
        for c in records
    ]
    if (
        len(records) != marker.get("count")
        or len(set(actual)) != len(actual)
        or set(actual) != expected
        or not expected <= original
        or any(
            c.get("valid") is not True
            or c.get("waveform_valid") is not True
            or c.get("passed") is not False
            or c.get("expected_valid") is False
            for c in records
        )
    ):
        raise ContractError(
            "Accepted mismatch subset differs from its verified CHECK records"
        )
    return records


def active_test_revisions(tests):
    """Use the latest valid tests while retaining older versions with unique valid checks."""
    covered, selected = {}, set()
    for index in range(len(tests) - 1, -1, -1):
        test = tests[index]
        tb = test.get("tb", {})
        stem, separator, revision = test["id"].rpartition("_r")
        family = (
            tb.get("group")
            or test.get("group_id")
            or (stem if separator and revision.isdigit() else test["id"])
        )
        key = digest(
            [family, test.get("source_id"), test.get("build"), tb.get("defines")]
        )
        checks = set()
        for backend, run in test.get("runs", {}).items():
            usable = run.get("status") == "ok" or run.get("observations_valid") is True
            records = run.get(
                "valid_checks",
                run.get("checks", run.get("mismatches", [])) if usable else [],
            )
            if run.get("accepted_mismatch_subset"):
                records = accepted_mismatch_subset(run, backend)
            for check in records:
                if not check.get("id") or any(
                    check.get(field) is False
                    for field in (
                        "valid",
                        "expected_valid",
                        "expected_known",
                        "waveform_valid",
                    )
                ):
                    continue
                requirements = check.get("requirement", [])
                requirements = (
                    [requirements] if isinstance(requirements, str) else requirements
                )
                checks.add((backend, check["id"], tuple(requirements)))
        # Within a test family, CHECK identity persists even when a revision corrects time or signal aliases.
        previous = covered.setdefault(key, set())
        if checks - previous:
            selected.add(index)
            previous.update(checks)
    return [test for index, test in enumerate(tests) if index in selected]


# Keep every original CHECK and requirement reference when merging identical observations.
def merge_entries(tests):
    """Merge identical observations within one execution while preserving all CHECK and requirement references."""
    merged, sequence = {}, 0
    for test in tests:
        for backend, run in test["runs"].items():
            records = (
                accepted_mismatch_subset(run, backend)
                if run.get("accepted_mismatch_subset")
                else run.get("mismatches", [])
            )
            for check in records:
                sequence += 1
                fields = {
                    k: check.get(k)
                    for k in ("signal", "time", "cycle", "bits", "expected", "actual")
                }
                key = digest([test["id"], backend, fields])
                if key not in merged:
                    merged[key] = {
                        "id": f"E{len(merged)+1}",
                        "test": test["id"],
                        "backend": backend,
                        "run_status": run["status"],
                        **fields,
                        "requirements": [],
                        "observations": [],
                    }
                entry = merged[key]
                for req in (
                    [check["requirement"]]
                    if isinstance(check["requirement"], str)
                    else check["requirement"]
                ):
                    if req not in entry["requirements"]:
                        entry["requirements"].append(req)
                entry["observations"].append(
                    {"mismatch_id": f"M{sequence}", "check": deepcopy(check)}
                )
    return list(merged.values())


def collect_candidates(toolbox, analyses, excluded=()):
    """Merge original-source candidate ranges and references, excluding confirmed invalid entries."""
    rows = {}
    entries = toolbox.entries
    from .source import CandidateAssignmentRanges

    assignment_ranges = CandidateAssignmentRanges(toolbox.ctx.sources)

    def add(file, lines, entry_ids, material_ids, reason, kind, requirement_ids=()):
        entry_ids = [e for e in entry_ids if e not in excluded]
        if file not in toolbox.ctx.sources or (not entry_ids and not requirement_ids):
            return
        lines = assignment_ranges.normalize(file, lines)
        key = (file, tuple(tuple(span) for span in lines))
        item = rows.setdefault(
            key,
            {
                "id": f"C{len(rows)+1}",
                "file": file,
                "lines": lines,
                "entries": [],
                "requirements": [],
                "records": [],
                "reasons": [],
                "kinds": [],
            },
        )
        for requirement in requirement_ids:
            if requirement not in item["requirements"]:
                item["requirements"].append(requirement)
        for entry in entry_ids:
            if entry in entries and entry not in item["entries"]:
                item["entries"].append(entry)
                for req in entries[entry]["requirements"]:
                    if req not in item["requirements"]:
                        item["requirements"].append(req)
        for value, field in (
            (material_ids, "records"),
            ([reason] if reason else [], "reasons"),
            ([kind], "kinds"),
        ):
            for v in value:
                if v not in item[field]:
                    item[field].append(v)

    for analysis in analyses:
        for c in analysis["candidates"]:
            add(
                c["file"],
                c["lines"],
                c["entries"],
                [m for m in c.get("records", c.get("materials", [])) if m in toolbox.materials],
                c.get("reason", ""),
                analysis.get("origin", "model_hypothesis"),
                c.get("requirements", []),
            )
    merged = []
    for item in rows.values():
        matches = [
            old
            for old in merged
            if old["file"] == item["file"]
            and any(
                a <= d and b >= c for a, b in old["lines"] for c, d in item["lines"]
            )
        ]
        for old in matches:
            merged.remove(old)
            item["lines"] = [
                list(span)
                for span in sorted(
                    {tuple(span) for span in [*item["lines"], *old["lines"]]}
                )
            ]
            for field in ("entries", "requirements", "records", "reasons", "kinds"):
                item[field] = list(dict.fromkeys([*old[field], *item[field]]))
        spans = []
        for a, b in sorted(item["lines"]):
            if spans and a <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        item["lines"] = spans
        merged.append(item)
    for index, item in enumerate(merged, 1):
        item["id"] = f"C{index}"
    return merged


def grouping_tests(toolbox, tests):
    """Provide all testbenches with shared code, compact diffs and individual configurations."""
    rows, base = [], None
    for test in tests:
        row = toolbox.read_test(test["id"])
        # Strip display line numbers before finding exact source insertions.
        row["code"] = "\n".join(
            line.partition(": ")[2] for line in row["code"].splitlines()
        )
        code = row["code"]
        if base is not None:
            delta = "\n".join(
                unified_diff(
                    base["code"].splitlines(),
                    code.splitlines(),
                    fromfile=base["test"],
                    tofile=row["test"],
                    lineterm="",
                )
            )
            if len(delta) < len(code):
                row.pop("code")
                row.update(code_from=base["test"], code_diff=delta)
        if "code" in row:
            base = row
        rows.append(row)
    return rows


def family(box, entry):
    """Identify a shared test environment by group, macros and simulation backend."""
    test = box._test(entry["test"])
    return digest(
        [
            test["tb"].get("group", test["id"].split("_r")[0]),
            test["tb"].get("defines"),
            entry.get("backend"),
        ]
    )


def usable_repair(test):
    """Require executed valid observations before replacing earlier CHECK records."""
    return any(
        run.get("status") == "ok" and run.get("valid_checks")
        for run in test.get("runs", {}).values()
    )


def joined(analyses):
    return {
        key: [row for a in analyses for row in a.get(key, [])]
        for key in ("candidates", "boundaries", "explained", "unresolved", "tb_errors")
    }


def proposal_pool(edits):
    """Interleave executable proposals by source signal to cover distinct relations."""
    proposals = list(edits.get("proposals") or [])
    proposals.extend(
        p
        for p in (edits.get("deferred") or [])
        if isinstance(p, dict)
        and p.get("edits")
        and p.get("anchor")
        and p.get("relation")
    )
    sources = {}
    for index, proposal in enumerate(proposals):
        source = (
            proposal.get("relation", {}).get("source", f"#{index}")
            if isinstance(proposal, dict)
            else f"#{index}"
        )
        sources.setdefault(source, []).append(proposal)
    return [
        bucket[offset]
        for offset in range(max((len(v) for v in sources.values()), default=0))
        for bucket in sources.values()
        if offset < len(bucket)
    ]


def set_requirement_context(ctx, frtg_result):
    """Retain unchecked and partially checked requirements for source analysis."""
    ctx.requirement_execution, checked, uncertain = [], set(), {}
    pending, pending_conditions = set(), {}
    for test in frtg_result.get("tests", []):
        for condition in test.get("tb", {}).get("conditions", []):
            if condition.get("uncertainty"):
                uncertain.setdefault(condition["requirement"], []).append(
                    condition["uncertainty"]
                )
        for run in test.get("runs", {}).values():
            for check in run.get("valid_checks", []):
                if any(
                    check.get(k) is False
                    for k in (
                        "valid",
                        "expected_valid",
                        "expected_known",
                        "waveform_valid",
                    )
                ):
                    continue
                ids = check.get("requirement", [])
                checked.update([ids] if isinstance(ids, str) else ids)
    for group in frtg_result.get("group_results", []):
        pending.update(group.get("pending_requirements", []))
        pending.update(group.get("pending_condition_requirements", []))
        row = {
            k: group[k]
            for k in (
                "id",
                "observed_requirements",
                "pending_requirements",
                "stop_reason",
            )
            if k in group
        }
        for condition in group.get("pending_conditions", []):
            identity = condition["requirement"]
            pending.add(identity)
            pending_conditions.setdefault(identity, []).append(condition)
            if condition.get("uncertainty"):
                uncertain.setdefault(condition["requirement"], []).append(
                    condition["uncertainty"]
                )
        attempts = group.get("attempts", [])
        if attempts and isinstance(attempts[-1], dict):
            row["last_runs"] = {
                backend: {
                    k: run[k] for k in ("status", "checker_errors", "log") if k in run
                }
                for backend, run in attempts[-1].get("runs", {}).items()
            }
        ctx.requirement_execution.append(row)
    ctx.requirement_hints = [
        {
            "requirement": r["id"],
            "loc": r.get("loc", {}),
            "check_status": (
                "uncertain"
                if r["id"] in uncertain
                else (
                    "partially_checked"
                    if r["id"] in checked
                    else "no_valid_executed_check"
                )
            ),
            "pending_conditions": pending_conditions.get(r["id"], []),
            "uncertainty": uncertain.get(r["id"], []),
        }
        for r in frtg_result["requirements"]
        if r["id"] not in checked or r["id"] in uncertain or r["id"] in pending
    ]


def original_page(box, entry, signals=None, limit=80):
    page = deepcopy(box.trace_temporal(entry["id"], signals=signals))
    if page["total"] > limit:
        page.update(box.read_material(page["material_id"], "relations", limit=limit))
    return page


def initial_test_issues(test, requirements, pending=(), review_passes=True):
    assigned = test.get("assigned_requirements") or [r["id"] for r in requirements]
    if not assigned:
        return []
    issues = []
    runs = test.get("runs", {})
    for backend, run in runs.items():
        if run.get("checker_errors"):
            issues.append(
                {
                    "entries": [],
                    "requirements": assigned,
                    "reason": "Recorded CHECK/VCD disagreement: "
                    + str(run["checker_errors"]),
                }
            )
        if run.get("status") != "ok":
            issues.append(
                {
                    "entries": [],
                    "requirements": assigned,
                    "reason": f"The saved {backend} run did not complete: {run.get('status')}. "
                    "Correct its actual compiler/interface/runtime error in the shared TB; "
                    "other requirements can remain pending.\n"
                    + str(run.get("log", "")),
                }
            )
    if not runs:
        issues.append(
            {
                "entries": [],
                "requirements": assigned,
                "reason": "This saved TB has no executable observations. Correct the recorded issue and produce a focused Spec-derived CHECK. "
                + str(test.get("error", "")),
            }
        )
    for item in pending:
        if item.get("blocked"):
            continue
        issues.append(
            {
                "entries": [],
                "requirements": [item["requirement"]],
                "condition": item.get("condition", ""),
                "reason": "Complete this pending Spec requirement in the shared TB: "
                + item.get("condition", "Implement its main reachable CHECK."),
            }
        )
    valid = [c for run in runs.values() for c in run.get("valid_checks", [])]
    if not issues and not valid:
        issues.append(
            {
                "entries": [],
                "requirements": assigned,
                "reason": "The run produced no valid CHECK. Implement a reachable trigger and independent Spec-derived expectation; preserve a finite runnable TB.",
            }
        )
    if (
        review_passes
        and not issues
        and valid
        and not any(c.get("passed") is False for c in valid)
    ):
        issues.append(
            {
                "entries": [],
                "requirements": assigned,
                "reason": "All saved checks pass. Review once whether they actually exercise the assigned triggers and distinguish the required behavior from the suspicious implementation. "
                "Use legal stimulus and an independent Spec-derived reference. Keep the TB unchanged if already adequate; do not force a mismatch or change a correct expectation.",
            }
        )
    return issues


def repaired_test_record(ctx, name, result, test, assigned):
    result = {
        **result,
        "repair_of": test["id"],
        "assigned_requirements": assigned,
        "group_id": test.get("group_id"),
    }
    observed, notes, accepted = observations(result, set(assigned))
    result["pending_requirements"] = [r for r in assigned if r not in observed]
    executed = {
        (c["requirement"], c["id"]) for checks in accepted.values() for c in checks
    }
    result["pending_conditions"] = [
        row
        for row in (result.get("tb") or {}).get("conditions", [])
        if row["requirement"] in assigned
        and (
            not row["checks"]
            or any((row["requirement"], cid) not in executed for cid in row["checks"])
        )
    ]
    result["repair_notes"] = notes
    ctx.save(f"tba/tb_repairs/{name}.json", result)
    return measured_test(result, accepted)


def test_progress(result, assigned, observed, conditions, covered_conditions, revision):
    """Accumulate valid observations and prepare missing scenarios and execution feedback."""
    current_observed, notes, accepted = observations(result, set(assigned))
    # Conditions describe scenarios; CHECK IDs identify executed checks.
    executed = {
        (c["requirement"], c["id"]) for checks in accepted.values() for c in checks
    }
    for row in (result.get("tb") or {}).get("conditions", []):
        if row["requirement"] not in assigned:
            continue
        key = (row["requirement"], row["condition"])
        conditions[key] = row
        if row["checks"] and all((key[0], cid) in executed for cid in row["checks"]):
            covered_conditions.add(key)
    undeclared = [
        key
        for key in assigned
        if not any(r["requirement"] == key for r in conditions.values())
    ]
    observed.update(current_observed)
    # Both passing and failing valid checks count as executed.
    pending = [key for key in assigned if key not in observed]
    feedback = tb_feedback(result, pending, notes)
    if undeclared:
        feedback["log"] += (
            "\nRequirements without a declared test condition: "
            + ", ".join(undeclared)
            + ". Add their Spec-defined cases to conditions and map the implemented CHECK IDs."
        )
    pending_conditions = [
        row for key, row in conditions.items() if key not in covered_conditions
    ]
    blocked = [row for row in pending_conditions if row.get("blocked")]
    pending_feasible_conditions = [
        row for row in pending_conditions if not row.get("blocked")
    ]
    blocked_ids = {
        key
        for key in pending
        if any(r["requirement"] == key for r in blocked)
        and all(
            r.get("blocked") for r in conditions.values() if r["requirement"] == key
        )
    }
    pending_feasible = [key for key in pending if key not in blocked_ids]
    feedback.update(
        pending=pending_feasible,
        pending_conditions=pending_feasible_conditions,
        blocked_conditions=blocked,
    )
    # Recheck stimulus discrimination when the first test version passes every check.
    execution_complete = bool(result.get("runs")) and all(
        run.get("status") == "ok" for run in result["runs"].values()
    )
    review_stimulus = (
        revision == 1
        and execution_complete
        and bool(observed)
        and not any(run.get("mismatches") for run in result.get("runs", {}).values())
    )
    if review_stimulus:
        feedback[
            "log"
        ] += "\nNo mismatch was observed. Review distinguishing Spec-supported scenarios once; do not invent an error or change a correct expectation."
    return {
        "observed": current_observed,
        "accepted": accepted,
        "feedback": feedback,
        "pending_conditions": pending_conditions,
        "undeclared": undeclared,
        "complete": not pending_feasible
        and not pending_feasible_conditions
        and not undeclared
        and not notes
        and execution_complete
        and not review_stimulus,
        "stop_reason": (
            "blocked_conditions_remain"
            if blocked
            else ("declared_checks_observed" if conditions else "all_ids_observed")
        ),
    }


def repair_inputs(ctx, frtg_result, tests):
    latest = {}
    for test in tests:
        key = digest(
            [
                test["tb"].get("group", test["id"].split("_r")[0]),
                test["tb"].get("defines"),
                "verilator",
            ]
        )
        latest[key] = test
    # Include the latest tests from groups with no valid observations yet.
    pending_by_family = {}
    for group in frtg_result.get("group_results", []):
        attempts = group.get("attempts", [])
        test = next((row for row in reversed(attempts) if row.get("tb")), None)
        if test is None:
            test = next(
                (row for row in reversed(tests) if row.get("group_id") == group["id"]),
                None,
            )
        if test is None:
            test = {
                "id": group["id"] + "_pending",
                "group_id": group["id"],
                "tb": {
                    "defines": group.get("group", {}).get(
                        "defines", ctx.build.get("defines", [])
                    )
                },
                "runs": {},
                "assigned_requirements": group.get("group", {}).get("requirements", []),
            }
        key = digest(
            [
                test["tb"].get("group", test["id"].split("_r")[0]),
                test["tb"].get("defines"),
                "verilator",
            ]
        )
        pending = list(group.get("pending_conditions", []))
        represented = {row["requirement"] for row in pending}
        pending.extend(
            {"requirement": rid}
            for rid in dict.fromkeys(
                [
                    *group.get("pending_requirements", []),
                    *group.get("pending_condition_requirements", []),
                ]
            )
            if rid not in represented
        )
        pending_by_family[key] = pending
        if pending or group.get("attempts"):
            latest[key] = test
    return latest, pending_by_family


def confirm_test_issues(analysis, repaired, box):
    """Confirm testbench issues using revised valid checks; retain unresolved entries."""
    if analysis.get("tb_errors"):
        checked = {
            r
            for run in repaired.get("runs", {}).values()
            for c in run.get("valid_checks", [])
            for r in (
                [c["requirement"]]
                if isinstance(c.get("requirement"), str)
                else c.get("requirement", [])
            )
        }
        confirmed = []
        for issue in analysis.pop("tb_errors"):
            affected = {
                r
                for e in issue["entries"]
                for r in box.entries[e].get("requirements", [])
            }
            if usable_repair(repaired) and affected and affected <= checked:
                confirmed.append(issue)
            else:
                # Confirm issues only for affected requirements with valid revised checks.
                analysis["unresolved"].append(
                    {
                        **issue,
                        "reason": "TB diagnosis remains unconfirmed for these requirements in the recorded revision: "
                        + issue["reason"],
                    }
                )
        analysis["tb_errors"] = confirmed


def update_requirement_context(ctx, frtg_result, tests, repairs):
    """Update executed scenarios while preserving other conditions and original locations."""
    groups = deepcopy(frtg_result.get("group_results", []))
    for repaired in repairs:
        for group in groups:
            if repaired.get("group_id") != group["id"]:
                continue
            group["pending_requirements"] = repaired.get("pending_requirements", [])
            executed = {
                (check["requirement"], check["id"])
                for run in repaired.get("runs", {}).values()
                for check in run.get("valid_checks", [])
            }
            conditions = repaired.get("tb", {}).get("conditions", [])
            covered = {
                (row["requirement"], row["condition"])
                for row in conditions
                if row.get("checks")
                and all((row["requirement"], cid) in executed for cid in row["checks"])
            }
            remaining = [
                row
                for row in group.get("pending_conditions", [])
                if (row["requirement"], row["condition"]) not in covered
            ]
            group["pending_conditions"] = list(
                {
                    digest(row): row
                    for row in [*remaining, *repaired.get("pending_conditions", [])]
                }.values()
            )
            declared = {row["requirement"] for row in conditions}
            group["pending_condition_requirements"] = [
                rid
                for rid in repaired.get("assigned_requirements", [])
                if rid not in declared
            ]
    set_requirement_context(
        ctx, {**frtg_result, "tests": tests, "group_results": groups}
    )


def save_tba_result(ctx, box, batches, result):
    """Save candidates and references together with unresolved analysis items."""
    # Keep full materials in memory for pagination; results retain IDs and location summaries.
    saved_result = {
        **result,
        "records": list(box.materials),
        "record_storage": "memory_only",
    }
    for name, value in [
        ("tba/initial_candidates.json", result["initial_candidates"]),
        ("tba/final_candidates.json", result["final_candidates"]),
        ("tba/candidates.json", result["final_candidates"]),
        ("tba/all_entries.json", result["all_entries"]),
        ("tba/result.json", saved_result),
    ]:
        ctx.save(name, value)
    membership = {
        entry: g["id"]
        for p in batches
        for g in original_groups(p)
        for entry in g["members"]
    }
    ctx.save(
        "tba/mismatch-map.json",
        [
            {
                "mismatch_id": o["mismatch_id"],
                "check": o["check"]["id"],
                "entry": e["id"],
                "group": membership.get(e["id"]),
                "test": e["test"],
            }
            for e in box.entries.values()
            for o in e.get("observations", [])
        ],
    )
