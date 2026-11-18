import copy
import difflib
import json
import re
from pathlib import Path

from ..contracts import digest, edit_offset, provided_constants
from .ast import validate_linked_edits, TBATools, _schema, connect_tb_aliases
from .cv_context import numbered, normalized_observations, columnar_result
from .simulation import simulate, compare, _last_vcd_tick, recovered_test_inputs
from .waveform import read_wave, list_signals
from .source import provided_interface, SourceLocator
from .cv_scoring import normalize_observation_sets, flatten_observations, compare_observations



def context(prepared):

    original, m2 = prepared["original"], prepared["m2"]
    visible = set(original["build"].get("model_files")
                  or original["build"].get("targets") or original["sources"])
    visible.update(row["file"] for row in prepared["catalog"])
    value = {
        "spec": original["spec"],
        "rtl": {name: numbered(code) for name, code in original["sources"].items()
                if name in visible},
        "build": original["build"],
        "requirements": m2.get("requirements", prepared["m1"].get("requirements", [])),
        "entries": normalized_observations(m2),
        "tests": [{k: v for k, v in test.items() if k != "runs"}
                  for test in m2.get("tests", [])],
        "candidates": prepared["catalog"],
        "m2_context": {k: m2[k] for k in (
            "analyses", "trace_context", "local_ast", "saved_M2_contexts",
            "augmentations", "boundaries", "unresolved", "requirement_hints",
        ) if k in m2},
        "saved_runs": observations(m2.get("tests", [])),
    }
    interface_build = {**original["build"], "model_files": sorted(visible)}
    if constants := provided_constants(original["sources"], interface_build):
        value["interface_constants"] = constants
    if declarations := provided_interface(original["sources"], interface_build, include_function_bodies=True):
        value["interface_declarations"] = declarations
    return value


def review_context(prepared, ranking_input, diagnosis, previous_ranking=None):

    value = copy.deepcopy(ranking_input)
    sources = prepared["original"]["sources"]
    numbered_sources = {name: numbered(code).splitlines() for name, code in sources.items()}
    for candidate in value.get("candidates", []):
        lines = numbered_sources[candidate["file"]]
        candidate["original_text"] = "\n\n".join(
            "\n".join(lines[lo - 1:hi]) for lo, hi in candidate["lines"]
        )
    original = prepared["original"]
    visible = set(original["build"].get("model_files")
                  or original["build"].get("targets") or sources)
    visible.update(row["file"] for row in value.get("candidates", []))
    interface_build = {**original["build"], "model_files": sorted(visible)}
    declarations = provided_interface(sources, interface_build, include_function_bodies=True)
    for file, packages in declarations.items():
        value.setdefault("interface_declarations", {}).setdefault(file, {}).update(packages)
    if constants := provided_constants(sources, interface_build):
        value.setdefault("interface_constants", {}).update(constants)
    source_groups = {}
    for history in value.get("counterfactual", []):
        locations = []
        for attempt in history.get("attempts", []):
            edits = attempt.get("intervention", {}).get("edits", [])
            if not edits:
                continue
            try:
                patched = apply_edits(sources, edits)
            except ValueError:
                continue
            source_id = digest(patched)
            source_groups.setdefault(source_id, set()).add(history["candidate"])
            changes = changed_locations(sources, patched, value.get("candidates", []))
            attempt["changed_locations"] = changes
            for change in changes:
                if change not in locations:
                    locations.append(change)
        for candidate in value.get("candidates", []):
            if candidate["id"] == history["candidate"]:
                candidate["changed_locations"] = locations
    value["identical_variant_groups"] = [
        {"source_id": source_id, "candidate_ids": sorted(ids)}
        for source_id, ids in source_groups.items() if len(ids) > 1
    ]
    value["findings"] = copy.deepcopy(diagnosis["findings"])
    value["diagnosis_limitations"] = copy.deepcopy(diagnosis["limitations"])
    if previous_ranking is not None:
        value["previous_ranking"] = copy.deepcopy(previous_ranking)
    order = (
        "spec", "rtl", "build", "interface_constants", "interface_declarations",
        "findings", "diagnosis_limitations", "candidates", "previous_ranking", "requirements", "entries",
        "m2_context", "tests", "saved_runs", "baseline", "counterfactual", "identical_variant_groups", "shared",
    )
    return {**{key: value[key] for key in order if key in value},
            **{key: item for key, item in value.items() if key not in order}}


def changed_locations(original, patched, candidates):

    result = []
    for file, source in original.items():
        if source == patched[file]:
            continue
        before, after = source.splitlines(keepends=True), patched[file].splitlines(keepends=True)
        for kind, lo, hi, new_lo, new_hi in difflib.SequenceMatcher(
                a=before, b=after, autojunk=False).get_opcodes():
            if kind == "equal":
                continue
            span = [lo + 1, hi] if hi > lo else [max(1, lo), max(1, lo)]
            row = {
                "file": file, "lines": span,
                "old": "".join(before[lo:hi]), "new": "".join(after[new_lo:new_hi]),
                "candidate_ids": [candidate["id"] for candidate in candidates
                                  if candidate["file"] == file and any(
                                      start <= span[1] and span[0] <= end
                                      for start, end in candidate["lines"])],
            }
            if kind == "insert":
                row["insertion_after_line"] = lo
            result.append(row)
    return result


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")


def validate_intervention(value, cid, tests):
    if not isinstance(value, dict) or value.get("candidate") != cid:
        raise ValueError("Intervention must retain its candidate ID")
    _text(value.get("reason"), "reason")
    edits = value.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ValueError("Provide at least one local edit")
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("Edit must be an object")
        for key in ("file", "old", "new"):
            if not isinstance(edit.get(key), str):
                raise ValueError("Edit " + key + " must be a string")
        if type(edit.get("line")) is not int or edit["line"] < 1:
            raise ValueError("Edit line must be a positive original line number")
    probes = value.get("probes")
    if not isinstance(probes, list):
        raise ValueError("probes must be an array")
    test_ids = {test["id"] for test in tests}
    for probe in probes:
        if not isinstance(probe, dict) or probe.get("test") not in test_ids:
            raise ValueError("Probe must reference an existing test")
        if probe.get("role") not in {"upstream", "entry", "downstream"}:
            raise ValueError("Probe role must be upstream, entry or downstream")
        if type(probe.get("time")) is not int or probe["time"] < 0:
            raise ValueError("Probe time must be a nonnegative VCD tick")
        for key in ("signal", "basis", "expected_bits"):
            _text(probe.get(key), key)
        if set(probe["expected_bits"]) - set("01"):
            raise ValueError("expected_bits must be a known binary bit string")
    return copy.deepcopy(value)


def validate_plans(value, ids, tests, entries=()):
    if not isinstance(value, dict):
        raise ValueError("Return an object")
    interventions, unresolved = value.get("interventions"), value.get("unresolved")
    if not isinstance(interventions, list) or not isinstance(unresolved, list):
        raise ValueError("Provide interventions and unresolved arrays")
    observation_sets = normalize_observation_sets(value.get("observation_sets"), tests, entries)
    limitations = value.get("observation_limitations", [])
    if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
        raise ValueError("observation_limitations must be an array of strings")
    common_probes = flatten_observations(observation_sets)
    value = copy.deepcopy(value)
    interventions, unresolved = value["interventions"], value["unresolved"]
    value["observation_sets"] = observation_sets
    value["observation_limitations"] = list(limitations)
    seen = []
    for row in interventions:
        cid = row.get("candidate") if isinstance(row, dict) else None
        if cid not in ids:
            raise ValueError("Unknown candidate")
        if "probes" in row and row["probes"] != common_probes:
            raise ValueError("Candidate-specific probes are not allowed; use observation_sets")
        row["probes"] = copy.deepcopy(common_probes)
        validate_intervention(row, cid, tests)
        seen.append(cid)
    for row in unresolved:
        if not isinstance(row, dict) or row.get("candidate") not in ids:
            raise ValueError("Unknown unresolved candidate")
        _text(row.get("need"), "need")
        seen.append(row["candidate"])
    if len(seen) != len(set(seen)) or set(seen) != set(ids):
        raise ValueError("Cover each candidate once in interventions or unresolved")
    return copy.deepcopy(value)


def validate_revision(value, cid, tests):
    if not isinstance(value, dict) or value.get("status") not in {"accepted", "revise", "unresolved"}:
        raise ValueError("status must be accepted, revise or unresolved")
    _text(value.get("reason"), "reason")
    if value["status"] == "revise":
        validate_intervention(value.get("intervention"), cid, tests)
    elif value.get("intervention") is not None:
        raise ValueError("Only revise supplies an intervention; otherwise use null")
    return copy.deepcopy(value)


def validate_ranking(value, ids, requirements):
    if not isinstance(value, dict) or not isinstance(value.get("ranked_candidates"), list):
        raise ValueError("Provide ranked_candidates")
    rows = value["ranked_candidates"]
    seen = []
    req_ids = {row["id"] for row in requirements}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each ranked candidate must be an object")
        seen.append(row.get("candidate_id"))
        _text(row.get("reason"), "reason")
        if row.get("role") not in {"root_cause", "propagation", "compensation", "unresolved", "no_conflict"}:
            raise ValueError("Invalid candidate role")
        if not isinstance(row.get("requirements"), list) or any(r not in req_ids for r in row["requirements"]):
            raise ValueError("Use existing requirement IDs")
    if any(not isinstance(cid, str) for cid in seen) or len(seen) != len(set(seen)) or set(seen) != set(ids):
        raise ValueError("Rank every original candidate ID exactly once")
    if not isinstance(value.get("limitations"), list) or any(not isinstance(v, str) for v in value["limitations"]):
        raise ValueError("limitations must be an array of strings")

    ordered = [row for row in rows if row["role"] != "no_conflict"]
    ordered.extend(row for row in rows if row["role"] == "no_conflict")

    return {"ranked_candidates": [{k: row[k] for k in ("candidate_id", "reason", "role", "requirements")}
                                  for row in ordered], "limitations": value["limitations"]}


def apply_edits(sources, edits):

    edits = validate_linked_edits(sources, edits)
    result = dict(sources)
    by_file = {}
    for edit in edits:
        offset = edit_offset(sources[edit["file"]], edit["old"], edit["line"])
        by_file.setdefault(edit["file"], []).append((offset, edit))
    for name, rows in by_file.items():
        for offset, edit in sorted(rows, key=lambda row: row[0], reverse=True):
            result[name] = result[name][:offset] + edit["new"] + result[name][offset + len(edit["old"]):]
    return result


def _literal_width(declaration):

    text = re.sub(r"\b(?:input|logic|reg|wire|signed|unsigned)\b", " ", declaration)
    ranges = re.findall(r"\[([^]]*)\]", text)
    rest = re.sub(r"\[[^]]*\]", " ", text).strip()
    if rest:
        return None
    width = 1
    for span in ranges:
        match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", span)
        if not match:
            return None
        width *= abs(int(match[1]) - int(match[2])) + 1
    return width


def adapt_tb_inputs(original, sources, tb):

    before, after = SourceLocator(original, tb), SourceLocator(sources, tb)
    report = {"original_tb_id": digest(tb), "original_source_id": digest(original),
              "source_id": digest(sources), "connections": [], "unresolved": []}
    top = tb.get("top")
    record = after._module(top)
    if record is None:
        report["unresolved"].append({"reason": "TB top module is not unique"})
        report["effective_tb_id"] = digest(tb)
        return copy.deepcopy(tb), report
    file, start, end = record
    scope = after.clean[file][start:end]
    for instance, rows in after.instances(top).items():
        if len(rows) != 1:
            report["unresolved"].append({"instance": instance, "reason": "TB instance is not unique"})
            continue
        binding = rows[0]
        module = binding["module"]
        added = set(after.port_names(module)) - set(before.port_names(module))
        if added and before._module(module) is None:
            report["unresolved"].append({"instance": instance, "module": module,
                                          "reason": "Original module declaration is absent or ambiguous"})
            continue
        for port in sorted(added):
            if not after._direction(module, port, "input") or port in binding["ports"]:
                continue
            info = {"instance": instance, "module": module, "port": port, "signal": port}
            module_record = after._module(module)
            name, lo, hi = module_record
            module_text = after.clean[name][lo:hi]
            header = module_text[:module_text.find(";")]
            declarations = []
            for match in re.finditer(r"\binput\b(.*?)(?=\b(?:input|output|inout)\b|$)", header, re.S):
                first = match[1].split(",", 1)[0].strip()
                tokens = list(re.finditer(r"[A-Za-z_$][\w$]*", first))
                if not tokens:
                    continue
                prefix = first[:tokens[-1].start()]
                for item in match[1].split(","):
                    if re.search(r"\b" + re.escape(port) + r"\b", item):
                        declarations.append(prefix)
            port_width = _literal_width(declarations[0]) if len(declarations) == 1 else None
            signal_widths = []
            for match in re.finditer(r"\b(?:logic|reg|wire)\s+((?:(?:signed|unsigned)\s+)?(?:\[[^]]*\]\s*)?)([^;]+);", scope):
                for item in match[2].split(","):
                    if re.match(r"\s*" + re.escape(port) + r"\s*(?:=|$)", item):
                        signal_widths.append(_literal_width(match[1]))
            reason = None
            if len(signal_widths) != 1:
                reason = "Same-name TB signal declaration is absent or ambiguous"
            elif port_width is None or signal_widths[0] is None:
                reason = "Input or TB width requires an unresolved type or expression"
            elif re.search(r"\b" + re.escape(instance) + r"\s*\(\s*(?!\.)[^\s)]", scope):
                reason = "Positional instance connections cannot be adapted by a named-port insertion"
            elif port_width != signal_widths[0]:
                reason = "Input and original TB signal widths differ"
            elif not any(row["lhs"] == port for row in before.assignments(top)):
                reason = "Original TB has no assignment driving the same-name signal"
            elif any(re.search(r"\b" + re.escape(port) + r"\b", match[1])
                     for match in re.finditer(r"\b(?:task|function)\b([^;]*);", scope, re.S)):
                reason = "Same-name task or function argument makes the drive scope ambiguous"
            elif any(match[1] not in {
                    "logic", "reg", "wire", "begin", "end", "assign", "return", "if",
                    "else", "while", "for", "initial", "always", "always_comb", "always_ff",
                    "endtask", "endfunction", "input", "output", "inout"}
                    for match in re.finditer(
                        r"\b([A-Za-z_$][\w$]*(?:::[A-Za-z_$][\w$]*)*)\s+"
                        r"(?:(?:signed|unsigned)\s+)?(?:\[[^]]*\]\s*)?"
                        + re.escape(port) + r"\s*(?:[;,=)]|$)", scope)):
                reason = "Same-name declaration with another type makes the drive scope ambiguous"
            if reason:
                report["unresolved"].append({**info, "reason": reason})
            else:
                report["connections"].append({**info, "width": port_width})
    effective = recovered_test_inputs(tb, sources, report["connections"])
    report["effective_tb_id"] = digest(effective)
    return effective, report


def replay(ctx, name, sources, tests):

    def replay_test(item):
        index, test = item
        tb = copy.deepcopy(test.get("tb", {}))
        tb["id"] = test["id"]
        build = copy.deepcopy(test.get("build") or ctx.build)
        tb, tb_adaptation = adapt_tb_inputs(ctx.sources, sources, tb)
        binding = digest({"sources": sources, "tb": tb, "build": build,
                          "tb_adaptation": tb_adaptation})
        relative = f"cv/replays/{name}/test_{index}.json"

        saved_run = test.get("runs", {}).get("verilator", {})
        if (name == "baseline" and test.get("source_id") == digest(sources)
                and test.get("tb") == tb and test.get("build") == build
                and saved_run.get("status") in {"ok", "invalid_checker"}
                and Path(saved_run.get("vcd") or "__missing__").is_file()):
            result = copy.deepcopy(test)
            result["tb_adaptation"] = tb_adaptation
            ctx.save(relative, {"input_id": binding, "output": result,
                                "reused_from": "m2.tests"})
            return result
        try:
            directory = ctx.root / "work/cv" / name / f"test_{index}"
            result = simulate(sources, build, tb, directory)
            result["id"] = test["id"]
        except (ValueError, OSError) as error:
            result = {"id": test["id"], "tb": tb, "build": build,
                      "source_id": digest(sources), "runs": {"verilator": {
                          "status": "execution_error", "log": str(error), "checks": []}}}
        result["tb_adaptation"] = tb_adaptation
        ctx.save(relative, {"input_id": binding, "output": result})
        return result

    return [replay_test(item) for item in enumerate(tests, 1)]


def observations(tests, declarations=False):

    rows = []
    for test in tests:
        run = copy.deepcopy({k: v for k, v in test.get("runs", {}).get("verilator", {}).items()
                             if k not in {"valid_checks", "mismatches"}})
        vcd = run.get("vcd")
        wave = Path(vcd) if vcd else None
        if wave and wave.is_file():
            if declarations:
                declared = list_signals(wave)
                run["wave_signals"] = {
                    "timescale": declared["timescale"],
                    "signals": {row["name"]: row["width"] for row in declared["signals"]},
                }
            run["last_tick"] = _last_vcd_tick(wave)
        rows.append({"test": test["id"], "source_id": test.get("source_id"), "run": run,
                     "tb_adaptation": copy.deepcopy(test.get("tb_adaptation", {}))})
    return rows


def sample_probes(tests, probes):
    runs = {test["id"]: test.get("runs", {}).get("verilator", {}) for test in tests}
    result = []

    for tid in dict.fromkeys(p["test"] for p in probes):
        selected = [p for p in probes if p["test"] == tid]
        vcd = runs.get(tid, {}).get("vcd")
        path = Path(vcd) if vcd else None
        if runs.get(tid, {}).get("status") != "ok" or not path or not path.is_file():
            result.extend({**p, "status": "unavailable", "actual_bits": None, "matches": None} for p in selected)
            continue
        signals = list(dict.fromkeys(p["signal"] for p in selected))
        times = {s: [p["time"] for p in selected if p["signal"] == s] for s in signals}
        try:
            last = _last_vcd_tick(path)
            wave = read_wave(path, signals, times)
        except (OSError, ValueError, KeyError, IndexError, StopIteration) as error:
            result.extend({**p, "status": "unavailable", "actual_bits": None,
                           "matches": None, "error": str(error)} for p in selected)
            continue
        for p in selected:
            width = wave["widths"].get(p["signal"])
            samples = wave["samples"].get(p["signal"], [])
            point = next((s for s in samples if s["time"] == p["time"]), {})
            bits = point.get("value")
            status = "observed"
            if p["time"] > last:
                status = "outside_window"
            elif width is None or bits is None:
                status = "unavailable"
            elif len(p["expected_bits"]) != width:
                status = "width_mismatch"
            elif set(bits) - set("01"):
                status = "unknown_value"
            actual = bits.zfill(width) if width and bits is not None else bits
            result.append({**p, "status": status, "width": width,
                           "actual_bits": actual if p["time"] <= last else None,
                           "matches": actual == p["expected_bits"] if status == "observed" else None,
                           "history": point.get("history", []), "timescale": wave["timescale"]})
    return result


def _multiline_string_lines(text):

    lines = set()
    for match in re.finditer(r'"(?:\\[\s\S]|[^"\\])*(?:"|$)|//[^\n]*|/\*[\s\S]*?\*/', text):
        if match[0].startswith('"') and "\n" in match[0]:
            first = text.count("\n", 0, match.start()) + 1
            lines.update(range(first, first + match[0].count("\n") + 1))
    return lines


def correct_edit_lines(sources, intervention):

    corrected = copy.deepcopy(intervention)
    corrections = []
    for index, edit in enumerate(corrected["edits"]):
        if (not isinstance(edit, dict) or not isinstance(edit.get("file"), str)
                or edit["file"] not in sources or not isinstance(edit.get("old"), str)
                or type(edit.get("line")) is not int):
            continue
        source, old = sources[edit["file"]], edit["old"]
        try:
            edit_offset(source, old, edit["line"])
            continue
        except ValueError:
            pass
        first = source.find(old) if old else -1
        effective_old = old
        reason = "old matches exactly once in the original file"
        if first >= 0:
            if source.find(old, first + 1) >= 0:
                continue
            line = source.count("\n", 0, first) + 1
            if _multiline_string_lines(source).intersection(range(line, line + old.count("\n") + 1)):
                continue
        else:
            if not old or _multiline_string_lines(old):
                continue
            old_lines, source_lines = old.split("\n"), source.split("\n")
            signature = [row.lstrip(" \t") for row in old_lines]
            protected_lines = _multiline_string_lines(source)
            matches = []
            for offset in range(len(source_lines) - len(old_lines) + 1):
                rows = source_lines[offset:offset + len(old_lines)]
                if [row.lstrip(" \t") for row in rows] == signature:
                    candidate = "\n".join(rows)
                    if not protected_lines.intersection(range(offset + 1, offset + len(rows) + 1)):
                        matches.append((offset + 1, candidate))
            if len(matches) != 1:
                continue
            line, effective_old = matches[0]
            reason = "Unique original operation differs only in line-leading spaces or tabs"
        corrections.append({"edit_index": index, "file": edit["file"],
                            "original_line": edit["line"], "corrected_line": line,
                            "old_changed": effective_old != old, "reason": reason})
        edit.update(line=line, old=effective_old)
    return corrected, corrections


def trial(ctx, name, intervention, tests, baseline, register=None,
          observation_sets=None, baseline_samples=None):
    original_intervention = copy.deepcopy(intervention)
    record = {"intervention": copy.deepcopy(intervention)}
    try:
        intervention, corrections = correct_edit_lines(ctx.sources, intervention)
        record["intervention"] = intervention
        if corrections:
            record.update(original_intervention=original_intervention, edit_corrections=corrections)
        sources = apply_edits(ctx.sources, intervention["edits"])
    except ValueError as error:
        record.update(status="edit_error", error=str(error), tests=[], comparison=[])
        if observation_sets is not None:
            record["observation_comparison"] = compare_observations(
                observation_sets, baseline_samples or [], [])
        return record
    variants = replay(ctx, name, sources, tests)
    if register is not None:
        register(sources, variants)
    original = {test["id"]: test for test in baseline}
    probes = (flatten_observations(observation_sets) if observation_sets is not None
              else intervention.get("probes", []))
    before = sample_probes(baseline, probes) if baseline_samples is None else baseline_samples
    after = sample_probes(variants, probes)
    record.update(
        status="executed" if variants else "no_tests",
        source_id=digest(sources), tests=observations(variants),
        comparison=[{"test": test["id"], **compare(original[test["id"]], test)} for test in variants],
        probes={"before": before, "after": after},
    )
    if observation_sets is not None:
        record["observation_comparison"] = compare_observations(observation_sets, before, after)
    return record


def validate_review(value):
    if not isinstance(value, dict) or value.get("status") not in {"accepted", "unresolved"}:
        raise ValueError("Return accepted or unresolved after completing tool calls")
    _text(value.get("reason"), "reason")
    return {"status": value["status"], "reason": value["reason"]}


def save_material(ctx, kind, payload):
    value = dict(payload)
    if "kind" in value:
        value["original_kind"] = value["kind"]
    value["kind"] = kind
    material_id = "M_" + digest(value)[:20]
    path = ctx.root / "cv" / "materials" / (material_id + ".json")
    if not path.exists():
        ctx.save(path, value)
    return material_id


def diagnostic_log(run):

    checks = {}
    for check in run.get("checks", []):
        checks.setdefault(check.get("id"), []).append(check)
    lines = []
    for line in run.get("log", "").splitlines():
        if line.startswith("CHECK "):
            try:
                packet = json.loads(line[6:])
            except ValueError:
                packet = None
            if isinstance(packet, dict) and all(
                value is None or isinstance(value, (str, int, float, bool))
                for value in packet.values()
            ) and any(
                all(key in check and check[key] == value for key, value in packet.items())
                for check in checks.get(packet.get("id"), [])
            ):
                continue
        lines.append(line)
    return "\n".join(lines)


def test_view(ctx, test, inline=False):
    run = test["run"]
    if inline:
        compact = columnar_result(
            {"source_id": test.get("source_id"), "runs": {"verilator": run}},
            include_scalar_fields=True,
        )
        compact.pop("log_tail", None)
        return {"test": test["test"], **compact, "tb_adaptation": test.get("tb_adaptation", {}),
                "log": diagnostic_log(run),
                "timescale": run.get("wave_signals", {}).get("timescale"),
                "last_tick": run.get("last_tick")}
    payload = {**run, "test": test["test"], "source_id": test.get("source_id")}
    payload["log_lines"] = run.get("log", "").splitlines()
    payload["check_values"] = [
        {key: value for key, value in row.items()
         if value is None or isinstance(value, (str, int, float, bool))}
        for row in run.get("checks", [])
    ]
    declared = run.get("wave_signals", {})
    if isinstance(declared.get("signals"), dict):
        payload["signals"] = declared["signals"]
    material_id = save_material(ctx, "simulation", payload)
    compact = columnar_result({"source_id": test.get("source_id"), "runs": {"verilator": run}}, include_scalar_fields=True)
    compact.pop("log_tail", None)
    return {"test": test["test"], **compact, "tb_adaptation": test.get("tb_adaptation", {}),
            "timescale": declared.get("timescale"), "last_tick": run.get("last_tick"),
            "material_id": material_id,
            "fields": [key for key, value in payload.items() if isinstance(value, (list, dict))]}


def public_view(ctx, public, inline=False):
    return {**public,
            "baseline": [test_view(ctx, test, inline) for test in public.get("baseline", [])],
            "saved_runs": [test_view(ctx, test, inline) for test in public.get("saved_runs", [])]}


def share_run_records(data):

    result = copy.deepcopy(data)
    seen, shared = {}, {}

    def intern(container, key):
        value = container[key]
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode()) < 128:
            return
        identity = digest(value)
        if identity not in seen:
            seen[identity] = (container, key, value)
            return
        original, original_key, first_value = seen[identity]
        name = "S_" + identity[:20]
        shared.setdefault(name, first_value)
        reference = {"$ref": "#/shared/" + name}
        original[original_key] = reference
        container[key] = reference

    tests = list(result.get("baseline", [])) + list(result.get("saved_runs", []))
    for history in result.get("counterfactual", []):
        for attempt in history.get("attempts", []):
            tests.extend(attempt.get("tests", []))
    for test in tests:
        rows = test.get("checks_table", {}).get("rows", [])
        for index in range(len(rows)):
            intern(rows, index)
        errors = test.get("checker_errors", [])
        for index in range(len(errors)):
            intern(errors, index)
        if "log" in test:
            intern(test, "log")
    if shared:
        result["shared"] = shared
    return result


def history_view(ctx, history, show_checks=True, inline=False):

    view = copy.deepcopy({key: value for key, value in history.items()
                          if key not in {"attempts", "queries", "review_id"}})
    view["attempts"] = []
    for record in history.get("attempts", []):
        attempt = copy.deepcopy({key: value for key, value in record.items() if key != "tests"})
        attempt["tests"] = []
        for test in record.get("tests", []):
            presented = test_view(ctx, test, inline)
            if not show_checks:
                presented.pop("checks_table", None)
                presented.pop("checker_errors", None)
            attempt["tests"].append(presented)
        view["attempts"].append(attempt)
    view["queries"] = []
    for query in history.get("queries", []):
        row = copy.deepcopy({key: value for key, value in query.items() if key != "result"})
        if "result" in query:
            result = query["result"]
            payload = dict(result) if isinstance(result, dict) else {"items": result if isinstance(result, list) else [result]}
            payload["query"] = {"candidate": history["candidate"], "tool": query["tool"], "arguments": query["arguments"]}
            if isinstance(payload.get("log"), str):
                payload["log_lines"] = payload["log"].splitlines()
            row["material_id"] = save_material(ctx, "query", payload)
            row["fields"] = [key for key, value in payload.items() if isinstance(value, (list, dict))]
            row["summary"] = {key: payload[key] for key in (
                "status", "run_status", "variant", "test", "error", "message", "total", "next_offset", "uncertain"
            ) if key in payload}
        view["queries"].append(row)
    return view


def material_bindings(ctx):

    reader = TBATools(ctx, [], [])

    def read(material_id, field, offset=0, limit=80):
        if not isinstance(material_id, str) or not re.fullmatch(r"M_[0-9a-f]{20}", material_id):
            raise ValueError("Unknown material_id")
        if material_id not in reader.materials:
            paths = [ctx.root / "cv" / "materials" / (material_id + ".json")]
            paths.extend((ctx.root / "cv").glob("candidate_*/tools/materials/" + material_id + ".json"))
            path = next((path for path in paths if path.is_file()), None)
            if path is None:
                raise ValueError("Unknown material_id")
            reader.materials[material_id] = json.loads(path.read_text())
        from .cv_page_budget import bounded_material_page
        return bounded_material_page(
            lambda count: reader.read_material(material_id, field, offset, count), limit,
        )

    return {"read_material": {
        "schema": _schema("read_material", "Read a saved simulation or query material using a field listed in its input index.",
                           {"material_id": {"type": "string"}, "field": {"type": "string"},
                            "offset": {"type": "integer"}, "limit": {"type": "integer"}},
                           ["material_id", "field"]),
        "call": read,
    }}


def counterfactual_state_id(ctx, candidate, proposal, tests, entries, max_revisions):
    return digest({
        "spec": ctx.spec, "sources": ctx.sources, "build": ctx.build,
        "candidate": candidate, "proposal": proposal, "entries": entries,
        "tests": [{key: test.get(key) for key in ("id", "tb", "build")} for test in tests],
        "max_revisions": max_revisions,
        "model": {key: getattr(ctx.model, key, None) for key in ("base_url", "model", "options")},
    })


class CounterfactualTools(TBATools):

    def __init__(self, ctx, index, candidate, proposal, tests, baseline, entries, publish, max_revisions):
        tool_ctx = copy.copy(ctx)
        tool_ctx.root = ctx.root / "cv" / f"candidate_{index}" / "tools"
        entries = copy.deepcopy(entries)
        for entry in entries:
            entry.setdefault("backend", "verilator")
        super().__init__(tool_ctx, baseline, entries)
        self.execution_ctx, self.index, self.candidate = ctx, index, candidate
        self.tests, self.baseline, self.publish = tests, baseline, publish
        self.max_revisions = max_revisions
        self.state_id = counterfactual_state_id(ctx, candidate, proposal, tests, entries, max_revisions)
        self.state_path = ctx.root / "cv" / f"candidate_{index}" / "state.json"
        self.history = {"candidate": candidate, "attempts": [], "queries": [], "status": "running"}
        if self.state_path.is_file():
            saved = json.loads(self.state_path.read_text())
            if saved.get("input_id") == self.state_id:
                self.history = saved["history"]
        self._query_keys = {digest(row) for row in self.history["queries"]}
        if self.history["attempts"]:
            for record in self.history["attempts"]:
                if record.get("variant") is None:
                    continue
                sources = apply_edits(ctx.sources, record["intervention"]["edits"])
                executed = replay(ctx, f"candidate_{index}/{record['variant']}", sources, tests)
                self.variants[record["variant"]] = {
                    "sources": sources, "tests": {test["id"]: test for test in executed},
                }
        else:
            self.simulate_variant(proposal)

    def save_history(self):
        self.execution_ctx.save(self.state_path, {"input_id": self.state_id, "history": self.history})
        self.publish(self.history)

    def _record(self, kind, payload):
        material_id = super()._record(kind, payload)
        path = self.ctx.root / "materials" / (material_id + ".json")
        if not path.exists():
            self.ctx.save(path, self.materials[material_id])
        return material_id

    def read_material(self, material_id, field="relations", offset=0, limit=80):
        if not isinstance(material_id, str) or not re.fullmatch(r"M_[0-9a-f]{20}", material_id):
            raise ValueError("Unknown material_id")
        if material_id not in self.materials:
            for path in (self.ctx.root / "materials" / (material_id + ".json"),
                         self.execution_ctx.root / "cv" / "materials" / (material_id + ".json")):
                if path.is_file():
                    self.materials[material_id] = json.loads(path.read_text())
                    break
        from .cv_page_budget import bounded_material_page
        return bounded_material_page(
            lambda count: super(CounterfactualTools, self).read_material(material_id, field, offset, count), limit,
        )

    def record_query(self, row):
        key = digest(row)
        if key not in self._query_keys:
            self._query_keys.add(key)
            self.history["queries"].append(row)
            self.save_history()

    def graph(self, test, variant="original"):
        previous = self.ctx.build
        executed = self._test(test, variant)
        self.ctx.build = executed.get("build") or previous
        try:
            inputs = [self._variant(variant)["sources"], self.ctx.build, executed["tb"]]
            key = digest(inputs)
            if key in self._graph_cache:
                return self._graph_cache[key]
            name = "tba_ast_" + digest([variant, test])[:16]
            path = self.ctx.root / "steps" / (name + ".json")
            if path.is_file():
                saved = json.loads(path.read_text())
                if saved.get("input_id") == key:
                    graph = connect_tb_aliases(saved["output"], executed["tb"])
                    self._graph_cache[key] = graph
                    return graph
            return super().graph(test, variant)
        finally:
            self.ctx.build = previous

    def query_ast(self, test, signals, offset=0, limit=80, variant="original"):
        result = super().query_ast(test, signals, offset, limit, variant)
        log = result.get("log")
        if isinstance(log, str) and log:
            material_id = self._record("ast_log", {
                "test": test, "variant": variant, "log_lines": log.splitlines(),
            })
            result.pop("log")
            result["log_page"] = self.read_material(material_id, "log_lines", 0, 80)
        return result

    def query_waveform(self, test, signals, times, history=8, variant="original"):
        run = self._run(test, "verilator", variant)
        path = run.get("vcd")
        if not path or not Path(path).is_file():
            return {"error": "VCD not available", "test": test, "variant": variant}
        last = _last_vcd_tick(path)
        if any(type(tick) is not int or tick < 0 or tick > last for tick in times):
            raise ValueError(f"Sample within the actual VCD window 0..{last}")
        return super().query_waveform(test, signals, times, history, variant)

    def trace_temporal(self, entry, signals=None, times=None, offset=0, limit=80, variant="original"):
        if entry not in self.entries:
            raise ValueError("Unknown entry ID")
        observation = self.entries[entry]
        run = self._run(observation["test"], "verilator", variant)
        path = run.get("vcd")
        if not path or not Path(path).is_file():
            return {"error": "VCD not available", "entry": entry, "variant": variant}
        requested = times or [observation["time"]]
        last = _last_vcd_tick(path)
        if any(type(tick) is not int or tick < 0 or tick > last for tick in requested):
            raise ValueError(f"Trace within the actual VCD window 0..{last}")
        return super().trace_temporal(entry, signals, requested, offset, limit, variant)

    def simulate_variant(self, intervention):
        proposal = validate_intervention(intervention, self.candidate, self.tests)
        for record in self.history["attempts"]:
            if record["intervention"] == proposal:
                return record
        attempt = len(self.history["attempts"])
        if attempt > self.max_revisions:
            raise ValueError("The initial attempt and three revisions have been executed")
        variant = f"attempt_{attempt}"

        def register(sources, tests):
            self.variants[variant] = {"sources": sources, "tests": {test["id"]: test for test in tests}}

        record = trial(self.execution_ctx, f"candidate_{self.index}/{variant}", proposal,
                       self.tests, self.baseline, register=register)
        record["variant"] = variant if variant in self.variants else None
        record["revisions_remaining"] = self.max_revisions - attempt
        self.history["attempts"].append(record)
        self.save_history()
        return record

    def bindings(self):
        tools = super().model_bindings()
        for tool in tools.values():
            parameter = tool["schema"]["parameters"]["properties"].get("variant")
            if parameter:
                parameter["description"] = "Use original or an attempt_N variant returned by simulate_variant."
            material = tool["schema"]["parameters"]["properties"].get("material_id")
            if material:
                material["description"] = "Use a material_id returned by a tool in this session."
        tools["query_ast"]["schema"]["description"] += (
            " Compiler diagnostics are returned as log_page with material_id, field and next_offset; "
            "use read_material to read later log_lines pages."
        )
        tools["simulate_variant"] = {
            "schema": _schema(
                "simulate_variant",
                "Apply a complete independent intervention to original RTL and replay all original TBs with Verilator. Returns checks, logs, probes and the queryable variant ID.",
                {"intervention": {"type": "object", "description": "candidate, edits [{file,line,old,new}], probes [{test,signal,time,role,expected_bits,basis}], reason"}},
                ["intervention"],
            ),
            "call": self.simulate_variant,
            "present": lambda result: history_view(self.execution_ctx, {
                "candidate": self.candidate, "attempts": [result], "queries": []
            })["attempts"][0],
        }
        for name, binding in tools.items():
            function = binding["call"]

            def recorded(_name=name, _function=function, **arguments):
                try:
                    result = _function(**arguments)
                except Exception as error:
                    self.record_query({"tool": _name, "arguments": arguments, "error": str(error)})
                    raise
                saved = {"variant": result.get("variant"), "status": result.get("status")} if _name == "simulate_variant" else result
                self.record_query({"tool": _name, "arguments": arguments, "result": copy.deepcopy(saved)})
                return result

            binding["call"] = recorded
        return tools
