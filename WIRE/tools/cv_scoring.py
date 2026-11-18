"""Shared trace observations and counterfactual scoring."""

import copy
import re

from .simulation import number


def normalize_observation_sets(value, tests, entries):
    if not isinstance(value, list):
        raise ValueError("observation_sets must be an array")
    test_ids = {test["id"] for test in tests}
    traces = {entry["id"]: entry for entry in entries}
    seen, expectations, result = set(), {}, []
    for group in value:
        if not isinstance(group, dict) or group.get("trace") not in traces:
            raise ValueError("Each observation set must name a supplied trace entry")
        trace, test = group["trace"], group.get("test")
        if trace in seen or test not in test_ids or traces[trace]["test"] != test:
            raise ValueError("Each trace needs one observation set for its original test")
        seen.add(trace)
        points = group.get("observations")
        if not isinstance(points, list) or not points:
            raise ValueError("Each trace needs nonempty observations")
        unique = {}
        for point in points:
            if not isinstance(point, dict):
                raise ValueError("Observation must be an object")
            for field in ("signal", "expected_bits", "basis"):
                if not isinstance(point.get(field), str) or not point[field].strip():
                    raise ValueError("Observation needs " + field)
            if type(point.get("time")) is not int or point["time"] < 0:
                raise ValueError("Observation time must be a nonnegative VCD tick")
            if point.get("role") not in {"entry", "upstream", "downstream"}:
                raise ValueError("Observation role must be entry, upstream or downstream")
            if set(point["expected_bits"]) - set("01"):
                raise ValueError("Expected values must be full-width binary strings")
            key = (point["signal"], point["time"])
            physical = (test, *key)
            if physical in expectations and expectations[physical] != point["expected_bits"]:
                raise ValueError("Conflicting expectations for the same signal and time")
            expectations[physical] = point["expected_bits"]
            if key in unique:
                if unique[key]["role"] != point["role"]:
                    raise ValueError("Conflicting roles within one trace")
                continue
            unique[key] = {field: point[field] for field in
                           ("signal", "time", "role", "expected_bits", "basis")}
        entry = traces[trace]
        anchor = unique.get((entry["signal"], entry["time"]))
        if anchor is None or anchor["role"] != "entry":
            raise ValueError("Include the original mismatch signal and time as an entry observation")
        expected = number(entry.get("expected"))
        if expected is not None:
            width = len(anchor["expected_bits"])
            if int(anchor["expected_bits"], 2) != expected % (1 << width):
                raise ValueError("Entry expectation must agree with the original testbench check")
        result.append({"trace": trace, "test": test, "observations": list(unique.values())})
    if seen != set(traces):
        raise ValueError("Provide one common observation set for every supplied trace")
    return result


def flatten_observations(observation_sets):
    return [{**point, "trace": group["trace"], "test": group["test"]}
            for group in observation_sets for point in group["observations"]]


def _identity(point):
    return point["trace"], point["test"], point["signal"], point["time"]


def _time_unit(value):
    match = re.fullmatch(r"\$timescale\s+(1|10|100)\s*(s|ms|us|ns|ps|fs)\s+\$end", value or "")
    if match is None:
        return None
    return int(match[1]) * {"s": 10**15, "ms": 10**12, "us": 10**9,
                           "ns": 10**6, "ps": 10**3, "fs": 1}[match[2]]


def compare_observations(observation_sets, before, after):
    """Count each signal/time once per trace; unavailable points never count as passes."""
    def index(rows):
        indexed = {}
        for row in rows:
            key = _identity(row)
            if key in indexed:
                raise ValueError("Duplicate sampled observation")
            indexed[key] = row
        return indexed

    original, variant = index(before), index(after)
    transitions = {key: [] for key in ("FP", "FF", "PF", "PP")}
    unavailable, seen = [], set()
    upstream_fp = 0
    for point in flatten_observations(observation_sets):
        key = _identity(point)
        if key in seen:
            continue
        seen.add(key)
        old, new = original.get(key), variant.get(key)
        reason = None
        for label, row in (("original", old), ("variant", new)):
            if row is None:
                reason = label + " observation missing"
                break
            bits = row.get("actual_bits")
            if (row.get("status") != "observed" or not isinstance(bits, str)
                    or set(bits) - set("01") or len(bits) != len(point["expected_bits"])):
                reason = label + " observation unavailable: " + row.get("status", "unknown")
                break
            if row.get("expected_bits") != point["expected_bits"]:
                reason = label + " expectation differs from the common observation set"
                break
        if reason is None:
            unit = _time_unit(old.get("timescale"))
            if unit is None or unit != _time_unit(new.get("timescale")):
                reason = "Missing or inconsistent VCD time units"
        if reason:
            unavailable.append({**point, "reason": reason})
            continue
        label = ("P" if old["actual_bits"] == point["expected_bits"] else "F")
        label += "P" if new["actual_bits"] == point["expected_bits"] else "F"
        transitions[label].append({**point, "before": old["actual_bits"], "after": new["actual_bits"]})
        if label == "FP" and point["role"] == "upstream":
            upstream_fp += 1
    counts = {key: len(rows) for key, rows in transitions.items()}
    comparable = sum(counts.values())
    return {"transitions": transitions, "counts": counts, "upstream_FP": upstream_fp,
            "score": counts["FP"] + upstream_fp - counts["PF"] if comparable else None,
            "comparable": comparable, "total": len(seen), "unavailable": unavailable,
            "complete": bool(seen) and not unavailable}


def heuristic_ranking(ids, histories, observation_sets=()):
    by_id = {history["candidate"]: history for history in histories}
    rows = []
    for cid in ids:
        attempts = by_id.get(cid, {}).get("attempts", [])
        comparison = attempts[-1].get("observation_comparison") if attempts else None
        if comparison is None:
            comparison = compare_observations(observation_sets, [], [])
        rows.append({"candidate_id": cid, **{key: copy.deepcopy(comparison[key]) for key in
                     ("score", "counts", "upstream_FP", "comparable", "total", "complete")}})
    # Stable ties preserve the original candidate order.
    rows.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0)))
    return [{**row, "rank": rank} for rank, row in enumerate(rows, 1)]
