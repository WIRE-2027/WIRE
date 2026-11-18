"""Waveform-guided temporal backtracing over Verilator compiler dependencies."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from ..contracts import digest, edit_offset, original_lines, build_for_tb, patched, save
from heapq import merge
from itertools import groupby
from pathlib import Path
from .simulation import (
    _unique_valid_checks,
    command,
    flags,
    materialize,
    compare,
    number,
)
from .source import OperationPositions, operation_location
from typing import TYPE_CHECKING
from .waveform import read_wave, compact_wave, query_declared_wave
import json, re


if TYPE_CHECKING:
    from ..model import Session


def nodes(value):
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))


def refs(value):
    return sorted(
        {n["name"] for n in nodes(value) if n.get("type") in {"VARREF", "VARXREF"}}
    )


def expression(value):
    holder = {}
    stack = [(value, holder, "root")]
    while stack:
        current, parent, key = stack.pop()
        if isinstance(current, list):
            result = [None] * len(current)
            parent[key] = result
            stack.extend((child, result, index) for index, child in enumerate(current))
        elif isinstance(current, dict):
            result = {}
            parent[key] = result
            for name, child in current.items():
                if (
                    name in {"type", "name", "edgeType", "widthConst"}
                    or isinstance(child, list)
                    and child
                ):
                    result[name] = None if isinstance(child, list) else child
                    if isinstance(child, list):
                        stack.append((child, result, name))
        else:
            parent[key] = current
    return holder["root"]


def compiler_input_ports(tree, source_location, sources):
    """Identify design inputs, including clocks and resets, from compiler port directions."""
    indexed = {n["addr"]: n for n in nodes(tree) if "addr" in n}
    design_scopes = {
        addr
        for addr, n in indexed.items()
        if n.get("type") == "SCOPE"
        and source_location(indexed.get(n.get("modp"), {}))[0] in sources
    }
    roots = {
        addr
        for addr in design_scopes
        if indexed[addr].get("aboveScopep") not in design_scopes
    }
    ports = [
        (n, indexed.get(n.get("varp"), {}))
        for n in indexed.values()
        if n.get("type") == "VARSCOPE" and n.get("scopep") in roots
    ]
    # Verilator splits inout declarations into input/output nodes; retain only unidirectional inputs.
    output_origins = {
        (n["scopep"], v["loc"])
        for n, v in ports
        if v.get("direction") == "OUTPUT" and v.get("loc")
    }
    return sorted(
        n["name"].removeprefix("TOP.")
        for n, v in ports
        if v.get("direction") == "INPUT"
        and (n["scopep"], v.get("loc")) not in output_origins
    )


_AST_FIELDS = frozenset(
    {
        "type",
        "name",
        "addr",
        "loc",
        "varScopep",
        "modp",
        "aboveScopep",
        "scopep",
        "varp",
        "direction",
        "access",
        "edgeType",
        "widthConst",
    }
)


def dependency_ast_node(node):
    import sys

    result = {
        key: value
        for key, value in node.items()
        if key in _AST_FIELDS
        or isinstance(value, dict)
        or isinstance(value, list)
        and value
    }
    for key in ("type", "name"):
        if isinstance(result.get(key), str) and len(result[key]) < 256:
            result[key] = sys.intern(result[key])
    return result


def build_ast(sources, original, build, tb, directory):
    build = build_for_tb(build, tb)
    from .simulation import adapt_build

    compile_sources, build, adaptation = adapt_build(sources, build, tb, directory)
    directory = materialize(directory, compile_sources)
    (directory / "__static_tb.sv").write_text(tb["code"])
    files = build.get("compile_files") or [
        f for f in sources if f.endswith((".v", ".sv"))
    ]
    result = command(
        [
            "verilator",
            "--json-only",
            "--flatten",
            "-O0",
            "--timing",
            "-Wno-fatal",
            "--top-module",
            tb["top"],
            "--Mdir",
            str(directory / "obj"),
            "--json-only-output",
            str(directory / "ast.json"),
            "--json-only-meta-output",
            str(directory / "meta.json"),
            *flags(build, "verilator"),
            *files,
            "__static_tb.sv",
        ],
        directory,
        build.get("compile_timeout", 180),
    )
    (directory / "compile.log").write_text(result["log"])
    if result["code"]:
        output = {
            "status": "compile_error",
            "log": result["log"],
            "relations": [],
            "signals": [],
        }
        return output
    ast_path = directory / "ast.json"
    tree = json.loads(ast_path.read_text(), object_hook=dependency_ast_node)
    meta = json.loads((directory / "meta.json").read_text())
    # -O0 preserves instances; varScope pointers recover exact hierarchical names.
    scopes = {
        n["addr"]: n["name"].removeprefix("TOP.")
        for n in nodes(tree)
        if n.get("type") == "VARSCOPE"
    }
    for node in nodes(tree):
        if (
            node.get("type") in {"VARREF", "VARXREF"}
            and node.get("varScopep") in scopes
        ):
            node["name"] = scopes[node["varScopep"]]
    maps = {
        name: original_lines(original[name], text) for name, text in sources.items()
    }
    relations = []
    positions = OperationPositions(original, sources)

    def source_location(node):
        match = re.match(r"([^,]+),(\d+):", node.get("loc", ""))
        if not match:
            return "", 0
        record = meta["files"].get(match[1], {})
        path = Path(record.get("realpath", ""))
        file = (
            str(path.relative_to(directory)) if path.is_relative_to(directory) else ""
        )
        return file, int(match[2])

    def visit(node, guards=(), clocks=(), clock_senses=()):
        if not isinstance(node, dict):
            return
        kind = node.get("type", "")
        if kind.startswith("ALWAYS"):
            clocks = tuple(refs(node.get("sentreep", [])))
            clock_senses = tuple(
                {"signal": signal, "edge": sense.get("edgeType", "")}
                for sense in nodes(node.get("sentreep", []))
                if sense.get("type") == "SENITEM"
                for signal in refs(sense.get("sensp", []))
            )
        if kind == "IF":
            condition = node.get("condp", [])
            guard_file, guard_line = source_location(node)
            for key, value in node.items():
                if isinstance(value, list):
                    branch = {"thensp": "then", "elsesp": "else"}.get(key)
                    active = (
                        (
                            *guards,
                            {
                                "signals": refs(condition),
                                "expression": expression(condition),
                                "branch": branch,
                                "file": guard_file,
                                "line": guard_line,
                                "original_line": maps.get(guard_file, {}).get(
                                    guard_line
                                ),
                            },
                        )
                        if branch
                        else guards
                    )
                    for child in value:
                        visit(child, active, clocks, clock_senses)
            return
        if kind in {"CASE", "CASEITEM", "WHILE", "FOR"}:
            condition = (
                node.get("condsp", [])
                if kind == "CASEITEM"
                else node.get("condp", []) or node.get("exprp", [])
            )
            branch = "default" if kind == "CASEITEM" and not condition else kind.lower()
            guard_file, guard_line = source_location(node)
            guard = {
                "signals": refs(condition),
                "expression": expression(condition),
                "branch": branch,
                "file": guard_file,
                "line": guard_line,
                "original_line": maps.get(guard_file, {}).get(guard_line),
            }
            if kind == "CASE":
                guard["case_options"] = [
                    expression(item.get("condsp", []))
                    for item in node.get("itemsp", [])
                    if item.get("type") == "CASEITEM" and item.get("condsp")
                ]
            guards = (*guards, guard)
        if kind.startswith("ASSIGN"):
            file, line = source_location(node)
            # Follow testbench aliases to original RTL operations for source-location queries.
            targets = sorted(
                {
                    n["name"]
                    for n in nodes(node.get("lhsp", []))
                    if n.get("type") in {"VARREF", "VARXREF"}
                    and n.get("access") in {"WR", "RW"}
                }
            )
            indices = [
                n["name"]
                for n in nodes(node.get("lhsp", []))
                if n.get("type") in {"VARREF", "VARXREF"} and n.get("access") == "RD"
            ]

            def arms(rhs, active_guards, anchor_file, anchor_line):
                # Verilator may lower if/else assignments to COND; recover branches using expression locations.
                if len(rhs) == 1 and rhs[0].get("type") == "COND":
                    conditional = rhs[0]
                    condition = conditional.get("condp", [])
                    cf, cl = (
                        source_location(condition[0])
                        if condition
                        else (anchor_file, anchor_line)
                    )
                    for key, branch in (("thenp", "then"), ("elsep", "else")):
                        child = conditional.get(key, [])
                        if not child:
                            continue
                        af, al = source_location(child[0])
                        guard = {
                            "signals": refs(condition),
                            "expression": expression(condition),
                            "branch": branch,
                            "file": cf,
                            "line": cl,
                            "original_line": maps.get(cf, {}).get(cl),
                        }
                        yield from arms(
                            child,
                            [*active_guards, guard],
                            af or anchor_file,
                            al or anchor_line,
                        )
                else:
                    yield rhs, active_guards, anchor_file, anchor_line

            for rhs, active_guards, af, al in arms(
                node.get("rhsp", []), list(guards), file, line
            ):
                for target in targets:
                    sources_used = sorted(set(refs(rhs) + indices))
                    port_bridge = file == "__static_tb.sv" and any(
                        s.startswith(target.rsplit(".", 1)[0] + ".")
                        and s.count(".") > target.count(".")
                        for s in sources_used
                    )
                    relations.append(
                        positions.decorate(
                            {
                                "id": f"A{len(relations)+1}",
                                "target": target,
                                "sources": sources_used,
                                "index_sources": sorted(set(indices)),
                                "guards": active_guards,
                                "clocks": list(clocks),
                                "clock_senses": list(clock_senses),
                                "sequential": kind == "ASSIGNDLY",
                                "port_bridge": port_bridge,
                                "expression": expression(rhs),
                                "file": af,
                                "line": al,
                                "assignment_origin": {
                                    "file": file,
                                    "line": line,
                                    "original_line": maps.get(file, {}).get(line),
                                },
                                "original_line": maps.get(af, {}).get(al),
                                "source": (
                                    sources.get(af, "").splitlines()[al - 1]
                                    if af in sources
                                    and 0 < al <= len(sources[af].splitlines())
                                    else ""
                                ),
                            }
                        )
                    )
        for value in node.values():
            if isinstance(value, list):
                for child in value:
                    visit(child, guards, clocks, clock_senses)

    visit(tree)
    output = {
            "status": "ok",
            "operation_position_version": 1,
            "relations": relations,
            "signals": sorted(set(scopes.values()) | set(refs(tree))),
            "input_ports": compiler_input_ports(tree, source_location, sources),
            "semantics": "Static compiler dependencies; guards may include inactive branches; use waveform history for temporal attribution.",
        }
    return output


def connect_tb_aliases(graph, tb):
    """Follow continuous-assignment aliases and packed selections while preserving AST expressions."""
    if graph.get("tb_alias_version") == 1:
        return graph
    lines = tb["code"].splitlines()

    def alias(expr):
        if not isinstance(expr, list) or len(expr) != 1:
            return False
        node = expr[0]
        return (
            node.get("type") in {"VARREF", "VARXREF"}
            or node.get("type") == "SEL"
            and alias(node.get("fromp", []))
            and all(n.get("type") == "CONST" for n in node.get("lsbp", []))
        )

    for row in graph["relations"]:
        line = row.get("line", 0)
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        if (
            row["file"] == "__static_tb.sv"
            and not row["guards"]
            and not row["clock_senses"]
            and not row["sequential"]
            and alias(row["expression"])
            and re.match(r"(?:assign|wire)\b", text)
        ):
            row["port_bridge"] = True
            row["bridge_kind"] = "continuous_tb_alias"
    graph["tb_alias_version"] = 1
    return graph


def last_anchor_divergences(before_path, after_path, signals, end):
    """Compare anchor changes within the original CHECK window."""
    waves = [
        read_wave(path, signals, [end], history_limit=None)
        for path in (before_path, after_path)
    ]
    witnesses = []
    for signal in signals:
        histories = [wave.get("samples", {}).get(signal, []) for wave in waves]
        if any(not rows for rows in histories):
            continue
        histories = [rows[0]["history"] for rows in histories]
        events = [{time: bits for time, bits in history} for history in histories]
        values, different, latest = [None, None], False, None
        for time in sorted(set(events[0]) | set(events[1])):
            for i in (0, 1):
                values[i] = events[i].get(time, values[i])
            known = all(bits and not set(bits.lower()) - {"0", "1"} for bits in values)
            changed = known and int(values[0], 2) != int(values[1], 2)
            if time > 0 and changed and not different:
                latest = time
            different = changed
        if latest is not None:
            witnesses.append((signal, latest))
    return witnesses


def comparable_entry_checks(before, after, entry):
    """Align original and replayed checks by ID, requirement, signal and expectation."""
    left, right = _unique_valid_checks(before), _unique_valid_checks(after)
    ids = {observation["check"]["id"] for observation in entry["observations"]}
    fields = ("requirement", "signal", "expected")
    return bool(ids) and all(
        check in left
        and check in right
        and all(left[check].get(field) == right[check].get(field) for field in fields)
        for check in ids
    )


def allowed_value_terms(expr, signal):
    if not isinstance(expr, list) or len(expr) != 1:
        return None
    node = expr[0]
    if node.get("type") in ("OR", "LOGOR"):
        left = allowed_value_terms(node.get("lhsp", []), signal)
        right = allowed_value_terms(node.get("rhsp", []), signal)
        return left | right if left is not None and right is not None else None
    if node.get("type") == "EQ":
        left, right = node.get("lhsp", []), node.get("rhsp", [])
        for value, constant in ((left, right), (right, left)):
            if (
                set(refs(value)) == {signal}
                and len(constant) == 1
                and constant[0].get("type") == "CONST"
            ):
                return {digest(node)}
    return None


def dependency_paths(graph, file, line, operation_positions=False):
    """Extract signal dependencies at original anchors, preserving branches and roles."""
    location = (
        operation_location
        if operation_positions
        else lambda n: (n.get("file"), n.get("original_line"))
    )
    result = set()
    for row in graph["relations"]:
        anchored = location(row) == (file, line)
        guards = row.get("guards", [])
        if not anchored and not any(location(g) == (file, line) for g in guards):
            continue
        context = tuple((*location(g), g.get("branch")) for g in guards)

        def selected_contexts(expr, signal):
            terms = allowed_value_terms(expr, signal)
            if terms is not None:
                return [
                    context + (("allowed_value", signal, term),)
                    for term in sorted(terms)
                ]
            selections = {
                digest([node.get("lsbp"), node.get("widthConst", node.get("widthp"))])
                for node in nodes(expr)
                if node.get("type") == "SEL" and signal in refs(node.get("fromp", []))
            }
            return [
                context + (("selected_bits", signal, selection),)
                for selection in sorted(selections)
            ] or [context]

        for signal in row["sources"]:
            for selected in selected_contexts(row.get("expression", []), signal):
                result.add((signal, row["target"], "data", selected))
        for guard in guards:
            if anchored or (location(guard) == (file, line)):
                for signal in guard["signals"]:
                    for selected in selected_contexts(
                        guard.get("expression", []), signal
                    ):
                        result.add((signal, row["target"], "control", selected))
    return result


def recovery_row_active(row, paths):
    available = set(row.get("active_sources", [])) | {
        s for g in row.get("guards", []) for s in g["signals"]
    }
    bits = row.get("values", {}).get("input_bits", {})
    wave = {
        "samples": {
            s: [{"time": 0, "history": [[0, value]]}] for s, value in bits.items()
        }
    }
    expressions = [
        row.get("expression", []),
        *[g.get("expression", []) for g in row.get("guards", [])],
    ]
    terms = {digest(n): n for n in nodes(expressions) if n.get("type") == "EQ"}
    for path in paths:
        if path[1] != row["target"] or path[0] not in available:
            continue
        required = [context[-1] for context in path[3] if context[0] == "allowed_value"]
        if not required or all(
            key in terms and _guard_value(terms[key], wave, 0) == 1 for key in required
        ):
            return True
    return False


def validate_linked_edits(sources, edits):
    """Validate exact anchored edits and reject overlapping replacements."""
    spans, result = {}, []
    for edit in edits:
        file, old, new, line = edit["file"], edit["old"], edit["new"], int(edit["line"])
        if file not in sources or not old or old == new:
            raise ValueError("Each edit changes exact source text in a supplied file")
        offset = edit_offset(sources[file], old, line)
        end = offset + len(old)
        if any(offset < b and a < end for a, b in spans.get(file, [])):
            raise ValueError(
                "Linked edits overlap; combine their exact source replacement"
            )
        spans.setdefault(file, []).append((offset, end))
        result.append({"file": file, "line": line, "old": old, "new": new})
    if not result:
        raise ValueError("A relation recovery needs at least one edit")
    return result


def resolve_packed_dependency(requested, dependencies, graph, anchors, edits):
    """Resolve field and bit selections to compiler parent signals at the edited anchor."""
    parents = [
        name
        for name in dependencies
        if requested.startswith(name + ".") or requested.startswith(name + "[")
    ]
    for parent in sorted(parents, key=len, reverse=True):
        source_spelling = parent.rsplit(".", 1)[-1] + requested[len(parent) :]
        if not any(
            re.search(
                r"(?<![\w$])" + re.escape(source_spelling) + r"(?![\w$])", e["new"]
            )
            for e in edits
        ):
            continue
        for row in graph["relations"]:
            if not any(
                (node.get("file"), node.get("original_line")) in anchors
                for node in [row, *row.get("guards", [])]
            ):
                continue
            expressions = [
                row.get("expression", []),
                *[g.get("expression", []) for g in row.get("guards", [])],
            ]
            if any(
                n.get("type") == "SEL" and parent in refs(n.get("fromp", []))
                for n in nodes(expressions)
            ):
                return parent
    return requested


def linked_dependency_paths(graph, paths, source, target=None):
    """Retain added source-to-target paths, including instance port connections."""
    edges = [
        (s, row["target"])
        for row in graph["relations"]
        for s in [*row["sources"], *(s for g in row["guards"] for s in g["signals"])]
    ]

    def closure(seeds, pairs):
        reached, pending = set(seeds), list(seeds)
        adjacency = {}
        for a, b in pairs:
            adjacency.setdefault(a, set()).add(b)
        while pending:
            for b in adjacency.get(pending.pop(), ()):
                if b not in reached:
                    reached.add(b)
                    pending.append(b)
        return reached

    # Bind a packed selection only when the retained assignment explicitly writes that field.
    if target and target not in {row["target"] for row in graph["relations"]}:
        parents = {
            row["target"]
            for row in graph["relations"]
            if target.startswith(row["target"] + ".")
            or target.startswith(row["target"] + "[")
        }
        for parent in sorted(parents, key=len, reverse=True):
            spelling = parent.rsplit(".", 1)[-1] + target[len(parent) :]
            lhs = r"(?<![\w$])" + re.escape(spelling) + r"\s*(?:<=|=(?!=))"
            if any(
                row["target"] == parent and re.search(lhs, row.get("source", ""))
                for row in graph["relations"]
            ):
                target = parent
                break
    downstream = closure([source], edges)
    upstream = closure([target], [(b, a) for a, b in edges]) if target else None
    return {
        p
        for p in paths
        if p[0] in downstream and (upstream is None or p[1] in upstream)
    }


def added_dependency_paths(before, after, file, line, operation_positions=False):
    """Compare original and augmented ASTs for added data and control dependencies."""
    previous, current = dependency_paths(
        before, file, line, operation_positions
    ), dependency_paths(after, file, line, operation_positions)

    def group(path):
        return (*path[:3], tuple(c for c in path[3] if c[0] != "allowed_value"))

    def alternatives(paths):
        values = {}
        for path in paths:
            values.setdefault(group(path), set()).update(
                c for c in path[3] if c[0] == "allowed_value"
            )
        return values

    before_terms, after_terms = alternatives(previous), alternatives(current)
    existing = {}
    for path in previous:
        existing.setdefault(path[:3], []).append(set(path[3]))
    added = set()
    for path in current:
        if any(context.issubset(path[3]) for context in existing.get(path[:3], [])):
            continue
        # Adding an allowed branch extends the relation; replacing a comparison changes an existing operation.
        old_terms = before_terms.get(group(path), set())
        if old_terms and not old_terms <= after_terms.get(group(path), set()):
            continue
        added.add(path)
    return added


def backtrace(graph, signals, depth=None):
    seen, selected, frontier, boundary = set(), {}, set(signals), set()
    by_target = {}
    for row in graph.get("relations", []):
        by_target.setdefault(row["target"], []).append(row)
    rounds = 0
    while frontier - seen and (depth is None or rounds < depth):
        rounds += 1
        current, frontier = frontier - seen, set()
        if not current:
            break
        seen.update(current)
        for row in (row for target in current for row in by_target.get(target, [])):
            if row["target"] in current:
                if row["file"] == "__static_tb.sv" and not row.get("port_bridge"):
                    boundary.add(row["target"])
                    continue
                selected[row["id"]] = row
                frontier.update(row["sources"])
                frontier.update(row["clocks"])
                frontier.update(s for guard in row["guards"] for s in guard["signals"])
    return {
        "status": graph["status"],
        "relations": list(selected.values()),
        "signals": sorted(seen),
        "unexpanded": sorted(frontier - seen),
        "unresolved": sorted(set(signals) - set(graph["signals"])),
        "testbench_inputs": sorted(boundary),
    }


def _wave_value(wave, signal, time, before=False):
    samples = wave.get("samples", {}).get(signal, [])
    sample = next((sample for sample in samples if sample["time"] >= time), None)
    if sample is None:
        return None
    history = sample.get("history", [])
    index = bisect_left(history, time, key=lambda event: event[0])
    if before and index < len(history) and history[index][0] == time:
        # VCD records final tick values; same-tick delta-cycle ordering remains unresolved.
        return None
    index = (
        index if before else bisect_right(history, time, key=lambda event: event[0])
    ) - 1
    value = history[index][1] if index >= 0 else None
    if not isinstance(value, str) or not value or set(value.lower()) - {"0", "1"}:
        return None
    return int(value, 2)


def _expression_width(expr, wave):
    if isinstance(expr, list):
        return _expression_width(expr[0], wave) if len(expr) == 1 else None
    if not isinstance(expr, dict):
        return None
    if expr.get("widthConst") is not None:
        return int(expr["widthConst"])
    kind = expr.get("type")
    if kind in {"VARREF", "VARXREF"}:
        return wave.get("widths", {}).get(expr.get("name"))
    if kind == "CONST":
        match = re.match(r"(\d+)'", str(expr.get("name", "")))
        return int(match[1]) if match else None
    if kind in {
        "LOGNOT",
        "LOGAND",
        "LOGOR",
        "EQ",
        "NEQ",
        "LT",
        "LTE",
        "GT",
        "GTE",
        "REDOR",
    }:
        return 1
    if kind == "SEL":
        return _guard_value(expr.get("widthp", []), wave, 0)
    return None


def _guard_value(expr, wave, time, before=False):
    """Evaluate a control expression at a waveform time; return None when unresolved."""
    if isinstance(expr, list):
        return _guard_value(expr[0], wave, time, before) if len(expr) == 1 else None
    if not isinstance(expr, dict):
        return None
    kind = expr.get("type")
    if kind in {"VARREF", "VARXREF"}:
        return _wave_value(wave, expr.get("name", ""), time, before)
    if kind == "CONST":
        literal = str(expr.get("name", "")).lower().replace("_", "")
        match = re.search(r"(?:\d+)?'([bdho])([0-9a-f]+)$", literal)
        if match:
            return int(match[2], {"b": 2, "d": 10, "h": 16, "o": 8}[match[1]])
        return int(literal) if literal.isdecimal() else None
    if kind == "SEL":
        value = _guard_value(expr.get("fromp", []), wave, time, before)
        shift = _guard_value(expr.get("lsbp", []), wave, time, before)
        width = expr.get("widthConst")
        if width is None:
            width = _guard_value(expr.get("widthp", []), wave, time, before)
        if (
            value is None
            or shift is None
            or not str(width).isdigit()
            or int(width) <= 0
        ):
            return None
        return (value >> shift) & ((1 << int(width)) - 1)
    if kind == "REDOR":
        value = _guard_value(
            expr.get("lhsp", expr.get("fromp", [])), wave, time, before
        )
        return None if value is None else int(value != 0)
    operands = [
        child
        for key, value in expr.items()
        if key not in {"type", "name", "edgeType"}
        for child in (value if isinstance(value, list) else [])
    ]
    values = [_guard_value(child, wave, time, before) for child in operands]
    # A control result can be known despite unknown inputs, such as bitwise AND with zero.
    if len(values) == 2:
        if kind in {"AND", "LOGAND"} and 0 in values:
            return 0
        if kind == "LOGOR" and any(
            value is not None and value != 0 for value in values
        ):
            return 1
    if not values or any(value is None for value in values):
        return None
    if kind == "LOGNOT" and len(values) == 1:
        return int(not values[0])
    if kind == "NOT" and len(values) == 1:
        width = _expression_width(operands[0], wave)
        return (~values[0]) & ((1 << width) - 1) if width and width > 0 else None
    if len(values) != 2:
        return None
    left, right = values
    operations = {
        "LOGAND": lambda: int(bool(left) and bool(right)),
        "LOGOR": lambda: int(bool(left) or bool(right)),
        "AND": lambda: left & right,
        "OR": lambda: left | right,
        "XOR": lambda: left ^ right,
        "EQ": lambda: int(left == right),
        "NEQ": lambda: int(left != right),
        "LT": lambda: int(left < right),
        "LTE": lambda: int(left <= right),
        "GT": lambda: int(left > right),
        "GTE": lambda: int(left >= right),
    }
    return operations[kind]() if kind in operations else None


def _guard_decisions(guards, wave, time, before=False):
    decisions = []
    selector, options = None, []
    for guard in guards:
        branch = guard["branch"]
        if branch == "case":
            selector = _guard_value(guard["expression"], wave, time, before)
            options = [
                _guard_value(option, wave, time, before)
                for item in guard.get("case_options", [])
                for option in item
            ]
            decisions.append(None if selector is None else True)
        elif branch == "caseitem":
            values = [
                _guard_value(option, wave, time, before)
                for option in guard["expression"]
            ]
            decisions.append(
                None
                if selector is None
                or not values
                or any(value is None for value in values)
                else selector in values
            )
        elif branch == "default":
            decisions.append(
                None
                if selector is None or any(value is None for value in options)
                else selector not in options
            )
        else:
            observed = _guard_value(guard["expression"], wave, time, before)
            decisions.append(
                None
                if observed is None or branch not in {"then", "else"}
                else bool(observed) == (branch == "then")
            )
    return decisions


def _active_data_refs(expr, wave, time, before=False):
    """Select observed expression branches; None denotes an unknown activation state."""
    active, stack = set(), [expr]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, dict):
            if current.get("type") in {"VARREF", "VARXREF"}:
                active.add(current["name"])
            elif current.get("type") == "COND":
                condition = current.get("condp", [])
                selected = _guard_value(condition, wave, time, before)
                if selected is None:
                    return None
                active.update(refs(condition))
                stack.append(current.get("thenp" if selected else "elsep", []))
            else:
                stack.extend(
                    value for value in current.values() if isinstance(value, list)
                )
    return active


def _clock_edges(wave, senses, time):
    streams = []

    def preceding(events):
        stop = bisect_right(events, time)
        return (events[index] for index in range(stop - 1, -1, -1))

    for sense in senses:
        signal, edge = sense["signal"], sense["edge"].upper()
        samples = wave.get("samples", {}).get(signal, [])
        sample = next((sample for sample in samples if sample["time"] >= time), None)
        if sample is None:
            continue
        history = sample.get("history", [])
        events = [
            when
            for index, (when, value) in enumerate(history)
            if index
            and (
                (edge == "POS" and history[index - 1][1] == "0" and value == "1")
                or (edge == "NEG" and history[index - 1][1] == "1" and value == "0")
            )
        ]
        streams.append(preceding(events))
    return (when for when, _ in groupby(merge(*streams, reverse=True)))


def temporal_backtrace(graph, signals, times, wave, depth=None):
    """Trace waveform-supported dependencies and report unresolved temporal conditions."""
    frontier = {(signal, time) for signal in signals for time in times}
    selected, suppressed = [], []
    seen, uncertain, boundary = set(), [], set()
    by_target = {}
    for row in graph.get("relations", []):
        by_target.setdefault(row["target"], []).append(row)
    guard_sets = {
        id(row["guards"]): row["guards"] for row in graph.get("relations", [])
    }

    def decisions(identity, tick, before):
        return tuple(_guard_decisions(guard_sets[identity], wave, tick, before))

    rounds = 0
    while frontier - seen and (depth is None or rounds < depth):
        rounds += 1
        current, frontier = frontier - seen, set()
        if not current:
            break
        seen.update(current)
        for signal, time in sorted(current):
            rows = by_target.get(signal, [])
            sequential = [row for row in rows if row["sequential"]]

            def row_events(row):
                return (
                    (edge, row["id"])
                    for edge in _clock_edges(wave, row.get("clock_senses", []), time)
                )

            edge_events = merge(*(row_events(row) for row in sequential), reverse=True)
            # For held registers, search earlier edges until the last effective write.
            timed_rows = [(row, None) for row in rows if not row["sequential"]]
            observed_edge = False
            for edge, events in groupby(edge_events, key=lambda event: event[0]):
                observed_edge = True
                writers = {identity for _, identity in events}
                edge_rows = [(row, edge) for row in sequential if row["id"] in writers]
                active_rows, unknown_rows = [], []
                for row, _ in edge_rows:
                    observed = (
                        decisions(id(row["guards"]), edge, True)
                        if row["guards"]
                        else ()
                    )
                    if False in observed:
                        suppressed.append(
                            {
                                "relation": row["id"],
                                "file": row["file"],
                                "original_line": row["original_line"],
                                "signal": signal,
                                "time": time,
                                "active_clock_edge": edge,
                                "control_signals": sorted(
                                    {
                                        name
                                        for guard in row["guards"]
                                        for name in guard["signals"]
                                    }
                                ),
                                "activity": "control_suppressed",
                            }
                        )
                    elif None in observed:
                        unknown_rows.append(row)
                    else:
                        active_rows.append(row)
                if unknown_rows:
                    uncertain.extend(
                        {
                            "relation": row["id"],
                            "signal": signal,
                            "time": time,
                            "reason": "branch activity not established by waveform",
                        }
                        for row in unknown_rows
                    )
                    break
                if active_rows:
                    if len(active_rows) > 1:
                        uncertain.append(
                            {
                                "signal": signal,
                                "time": time,
                                "reason": "multiple active writers; ordering not established by waveform",
                            }
                        )
                    else:
                        timed_rows.append((active_rows[0], edge))
                    break
            if sequential and not observed_edge:
                uncertain.append(
                    {
                        "signal": signal,
                        "time": time,
                        "reason": "active clock edge not observed",
                    }
                )
            for row, edge in timed_rows:
                if row["file"] == "__static_tb.sv" and not row.get("port_bridge"):
                    boundary.add(signal)
                    continue
                read_time = edge if row["sequential"] else time
                guards = (
                    decisions(id(row["guards"]), read_time, row["sequential"])
                    if row["guards"]
                    else ()
                )
                if False in guards:
                    suppressed.append(
                        {
                            "relation": row["id"],
                            "file": row["file"],
                            "original_line": row["original_line"],
                            "signal": signal,
                            "time": time,
                            "active_clock_edge": edge,
                            "control_signals": sorted(
                                {
                                    name
                                    for guard in row["guards"]
                                    for name in guard["signals"]
                                }
                            ),
                            "activity": "control_suppressed",
                        }
                    )
                    continue
                if None in guards:
                    uncertain.append(
                        {
                            "relation": row["id"],
                            "signal": signal,
                            "time": time,
                            "reason": "branch activity not established by waveform",
                        }
                    )
                    continue
                # Sequential inputs use values before the update edge; combinational inputs use the current tick.
                source_time = max(0, edge - 1) if edge is not None else time
                data_sources = (
                    _active_data_refs(
                        row["expression"], wave, read_time, row["sequential"]
                    )
                    if row.get("expression")
                    else set(row["sources"])
                )
                if data_sources is None:
                    uncertain.append(
                        {
                            "relation": row["id"],
                            "signal": signal,
                            "time": time,
                            "reason": "conditional data arm not established by waveform",
                        }
                    )
                    continue
                active_sources = sorted(
                    data_sources | set(row.get("index_sources", []))
                )
                selected.append(
                    {
                        **row,
                        "observed_time": time,
                        "source_time": source_time,
                        "active_clock_edge": edge,
                        "activity": "waveform_supported",
                        "active_sources": active_sources,
                    }
                )
                frontier.update(
                    (source, source_time)
                    for source in [
                        *active_sources,
                        *row["clocks"],
                        *(
                            signal
                            for guard in row["guards"]
                            for signal in guard["signals"]
                        ),
                    ]
                )
    visited = []
    visited.extend({"signal": signal, "time": time} for signal, time in sorted(seen))
    return {
        "status": graph["status"],
        "relations": selected,
        "suppressed": suppressed,
        "suppressed_total": len(suppressed),
        "visited": visited,
        "unexpanded": [
            {"signal": signal, "time": time} for signal, time in sorted(frontier - seen)
        ],
        "uncertain": uncertain,
        "testbench_inputs": sorted(boundary),
        "unresolved": sorted(set(signals) - set(graph.get("signals", []))),
    }


def _schema(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


class TBATools:
    """Return compact material pages with access to complete records by content ID."""

    def __init__(self, ctx: Session, tests, entries):
        self.ctx = ctx
        self.entries = {e["id"]: e for e in entries}
        self.variants = {
            "original": {"sources": ctx.sources, "tests": {t["id"]: t for t in tests}}
        }
        self.materials, self.augmentations = {}, []
        self._graph_cache = {}

    def _variant(self, variant):
        if variant not in self.variants:
            raise ValueError(
                "Unknown variant; use original or an augmentation variant returned by the tool"
            )
        return self.variants[variant]

    def _test(self, test, variant="original"):
        tests = self._variant(variant)["tests"]
        if test not in tests:
            raise ValueError(f"Unknown test {test}; available: {list(tests)}")
        return tests[test]

    def _run(self, test, backend, variant="original"):
        runs = self._test(test, variant)["runs"]
        if backend not in runs:
            raise ValueError(f"Unknown backend {backend}; available: {list(runs)}")
        return runs[backend]

    def graph(self, test, variant="original"):
        trial = self._test(test, variant)
        sources = self._variant(variant)["sources"]
        cache_key = digest([sources, self.ctx.build, trial["tb"]])
        if cache_key in self._graph_cache:
            return self._graph_cache[cache_key]
        name = "tba_ast_" + digest([variant, test])[:16]
        graph = self.ctx.compute(
            name,
            [sources, self.ctx.build, trial["tb"]],
            lambda: build_ast(
                sources,
                self.ctx.sources,
                self.ctx.build,
                trial["tb"],
                self.ctx.root / "work" / "tba" / name,
            ),
        )
        graph = connect_tb_aliases(graph, trial["tb"])
        self._graph_cache[cache_key] = graph
        return graph

    def _record(self, kind, payload):
        value = {"kind": kind, **payload}
        mid = "M_" + digest(value)[:20]
        self.materials[mid] = value
        return mid

    def read_material(self, material_id, field="relations", offset=0, limit=80):
        """Read material pages, preserving dictionary keys and complete selected values."""
        if material_id not in self.materials:
            raise ValueError("Unknown material_id")
        material = self.materials[material_id]
        if offset < 0 or limit < 1:
            raise ValueError("offset must be nonnegative and limit must be positive")
        value = material.get(field)
        if not isinstance(value, (list, dict)):
            raise ValueError(
                f"Choose a list or dictionary field: {[k for k,v in material.items() if isinstance(v,(list,dict))]}"
            )
        stop = offset + limit
        items = (
            {key: value[key] for key in sorted(value)[offset:stop]}
            if isinstance(value, dict)
            else value[offset:stop]
        )
        # Keep material identity, time units and diagnostics across pages and variants.
        metadata = (
            "kind",
            "test",
            "entry",
            "variant",
            "backend",
            "timescale",
            "status",
            "run_status",
            "error",
            "wave_error",
            "unresolved",
            "uncertain",
            "checker_wave_conflicts",
            "unexpanded",
        )
        page = {
            **{
                key: material[key]
                for key in metadata
                if key in material and key != field
            },
            "material_id": material_id,
            "field": field,
            "total": len(value),
            "items": items,
            "next_offset": stop if stop < len(value) else None,
        }
        if material["kind"] == "temporal_trace" and field == "relations":
            # Share static statements while preserving every visit's timestamp and observed values.
            event_fields = {
                "observed_time",
                "source_time",
                "active_clock_edge",
                "activity",
                "active_sources",
                "values",
            }
            statements, events = {}, []
            for row in items:
                statement = {k: v for k, v in row.items() if k not in event_fields}
                key = next(
                    (k for k, saved in statements.items() if saved == statement),
                    f"S{len(statements)+1}",
                )
                statements[key] = statement
                events.append(
                    {
                        "statement": key,
                        **{k: v for k, v in row.items() if k in event_fields},
                    }
                )
            page.update(statements=statements, items=events)
        return page

    def read_source(self, file, start, end, variant="original"):
        sources = self._variant(variant)["sources"]
        if file not in sources:
            raise ValueError("File must be one of the supplied source files")
        lines = sources[file].splitlines()
        start, end = int(start), int(end)
        if not 1 <= start <= end <= len(lines):
            raise ValueError(f"Valid source line range: 1..{len(lines)}")
        mapping = original_lines(self.ctx.sources[file], sources[file])
        result = {
            "variant": variant,
            "file": file,
            "start": start,
            "end": end,
            "lines": [
                {"line": n, "original_line": mapping[n], "text": lines[n - 1]}
                for n in range(start, end + 1)
            ],
        }
        return {"material_id": self._record("source", result), **result}

    def read_test(self, test, variant="original"):
        trial = self._test(test, variant)
        tb = trial["tb"]
        build = trial.get("build") or build_for_tb(self.ctx.build, tb)
        metadata = {
            "test": test,
            "variant": variant,
            "top": tb["top"],
            "run_status": trial["runs"]["verilator"]["status"],
            "build": {
                key: build[key]
                for key in (
                    "compile_files",
                    "include_dirs",
                    "defines",
                    "verilator_flags",
                )
                if key in build
            },
        }
        lines = [
            {"line": i, "text": line}
            for i, line in enumerate(tb["code"].splitlines(), 1)
        ]
        mid = self._record("test", {**metadata, "lines": lines})
        return {
            **metadata,
            "material_id": mid,
            "code": "\n".join(f"{row['line']}: {row['text']}" for row in lines),
        }

    def query_ast(self, test, signals, offset=0, limit=80, variant="original"):
        graph = self.graph(test, variant)
        result = backtrace(graph, signals, depth=None)
        mid = self._record("static_ast", {"test": test, "variant": variant, **result})
        return {
            "status": result["status"],
            "unresolved": result["unresolved"],
            "testbench_inputs": result["testbench_inputs"],
            "log": graph.get("log"),
            **self.read_material(mid, "relations", offset, limit),
        }

    def query_waveform(self, test, signals, times, history=8, variant="original"):
        run = self._run(test, "verilator", variant)
        result = query_declared_wave(
            run.get("vcd", ""), signals, times, history_limit=history
        )
        mid = self._record(
            "waveform",
            {"test": test, "backend": "verilator", "variant": variant, **result},
        )
        return {"material_id": mid, "run_status": run["status"], **compact_wave(result)}

    def _trace(self, entry, signals, times, variant="original"):
        test, backend = entry["test"], entry["backend"]
        graph = self.graph(test, variant)
        static = backtrace(graph, signals, depth=None)
        needed = set(signals)
        for row in static["relations"]:
            needed.update([row["target"], *row["sources"], *row["clocks"]])
            needed.update(s for guard in row["guards"] for s in guard["signals"])
        run = self._run(test, backend, variant)
        wave = read_wave(
            run.get("vcd", ""),
            sorted(needed),
            times,
            history_limit=None,
            compact_history=True,
        )
        trace = temporal_backtrace(graph, signals, times, wave)

        def history(signal, time):
            return next(
                (
                    sample.get("history", [])
                    for sample in wave.get("samples", {}).get(signal, [])
                    if sample["time"] >= time
                ),
                [],
            )

        def bits_at(signal, time):
            events = history(signal, time)
            index = bisect_right(events, time, key=lambda event: event[0]) - 1
            return events[index][1] if index >= 0 else None

        for row in trace["relations"]:
            inputs = sorted(
                set(row["active_sources"])
                | {signal for guard in row["guards"] for signal in guard["signals"]}
            )
            values = {
                "target_bits": bits_at(row["target"], row["observed_time"]),
                "input_bits": {
                    signal: bits_at(signal, row["source_time"]) for signal in inputs
                },
            }
            edge = row.get("active_clock_edge")
            changed = []
            if edge is not None:
                for signal in inputs:
                    events = history(signal, edge)
                    first = max(
                        1, bisect_left(events, edge, key=lambda event: event[0])
                    )
                    stop = bisect_right(events, edge, key=lambda event: event[0])
                    if any(
                        events[index][1] != events[index - 1][1]
                        for index in range(first, stop)
                    ):
                        changed.append(signal)
            # Keep pre-edge and same-tick values separate when sampling order is unresolved.
            if changed:
                values["changed_at_edge"] = changed
            row["values"] = values
        conflicts = []
        for signal, values in wave.get("samples", {}).items():
            for sample in values:
                bits = sample["value"]
                if not bits or set(bits) - {"0", "1"}:
                    continue
                for check in run.get("checks", []):
                    if (
                        check["signal"] == signal
                        and check["time"] == sample["time"]
                        and check.get("known")
                    ):
                        if number(check["actual"]) % (1 << len(bits)) != int(bits, 2):
                            conflicts.append(
                                {
                                    "check": check["id"],
                                    "time": sample["time"],
                                    "signal": signal,
                                    "checker_actual": check["actual"],
                                    "vcd_final_bits": bits,
                                }
                            )
        result = {
            "entry": entry["id"],
            "test": test,
            "backend": backend,
            "variant": variant,
            "starts": [{"signal": s, "time": t} for s in signals for t in times],
            "timescale": wave.get("timescale"),
            "run_status": run["status"],
            "checker_wave_conflicts": conflicts,
            **trace,
        }
        if wave.get("error"):
            result["wave_error"] = wave["error"]
        return result

    def trace_temporal(
        self, entry, signals=None, times=None, offset=0, limit=80, variant="original"
    ):
        if entry not in self.entries:
            raise ValueError("Unknown entry ID")
        e = self.entries[entry]
        result = self._trace(
            e, list(signals or [e["signal"]]), list(times or [e["time"]]), variant
        )
        mid = self._record("temporal_trace", result)
        return {
            "entry": entry,
            "status": result["status"],
            "run_status": result["run_status"],
            "timescale": result.get("timescale"),
            "wave_error": result.get("wave_error"),
            "unresolved": result["unresolved"],
            "uncertain": result["uncertain"],
            "suppressed_count": result.get(
                "suppressed_total", len(result["suppressed"])
            ),
            "checker_wave_conflicts": result["checker_wave_conflicts"],
            **self.read_material(mid, "relations", offset, limit),
        }

    def augment_and_replay(
        self,
        entry,
        file=None,
        line=None,
        old=None,
        new=None,
        missing_signal=None,
        reason="",
        edits=None,
        anchor=None,
        relation=None,
        entries=None,
        preflight_only=False,
        test_connections=None,
    ):
        """Apply linked edits to an independent original-RTL copy, then replay and trace."""
        if anchor:
            file, line = anchor["file"], int(anchor["line"])
        relation = relation or {}
        missing_signal = relation.get("source", missing_signal)
        if (
            entry not in self.entries
            or file not in self.ctx.sources
            or not missing_signal
        ):
            raise ValueError(
                "Use a supplied entry, original anchor and relation source"
            )
        edits = (
            edits
            if edits is not None
            else [{"file": file, "line": line, "old": old, "new": new}]
        )
        edits = validate_linked_edits(self.ctx.sources, edits)
        proposal = {
            "entry": entry,
            "entries": entries or [entry],
            "file": file,
            "line": int(line),
            "edits": edits,
            "missing_signal": missing_signal,
            "relation": relation,
            "reason": reason,
        }
        if test_connections:
            proposal["test_connections"] = test_connections
        variant = "aug_" + digest(proposal)[:16]
        sources = patched(
            self.ctx.sources,
            sorted(
                edits,
                key=lambda x: (
                    x["file"],
                    -edit_offset(self.ctx.sources[x["file"]], x["old"], x["line"]),
                ),
            ),
        )
        source = self.ctx.sources[file]
        anchors = {(file, int(line))}
        for edit in edits:
            start = edit_offset(
                self.ctx.sources[edit["file"]], edit["old"], edit["line"]
            )
            first = self.ctx.sources[edit["file"]][:start].count("\n") + 1
            anchors.update(
                (edit["file"], n)
                for n in range(first, first + edit["old"].count("\n") + 1)
            )
        # Include the edited range's actual mapped anchors when inserted lines map to a neighboring line.
        from difflib import SequenceMatcher

        for edited_file in dict.fromkeys(edit["file"] for edit in edits):
            mapping = original_lines(
                self.ctx.sources[edited_file], sources[edited_file]
            )
            for tag, _, _, first, last in SequenceMatcher(
                None,
                self.ctx.sources[edited_file].splitlines(),
                sources[edited_file].splitlines(),
                autojunk=False,
            ).get_opcodes():
                if tag != "equal":
                    anchors.update(
                        (edited_file, mapping[n]) for n in range(first + 1, last + 1)
                    )
        # Include operands on subsequent lines of the same assignment in compiler-anchor checks.
        from .source import SourceLocator

        locator = SourceLocator(
            {f: self.ctx.sources[f] for f, _ in anchors if f in self.ctx.sources}
        )
        for module in locator.modules:
            for assignment in locator.assignments(module):
                if any(
                    f == assignment["file"]
                    and assignment["line"] <= n <= assignment["line_end"]
                    for f, n in anchors
                ):
                    anchors.add((assignment["file"], assignment["line"]))
        e = self.entries[entry]
        baseline = self._test(e["test"])
        from .simulation import recovered_test_inputs

        replay_tb = recovered_test_inputs(baseline["tb"], sources, test_connections)

        # An anchor can be an assignment or an if condition stored in the assignment guards.
        def at_anchor(node):
            return (node.get("file"), node.get("original_line")) in anchors

        def dependencies(graph):
            found = set()
            for row in graph["relations"]:
                if at_anchor(row):
                    found.update(row["sources"])
                    found.update(s for guard in row["guards"] for s in guard["signals"])
                for guard in row["guards"]:
                    if at_anchor(guard):
                        found.update(guard["signals"])
            return found

        # Check the added AST dependency before replaying; bind resulting waveforms to the variant.
        graph_cache_key = digest([sources, self.ctx.build, replay_tb])
        graph_name = "tba_ast_oppos_v1_" + graph_cache_key[:16]
        graph_after = self._graph_cache.get(graph_cache_key)
        if graph_after is None:
            graph_after = self.ctx.compute(
                graph_name,
                [sources, self.ctx.build, replay_tb],
                lambda: build_ast(
                    sources,
                    self.ctx.sources,
                    self.ctx.build,
                    replay_tb,
                    self.ctx.root / "work" / "tba" / graph_name,
                ),
            )
            graph_after = connect_tb_aliases(graph_after, replay_tb)
            self._graph_cache[graph_cache_key] = graph_after
        if graph_after["status"] != "ok":
            raise ValueError(
                "Modified AST could not be built; replay was not run: "
                + str(graph_after.get("log", graph_after["status"]))
            )
        graph_before = self.graph(e["test"])
        before, after = dependencies(graph_before), dependencies(graph_after)
        # Resolve short names only within added anchor dependencies; retain ambiguity as a failure.
        requested_signal = missing_signal
        new_paths = set().union(
            *(
                added_dependency_paths(graph_before, graph_after, f, n)
                for f, n in anchors
            )
        )
        new_dependencies = {path[0] for path in new_paths}
        if missing_signal not in new_dependencies and "." not in missing_signal:
            matches = [
                signal
                for signal in new_dependencies
                if signal.rsplit(".", 1)[-1] == missing_signal
            ]
            if len(matches) > 1:
                page = self.trace_temporal(e["id"])
                original = self.materials[page["material_id"]]
                scopes = {
                    row["target"].rsplit(".", 1)[0]
                    for row in original["relations"]
                    if at_anchor(row) or any(at_anchor(g) for g in row["guards"])
                }
                if not scopes:
                    scopes = {
                        row["target"].rsplit(".", 1)[0]
                        for row in graph_before["relations"]
                        if row["target"] == e["signal"] and row.get("file") == file
                    }
                matches = [
                    signal for signal in matches if signal.rsplit(".", 1)[0] in scopes
                ]
            if len(matches) == 1:
                missing_signal = matches[0]
        if missing_signal not in new_dependencies:
            missing_signal = resolve_packed_dependency(
                missing_signal, new_dependencies, graph_after, anchors, edits
            )
        unfiltered_new_paths = new_paths
        new_paths = linked_dependency_paths(
            graph_after, new_paths, missing_signal, relation.get("target")
        )
        operation_paths = set().union(
            *(
                added_dependency_paths(graph_before, graph_after, f, n, True)
                for f, n in anchors
            )
        )
        operation_paths = linked_dependency_paths(
            graph_after, operation_paths, missing_signal, relation.get("target")
        )
        added = bool(new_paths)
        if not added:
            actual_edges = sorted({(p[0], p[1], p[2]) for p in unfiltered_new_paths})
            detail = (
                (
                    " Actual newly added anchor edges (source, target, role): "
                    + json.dumps(actual_edges, ensure_ascii=False)
                )
                if actual_edges
                else " No new anchor edge was produced."
            )
            raise ValueError(
                "The proposed source-to-target relation was not added at the anchor; replay was not run. "
                "Check the exact named source and target against the compiler paths. "
                "A change to existing logic alone is not augmentation." + detail
            )
        if preflight_only:
            return {
                "variant": variant,
                "proposal": proposal,
                "dependency_added": True,
                "new_dependency_paths": sorted(new_paths, key=str),
                "preflight_only": True,
            }
        affected = {
            row["id"]
            for row in graph_after["relations"]
            if set().union(
                *(dependency_paths({"relations": [row]}, f, n) for f, n in anchors)
            )
            & new_paths
        }
        ast_status = graph_after["status"]
        replay = self.ctx.simulate("tba_" + variant, replay_tb, sources=sources)
        self.variants[variant] = {"sources": sources, "tests": {e["test"]: replay}}
        comparison = compare(baseline, replay)
        old_run = baseline["runs"][e["backend"]]
        new_run = replay["runs"].get(e["backend"], {})

        # Align CHECK identities and preserve timestamp changes separately.
        def signature(check):
            return tuple(
                str(check.get(k, ""))
                for k in ("id", "requirement", "signal", "expected")
            )

        full_run_comparable = old_run["status"] == new_run.get("status") == "ok" and [
            signature(c) for c in old_run["checks"]
        ] == [signature(c) for c in new_run.get("checks", [])]
        comparable = comparable_entry_checks(old_run, new_run, e)
        aligned_checks = _unique_valid_checks(new_run)

        def replay_time(observed):
            times = {
                aligned_checks[o["check"]["id"]].get("time")
                for o in observed["observations"]
                if o["check"]["id"] in aligned_checks
            }
            return (
                next(iter(times))
                if len(times) == 1 and None not in times
                else observed["time"]
            )

        trace = self._trace(e, [e["signal"]], [replay_time(e)], variant)
        trace_id = self._record("temporal_trace", trace)
        # A corrected output can select its default and remove the edit from the new slice; inspect the original boundary event.
        original_page = self.trace_temporal(e["id"])
        original_trace = self.materials[original_page["material_id"]]
        witnesses = sorted(
            {
                (row["target"], row["observed_time"])
                for row in original_trace["relations"]
                if at_anchor(row) or any(at_anchor(g) for g in row["guards"])
            }
        )
        witness_source = "original_temporal_path"
        fixed_ids = set(comparison.get(e["backend"], {}).get("fixed", []))
        if not witnesses and any(
            observation["check"]["id"] in fixed_ids for observation in e["observations"]
        ):
            targets = sorted({path[1] for path in new_paths})
            witnesses = last_anchor_divergences(
                old_run.get("vcd", ""),
                new_run.get("vcd", ""),
                targets,
                max(e["time"], replay_time(e)),
            )
            witness_source = "paired_anchor_divergence_before_fixed_CHECK"
        boundary_traces = [
            self._trace(e, [signal], [time], variant) for signal, time in witnesses
        ]
        boundary_materials = [
            self._record("temporal_trace", observations) for observations in boundary_traces
        ]
        # Suppressed paths retain relation IDs for resolving their original control locations.
        checked_traces = [trace, *boundary_traces]
        active_rows = [
            row
            for observations in checked_traces
            for row in observations["relations"]
            if row["id"] in affected
        ]
        active = any(recovery_row_active(row, new_paths) for row in active_rows)
        suppression_sources = {
            path[0]
            for path in new_paths
            if not any(c[0] == "allowed_value" for c in path[3])
        }
        suppressed, inactive_read_times = False, set()
        for observations in checked_traces:
            rows = observations["suppressed"]
            affected_rows = [row for row in rows if row["relation"] in affected]
            suppressed |= any(
                suppression_sources.intersection(row["control_signals"])
                for row in affected_rows
            )
            inactive_read_times.update(
                (
                    max(0, row["active_clock_edge"] - 1)
                    if row.get("active_clock_edge") is not None
                    else row["time"]
                )
                for row in affected_rows
            )
        read_times = sorted(
            inactive_read_times
            | {
                (
                    max(0, row["active_clock_edge"] - 1)
                    if row.get("active_clock_edge") is not None
                    else row["source_time"]
                )
                for row in active_rows
            }
        ) or [e["time"]]
        # The representative may still fail while other linked checks recover; inspect all linked checks in this replay.
        supporting_entries = []
        representative_comparable = comparable
        if not (comparable and (active or suppressed)):
            related = [
                self.entries[i]
                for i in proposal["entries"]
                if i != entry
                and i in self.entries
                and self.entries[i]["test"] == e["test"]
                and self.entries[i]["backend"] == e["backend"]
            ]
            related.sort(
                key=lambda x: not any(
                    o["check"]["id"] in fixed_ids for o in x["observations"]
                )
            )
            for other in related:
                if not comparable_entry_checks(old_run, new_run, other):
                    continue
                page = self.trace_temporal(other["id"])
                prior = self.materials[page["material_id"]]
                points = sorted(
                    {
                        (row["target"], row["observed_time"])
                        for row in prior["relations"]
                        if at_anchor(row) or any(at_anchor(g) for g in row["guards"])
                    }
                )
                if not points and any(
                    o["check"]["id"] in fixed_ids for o in other["observations"]
                ):
                    points = last_anchor_divergences(
                        old_run.get("vcd", ""),
                        new_run.get("vcd", ""),
                        sorted({path[1] for path in new_paths}),
                        max(other["time"], replay_time(other)),
                    )
                related_traces = [
                    self._trace(
                        other, [other["signal"]], [replay_time(other)], variant
                    ),
                    *(self._trace(other, [s], [t], variant) for s, t in points),
                ]
                rows = [
                    row
                    for data in related_traces
                    for row in data["relations"]
                    if row["id"] in affected
                ]
                related_active = any(
                    recovery_row_active(row, new_paths) for row in rows
                )
                related_suppressed = any(
                    suppression_sources.intersection(row["control_signals"])
                    for data in related_traces
                    for row in data["suppressed"]
                    if row["relation"] in affected
                )
                if related_active or related_suppressed:
                    supporting_entries.append(other["id"])
                    active |= related_active
                    suppressed |= related_suppressed
                    comparable = True
                    boundary_materials.extend(
                        self._record("temporal_trace", data) for data in related_traces
                    )
                    witnesses = sorted(set(witnesses) | set(points))
                    witness_source = "retained_related_entry_temporal_path"
                    read_times = sorted(
                        set(read_times) | {t for _, t in points} | {other["time"]}
                    )
                    break
        continued = (
            self._trace(e, [missing_signal], read_times, variant) if added else None
        )
        continued_id = self._record("temporal_trace", continued) if continued else None
        rejection_reasons = []
        if not comparable:
            rejection_reasons.append(
                "The selected entry lacks unique, waveform-validated CHECKs with unchanged identities in both runs."
            )
        if not (active or suppressed):
            rejection_reasons.append(
                "The new dependency did not participate in the traced path for this entry."
            )
        outcome = {
            "variant": variant,
            "proposal": proposal,
            "comparisons": comparison,
            "check_identity_version": 2,
            "observation_times": {"original": e["time"], "replay": replay_time(e)},
            "ast_status": ast_status,
            "dependency_added": added,
            "entry_comparable": comparable,
            "full_run_comparable": full_run_comparable,
            "comparison_scope": (
                "retained_related_entries" if supporting_entries else "selected_entry"
            ),
            "representative_entry_comparable": representative_comparable,
            "supporting_entries": supporting_entries,
            "resolved_missing_signal": missing_signal,
            "requested_missing_signal": requested_signal,
            "new_dependency_paths": sorted(new_paths, key=str),
            "operation_dependency_paths": sorted(operation_paths, key=str),
            "operation_position_version": 1,
            "removed_dependencies": sorted(before - after),
            "path_active": active,
            "path_suppressed_by_new_control": suppressed,
            "trace_material": trace_id,
            "continued_material": continued_id,
            "boundary_materials": boundary_materials,
            "boundary_witness_source": witness_source,
            "boundary_witnesses": [
                {"signal": signal, "time": time} for signal, time in witnesses
            ],
            "accepted": ast_status == "ok"
            and comparable
            and added
            and (active or suppressed),
            "rejection_reasons": rejection_reasons,
            "original_line_map": {
                str(k): v for k, v in original_lines(source, sources[file]).items()
            },
            "original_line_maps": {
                f: {
                    str(k): v
                    for k, v in original_lines(self.ctx.sources[f], sources[f]).items()
                }
                for f in dict.fromkeys(edit["file"] for edit in edits)
            },
        }
        # Keep distinct same-signal checks by original CHECK ID and observation time.
        outcome["entry_results"] = []
        for observed in self.entries.values():
            if observed["test"] != e["test"]:
                continue
            ids = [o["check"]["id"] for o in observed["observations"]]
            changes = comparison.get(observed["backend"], {})
            outcome["entry_results"].append(
                {
                    "entry": observed["id"],
                    "checks": ids,
                    **{
                        key: [check for check in ids if check in changes.get(key, [])]
                        for key in ("fixed", "remaining", "missing", "regressions")
                    },
                    "timing_changes": {
                        cid: changes["timing_changes"][cid]
                        for cid in ids
                        if cid in changes.get("timing_changes", {})
                    },
                }
            )
        self.augmentations.append(outcome)
        return outcome

    def model_bindings(self, members=None):
        """Expose query tools to the model; augmentation and replay are executed by the program."""
        string = {"type": "string"}
        integer = {"type": "integer"}
        strings = {"type": "array", "items": string}
        integers = {"type": "array", "items": integer}
        page = {
            "offset": {
                **integer,
                "description": "从 0 开始的分页偏移；续页使用上次返回的 next_offset，默认 0。",
            },
            "limit": {
                **integer,
                "description": "本页条目数，默认 80；其余条目通过分页读取。",
            },
        }
        variant = {
            "variant": {
                **string,
                "description": "默认 original；查询增补副本时使用重放返回的 variant ID 及对应 test/entry。",
            }
        }
        definitions = [
            (
                "read_source",
                "读取指定源码行及其原始行号映射。",
                {"file": string, "start": integer, "end": integer, **variant},
                ["file", "start", "end"],
                self.read_source,
            ),
            (
                "query_ast",
                "构建实际编译器 AST 并返回上游静态依赖；静态依赖不代表运行时已激活。",
                {"test": string, "signals": strings, **page, **variant},
                ["test", "signals"],
                self.query_ast,
            ),
            (
                "query_waveform",
                "按 VCD 原生 tick 读取值与此前跳变；相关信号和时刻应批量查询。samples[signal] 含 time/value 和 history_range=[start,end)，对应 histories[signal][start:end] 中的 [tick,bits]。",
                {
                    "test": string,
                    "signals": strings,
                    "times": integers,
                    "history": integer,
                    **variant,
                },
                ["test", "signals", "times"],
                self.query_waveform,
            ),
            (
                "trace_temporal",
                "依据波形沿赋值及控制条件回溯失配，包含先前时钟沿。statements 保存源码与守卫，items 通过 event.statement 引用。target_bits 对应 observed_time，input_bits 对应 source_time；changed_at_edge 表示同 tick 输入变化，采样先后尚不确定。",
                {
                    "entry": string,
                    "signals": strings,
                    "times": integers,
                    **page,
                    **variant,
                },
                ["entry"],
                self.trace_temporal,
            ),
            (
                "read_material",
                "读取已存材料字段。diagnostic_pages 给出大字段的 total 和 next_offset，续页沿用 material_id。时序事件 items 通过键引用 statements；relations/signals 等列表按条目分页，samples 等字典按排序后的键分页。",
                {"material_id": string, "field": string, **page},
                ["material_id"],
                self.read_material,
            ),
        ]
        descriptions = {
            "test": "使用 entries[].test 中的测试 ID，不使用入口 ID 或材料 ID。",
            "entry": "使用 entries[].id，例如 E1；对应测试和失配时刻已登记。",
            "file": "使用 rtl 输入或 AST 返回的原始源码文件路径。",
            "start": "选定副本中的起始行，从 1 开始，包含该行。",
            "end": "选定副本中的结束行，从 1 开始，包含该行。",
            "signals": "使用入口、AST 或信号目录中的精确层次名称；trace_temporal 省略时从入口信号开始。",
            "times": "非负整数 VCD 原生 tick，先核对 timescale；trace_temporal 省略时使用入口时刻。",
            "contains": "可选信号名称子串过滤，默认空。",
            "history": "每个采样点此前的跳变数量，默认 8；0 表示全部此前跳变。",
            "material_id": "工具返回或输入中已有的 M_ 材料 ID。",
            "field": "已存列表或字典字段，默认 relations；原 CHECK 用 observations，源码/TB 用 lines，波形用 samples，名称用 signals，未激活分支用 suppressed。",
            "line": "原始锚点行，从 1 开始，必须位于 old 文本替换范围内。",
            "old": "不含行号前缀的精确原文，与原始锚点行共同确定修改位置。",
            "new": "完整替换 old 范围的文本；每次从原始 RTL 开始，不累积修改。",
            "missing_signal": "锚点处缺失依赖的现有信号精确层次名，修改前先确认名称。",
            "reason": "支持该缺失依赖判断的 Spec 需求及实际观测条件。",
        }
        for _, _, properties, _, _ in definitions:
            for key, schema in list(properties.items()):
                if key in descriptions:
                    properties[key] = {**schema, "description": descriptions[key]}
        bindings = {
            name: {"schema": _schema(name, desc, props, required), "call": call}
            for name, desc, props, required, call in definitions
        }
        return bindings
