"""Resolve interfaces, dependency context and original source locations."""

from __future__ import annotations

from difflib import SequenceMatcher
import hashlib, re


class CandidateAssignmentRanges:
    """Resolve candidate anchors to complete original assignments."""

    def __init__(self, sources):
        self.sources = sources

    def _file(self, file):
        locator = SourceLocator({file: self.sources[file]})
        spans = {
            (row["line"], row["line_end"])
            for module in locator.modules
            for row in locator.assignments(module)
        }
        return locator.clean[file].splitlines(), sorted(spans)

    def normalize(self, file, lines):
        """Expand unambiguous single-line anchors; preserve explicit ranges and ambiguous locations."""
        if file not in self.sources:
            return [list(span) for span in lines]
        output = []
        for span in lines:
            current = list(span)
            if (
                len(span) in (1, 2)
                and type(span[0]) is int
                and span[0] == span[-1]
                and span[0] > 0
            ):
                point = span[0]
                clean, assignments = self._file(file)
                text = clean[point - 1].strip() if point <= len(clean) else ""
                # Preserve ambiguity when a line mixes control and assignments without column locations.
                structural = re.match(
                    r"^(?:(?:else\s+)?if\b|(?:unique\s+|priority\s+)?case[zx]?\b|"
                    r"for\b|while\b|repeat\b|always(?:_comb|_ff|_latch)?\b|"
                    r"end(?:case|function|task|generate|module|interface|package)?\b|"
                    r"join(?:_any|_none)?\b)",
                    text,
                )
                matches = {(a, b) for a, b in assignments if a <= point <= b}
                if text and not structural and len(matches) == 1:
                    current = list(matches.pop())
            if current not in output:
                output.append(current)
        return output


def _clean(text):
    """Replace comments with whitespace while preserving offsets and line numbers."""
    return re.sub(
        r"/\*.*?\*/|//[^\n]*",
        lambda m: "".join("\n" if c == "\n" else " " for c in m[0]),
        text,
        flags=re.S,
    )


def _balanced(text, start):
    depth = 0
    quote = False
    escape = False
    for end in range(start, len(text)):
        c = text[end]
        if quote:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                quote = False
        elif c == '"':
            quote = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return end
    return None


def _base(signal):
    return re.sub(r"\[[^\]]*\]", "", signal).strip()


def _overlaps(left, right):
    if _base(left) != _base(right):
        return False
    a = re.findall(r"\[([^\]]*)\]", left)
    b = re.findall(r"\[([^\]]*)\]", right)
    for aa, bb in zip(a, b):
        aa = re.fullmatch(r"\s*(\d+)\s*(?::\s*(\d+)\s*)?", aa)
        bb = re.fullmatch(r"\s*(\d+)\s*(?::\s*(\d+)\s*)?", bb)
        if aa is None or bb is None:
            continue
        al, ah = sorted((int(aa[1]), int(aa[2] or aa[1])))
        bl, bh = sorted((int(bb[1]), int(bb[2] or bb[1])))
        if ah < bl or bh < al:
            return False
    return True


def _member_suffix(container, signal):
    container = re.sub(r"\s+", "", container)
    signal = re.sub(r"\s+", "", signal)
    if signal.startswith(container + ".") or signal.startswith(container + "["):
        return signal[len(container) :]
    if container.startswith(signal + ".") or container.startswith(signal + "["):
        return ""
    return "" if _overlaps(container, signal) else None


def _mask_conditions(text):
    """Treat <= as a relational operator inside control conditions."""
    spans = []
    for m in re.finditer(
        r"\b(?:if|while|for|repeat|case|casex|casez|assert|assume|cover)\s*(?:property\s*)?\(",
        text,
    ):
        start = m.end() - 1
        end = _balanced(text, start)
        if end is not None:
            spans.append((start, end + 1))
    chars = list(text)
    for start, end in spans:
        chars[start:end] = ["\n" if c == "\n" else " " for c in text[start:end]]
    masked = "".join(chars)
    return re.sub(
        r'"(?:\\.|[^"\\])*"',
        lambda m: "".join("\n" if c == "\n" else " " for c in m[0]),
        masked,
    )


class SourceLocator:
    """Trace RTL drivers through assignments and instance ports, retaining ambiguous matches."""

    def __init__(self, sources, tb=None):
        self.sources = dict(sources)
        self.tb = tb or {}
        self.texts = {**self.sources, "__wire_tb.sv": self.tb.get("code", "")}
        self.clean = {p: _clean(t) for p, t in self.texts.items()}
        self.writes = {p: _mask_conditions(t) for p, t in self.clean.items()}
        self.modules = {}
        for file, text in self.clean.items():
            for m in re.finditer(r"\bmodule\s+(?:automatic\s+)?([\w$]+)\b", text):
                end = text.find("endmodule", m.end())
                self.modules.setdefault(m[1], []).append(
                    (file, m.start(), len(text) if end < 0 else end)
                )
        names = sorted(self.modules, key=len, reverse=True)
        self.module_pattern = (
            re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
            if names
            else None
        )

    def _module(self, name):
        choices = self.modules.get(name, [])
        if len(choices) == 1:
            return choices[0]
        if choices:
            # Identical module bodies can occur in several supplied files; retain each source candidate.
            bodies = {self.texts[f][a:b] for f, a, b in choices}
            if len(bodies) == 1:
                return choices[0]
        return None

    def instances(self, module):
        out = {}
        record = self._module(module)
        if not record or not self.module_pattern:
            return out
        file, start, end = record
        text = self.clean[file]
        for m in self.module_pattern.finditer(text, start, end):
            pos = m.end()
            while pos < end and text[pos].isspace():
                pos += 1
            if pos < end and text[pos] == "#":
                pos += 1
                while pos < end and text[pos].isspace():
                    pos += 1
                if pos >= end or text[pos] != "(":
                    continue
                close = _balanced(text, pos)
                if close is None:
                    continue
                pos = close + 1
            match = re.match(r"\s*([\w$]+)\s*(?:\[[^\]]*\]\s*)?\(", text[pos:end])
            if not match:
                continue
            instance = match[1]
            port_start = pos + match.end() - 1
            port_end = _balanced(text, port_start)
            if port_end is None:
                continue
            ports = {}
            for pm in re.finditer(r"\.([\w$]+)\s*\(", text[port_start : port_end + 1]):
                begin = port_start + pm.end() - 1
                finish = _balanced(text, begin)
                if finish is not None:
                    ports[pm[1]] = text[begin + 1 : finish].strip() or pm[1]
            # Shorthand .port connections have no parentheses.
            for pm in re.finditer(
                r"\.([\w$]+)\s*(?=,|\))", text[port_start : port_end + 1]
            ):
                ports.setdefault(pm[1], pm[1])
            if re.search(r"\.\s*\*", text[port_start : port_end + 1]):
                for port in self.port_names(m[1]):
                    if re.search(r"\b" + re.escape(port) + r"\b", text[start:end]):
                        ports.setdefault(port, port)
            out.setdefault(instance, []).append({"module": m[1], "ports": ports})
        return out

    def port_names(self, module):
        record = self._module(module)
        if not record:
            return []
        file, start, end = record
        text = self.clean[file][start:end]
        header_end = re.search(r"\)\s*;", text)
        header = text[: header_end.end()] if header_end else text
        names = []
        for direction in re.finditer(
            r"\b(?:input|output|inout)\b(.*?)(?=\b(?:input|output|inout)\b|$)",
            header,
            re.S,
        ):
            for declaration in direction[1].split(","):
                declaration = re.sub(r"\[[^\]]*\]", " ", declaration.split("=")[0])
                tokens = re.findall(r"[\w$]+", declaration)
                if tokens:
                    names.append(tokens[-1])
        return names

    def blocks(self, module):
        record = self._module(module)
        if not record:
            return {}
        file, start, end = record
        stack = []
        blocks = {}
        for m in re.compile(r"\bbegin\b(?:\s*:\s*([\w$]+))?|\bend\b").finditer(
            self.clean[file], start, end
        ):
            if m[0].startswith("begin"):
                stack.append((m[1], m.start()))
            elif stack:
                name, begin = stack.pop()
                if name:
                    blocks.setdefault(name, []).append((begin, m.end()))
        return blocks

    def _direction(self, module, port, direction):
        record = self._module(module)
        if not record:
            return False
        file, start, end = record
        text = self.clean[file][start:end]
        header_end = re.search(r"\)\s*;", text)
        header = text[: header_end.end()] if header_end else text
        return any(
            re.search(r"\b" + re.escape(port) + r"\b", m[1])
            for m in re.finditer(
                r"\b" + direction + r"\b(.*?)(?=\b(?:input|output|inout)\b|$)",
                header,
                re.S,
            )
        )

    def _output(self, module, port):
        return self._direction(module, port, "output") or self._direction(
            module, port, "inout"
        )

    def assignments(self, module):
        """Extract assignment targets, expressions, source locations and sequential properties."""
        record = self._module(module)
        if not record:
            return []
        file, start, end = record
        pattern = re.compile(
            r"(?<![\w$.])([\w$]+(?:\s*\[[^\]\n]+\])*(?:\.[\w$]+(?:\s*\[[^\]\n]+\])*)*)\s*(<=|=(?!=))\s*([^;]+);"
        )
        rows = []
        body = self.clean[file].find(";", start, end)
        for m in pattern.finditer(
            self.writes[file], body + 1 if body >= 0 else start, end
        ):
            lhs, rhs = m[1].strip(), m[3].strip()
            # Exclude declaration defaults and loop counters unless resolving that assigned variable.
            line = self.clean[file].count("\n", 0, m.start(1)) + 1
            line_end = self.clean[file].count("\n", 0, m.end()) + 1
            rows.append(
                {
                    "lhs": lhs,
                    "rhs": rhs,
                    "line": line,
                    "line_end": line_end,
                    "file": file,
                    "offset": m.start(),
                    "sequential": m[2] == "<=",
                }
            )
        concat = re.compile(r"(?:\bassign\s+)?\{([^{};]+)\}\s*(<=|=(?!=))\s*([^;]+);")
        for m in concat.finditer(
            self.writes[file], body + 1 if body >= 0 else start, end
        ):
            for target in m[1].split(","):
                target = target.strip()
                if not re.fullmatch(
                    r"[\w$]+(?:\[[^\]]+\])*(?:\.[\w$]+(?:\[[^\]]+\])*)*", target
                ):
                    continue
                rows.append(
                    {
                        "lhs": target,
                        "rhs": m[3].strip(),
                        "line": self.clean[file].count("\n", 0, m.start()) + 1,
                        "line_end": self.clean[file].count("\n", 0, m.end()) + 1,
                        "file": file,
                        "offset": m.start(),
                        "sequential": m[2] == "<=",
                    }
                )
        return rows

    def _location(self, row, signal, role="direct_driver"):
        file = row["file"]
        text = self.texts[file]
        return {
            "file": file,
            "lines": [row["line"]],
            "line_end": row["line_end"],
            "signal": signal,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "resolution": "static_driver",
            "role": role,
            "code": "\n".join(text.splitlines()[row["line"] - 1 : row["line_end"]]),
            "assignment_target": row["lhs"],
            "assignment_expression": row["rhs"],
        }

    def function_locations(self, package, function):
        found = []
        for file, text in self.clean.items():
            for pm in re.finditer(r"\bpackage\s+" + re.escape(package) + r"\s*;", text):
                end = text.find("endpackage", pm.end())
                if end < 0:
                    continue
                pattern = re.compile(
                    r"\bfunction\b[^;]*?\b" + re.escape(function) + r"\s*\([^;]*?\)\s*;"
                )
                for fm in pattern.finditer(text, pm.end(), end):
                    finish = text.find("endfunction", fm.end(), end)
                    if finish < 0:
                        continue
                    for rm in re.compile(r"\breturn\s+([^;]+);").finditer(
                        text, fm.end(), finish
                    ):
                        row = {
                            "file": file,
                            "line": text.count("\n", 0, rm.start()) + 1,
                            "line_end": text.count("\n", 0, rm.end()) + 1,
                            "lhs": package + "::" + function,
                            "rhs": rm[1].strip(),
                        }
                        found.append(
                            self._location(
                                row, package + "::" + function, "function_return"
                            )
                        )
        return found

    def locate(self, signal):
        """Trace a testbench observation through connected ports to its RTL driver."""
        top = self.tb.get("top", "tb")
        trace, found, seen, parents = [], [], set(), {}

        def walk(module, name, scope):
            if scope == top:
                name = name.removeprefix("TOP.").removeprefix(top + ".")
            if (module, name, scope) in seen:
                return
            seen.add((module, name, scope))
            pieces = name.split(".")
            if len(pieces) > 1:
                instances = self.instances(module)
                for i, piece in enumerate(pieces[:-1]):
                    choices = instances.get(_base(piece), [])
                    if choices:
                        for child in choices:
                            child_scope = scope + "." + ".".join(pieces[: i + 1])
                            parents[child_scope] = (module, scope, child)
                            walk(
                                child["module"], ".".join(pieces[i + 1 :]), child_scope
                            )
                        return
                block = self.blocks(module).get(_base(pieces[0]), [])
                if len(block) == 1:
                    walk(module, ".".join(pieces[1:]), scope + "." + pieces[0])
                    return
            wanted = name.strip()
            rows = [r for r in self.assignments(module) if _overlaps(r["lhs"], wanted)]
            if not rows and "." in wanted:
                # Whole-structure assignments also drive individual fields.
                rows = [
                    r
                    for r in self.assignments(module)
                    if _overlaps(r["lhs"], wanted.split(".")[0])
                ]
            if not rows:
                rows = [
                    r
                    for r in self.assignments(module)
                    if r["lhs"].startswith(wanted + ".")
                ]
            for part in scope.split("."):
                spans = self.blocks(module).get(_base(part), [])
                if len(spans) == 1:
                    begin, end = spans[0]
                    rows = [r for r in rows if begin <= r["offset"] < end]
            # Follow output ports inward when a parent net has no local driver.
            if not rows or any(r["file"] == "__wire_tb.sv" for r in rows):
                for inst, children in self.instances(module).items():
                    for child in children:
                        for port, expr in child["ports"].items():
                            suffix = _member_suffix(expr, wanted)
                            if suffix is not None and self._output(
                                child["module"], port
                            ):
                                trace.append(
                                    {
                                        "kind": "port_bridge",
                                        "from_signal": scope + "." + name,
                                        "to_signal": scope
                                        + "."
                                        + inst
                                        + "."
                                        + port
                                        + suffix,
                                    }
                                )
                                parents[scope + "." + inst] = (module, scope, child)
                                walk(child["module"], port + suffix, scope + "." + inst)
            if not rows:
                root = re.match(r"[\w$]+", wanted)
                if (
                    root
                    and scope in parents
                    and self._direction(module, root[0], "input")
                ):
                    parent_module, parent_scope, child = parents[scope]
                    expression = child["ports"].get(root[0], "")
                    suffix = wanted[len(root[0]) :]
                    if re.fullmatch(
                        r"[\w$]+(?:\[[^\]]+\])*(?:\.[\w$]+(?:\[[^\]]+\])*)*", expression
                    ):
                        trace.append(
                            {
                                "kind": "input_port_bridge",
                                "from_signal": scope + "." + wanted,
                                "to_signal": parent_scope + "." + expression + suffix,
                            }
                        )
                        walk(parent_module, expression + suffix, parent_scope)
                return
            for row in rows:
                qualified = scope + "." + name
                if row["file"] != "__wire_tb.sv":
                    reference = self._module(module)
                    aliases = self.modules.get(module, [reference])
                    for file, start, _ in aliases:
                        offset = self.texts[file].count("\n", 0, start) - self.texts[
                            reference[0]
                        ].count("\n", 0, reference[1])
                        location = self._location(
                            {
                                **row,
                                "file": file,
                                "line": row["line"] + offset,
                                "line_end": row["line_end"] + offset,
                            },
                            qualified,
                        )
                        if len(aliases) > 1:
                            location["equivalent_module_source_candidates"] = len(
                                aliases
                            )
                        found.append(location)
                    continue
                trace.append(
                    {
                        **self._location(row, qualified, "TB_bridge"),
                        "kind": "TB_alias_or_capture",
                    }
                )
                rhs = row["rhs"].strip()
                # Preserve aliases and selections; resolve complex testbench expressions through actual connections.
                if re.fullmatch(
                    r"[\w$]+(?:\[[^\]]+\])*(?:\.[\w$]+(?:\[[^\]]+\])*)*", rhs
                ):
                    suffix = _member_suffix(row["lhs"], wanted)
                    walk(module, rhs + (suffix or ""), scope)
                elif rhs.startswith("{") and rhs.endswith("}") and "{" not in rhs[1:-1]:
                    components = [part.strip() for part in rhs[1:-1].split(",")]
                    reference = r"[\w$]+(?:\[[^\]]+\])*(?:\.[\w$]+(?:\[[^\]]+\])*)*"
                    literal = r"(?:\d+)?'[sS]?[bBoOdDhH]?[0-9a-fA-F_xXzZ?]+|\d+"
                    if all(
                        re.fullmatch(reference + "|" + literal, x) for x in components
                    ):
                        for component in components:
                            if re.fullmatch(literal, component):
                                continue
                            walk(module, component, scope)
                else:
                    call = re.fullmatch(r"([\w$]+)::([\w$]+)\s*\(.*\)", rhs, re.S)
                    if call:
                        found.extend(self.function_locations(call[1], call[2]))

        name = signal.removeprefix("TOP.").removeprefix(top + ".")
        walk(top, name, top)
        unique = {(x["file"], tuple(x["lines"]), x["signal"]): x for x in found}
        return list(unique.values()), trace

    def annotate(self, check):
        locations, _ = self.locate(check.get("signal", ""))
        check["actual_driver_locations"] = locations
        return check


def annotate_actual_sources(checks, sources, tb):
    """Add source-derived driver locations without changing observed values or CHECK IDs."""
    locator = SourceLocator(sources, tb)
    for check in checks:
        # Model-provided actual_source is only a hint; derive added locations from source and observed signals.
        locator.annotate(check)
    return checks


_IDENT = r"[A-Za-z_$][\w$]*"
_FIELD = re.compile(
    r"\b("
    + _IDENT
    + r"(?:\s*\[[^\]\n]+\])?(?:\s*\.\s*"
    + _IDENT
    + r"(?:\s*\[[^\]\n]+\])?)+)"
)


def _normal(value):
    return re.sub(r"\s+|\[[^\]]*\]", "", value)


def _ranges(lines):
    result = []
    for n in sorted(set(lines)):
        if result and n == result[-1][1] + 1:
            result[-1][1] = n
        else:
            result.append([n, n])
    return result


def related_dependency_context(sources, build, requirements=(), supplied_files=()):
    """Collect source context along actual target-module instance connections."""
    targets = list(
        build.get("model_files") or build.get("targets") or [build["target"]]
    )
    supplied = set(supplied_files or targets)
    locator = SourceLocator(sources)
    pending = sorted(
        name
        for name, records in locator.modules.items()
        if any(file in targets for file, _, _ in records)
    )
    visited, children, missing = set(), {}, []
    while pending:
        parent = pending.pop(0)
        if parent in visited:
            continue
        visited.add(parent)
        parent_files = [file for file, _, _ in locator.modules[parent]]
        for instance, records in locator.instances(parent).items():
            for record in records:
                child = record["module"]
                module = locator._module(child)
                if module is None:
                    missing.append(
                        {
                            "parent": parent,
                            "instance": instance,
                            "module": child,
                            "reason": "No unique supplied child definition",
                        }
                    )
                    continue
                pending.append(child)
                file, _, _ = module
                if file in supplied:
                    continue
                allowed = set(locator.port_names(child))
                info = children.setdefault(file, {"modules": set(), "edges": []})
                info["modules"].add(child)
                info["edges"].append(
                    {
                        "parent_module": parent,
                        "parent_files": parent_files,
                        "instance": instance,
                        "child_module": child,
                        "ports": {
                            k: v for k, v in record["ports"].items() if k in allowed
                        },
                    }
                )
    files = []
    for file, info in sorted(children.items()):
        source = sources[file]
        lines = source.splitlines()
        files.append(
            {
                "file": file,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "modules": sorted(info["modules"]),
                "mode": "complete_file",
                "total_original_lines": len(lines),
                "shown_original_lines": len(lines),
                "excerpt": False,
                "truncated": False,
                "direct_instances": info["edges"],
                "selection": "Complete supplied source reached through actual module instances",
                "matched_references": [],
                "windows": [
                    {
                        "start_line": 1,
                        "end_line": len(lines),
                        "text": "\n".join(
                            f"{n}: {line}" for n, line in enumerate(lines, 1)
                        ),
                    }
                ],
            }
        )
    return {
        "schema_version": 1,
        "scope": "Complete supplied source of reachable child modules",
        "file_count": len(files),
        "shown_original_lines": sum(f["shown_original_lines"] for f in files),
        "truncated": False,
        "files": files,
        "omitted": [],
        "unresolved_child_definitions": missing,
    }


def provided_interface(sources, build, include_function_bodies=False):
    """Read original declarations and parameter expressions, including referenced functions."""
    visible = build.get("model_files") or build.get("targets") or [build["target"]]
    needed = set(
        re.findall(r"\b[A-Za-z_$][\w$]*\b", "\n".join(sources[p] for p in visible))
    )
    declarations = []
    files = build.get("compile_files") or sources
    if include_function_bodies:
        files = dict.fromkeys([*files, *sources])
    for file in files:
        original = sources.get(file, "")
        code = re.sub(
            r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*',
            lambda m: m[0] if m[0].startswith('"') else re.sub(r"[^\n]", " ", m[0]),
            original,
            flags=re.S,
        )
        structure = re.sub(r'"(?:\\.|[^"\\])*"', lambda m: " " * len(m[0]), code)
        for package in re.finditer(
            r"\bpackage\s+(\w+)\s*;([\s\S]*?)\bendpackage\b", code
        ):
            is_config = bool(re.search(r"config|cfg", package[1], re.I))
            consumed = package.start(2)
            for start in re.finditer(
                r"\b(typedef|localparam|parameter|function)\b", package[2]
            ):
                begin = package.start(2) + start.start()
                if begin < consumed:
                    continue
                kind = start[1]
                if kind == "function":
                    end = code.find("endfunction", begin, package.end(2))
                    if end < 0:
                        continue
                    end += len("endfunction")
                    if include_function_bodies:
                        label = re.match(r"[ \t]*:[ \t]*\w+", code[end:package.end(2)])
                        if label:
                            end += label.end()
                else:
                    depth, end = 0, begin
                    for end in range(begin, package.end(2)):
                        char = structure[end]
                        depth += (char in "({[") - (char in ")}]")
                        if char == ";" and depth == 0:
                            break
                    end += 1
                consumed = end
                text = code[begin:end]
                if kind == "function" and not include_function_bodies:
                    text = text.split(";", 1)[0] + ";"
                if kind == "typedef":
                    name = re.search(r"(\w+)\s*(?:\[[^]]*\]\s*)*;$", text)
                    symbols = {name[1]} if name else set()
                    if re.search(r"\benum\b", text) and "{" in text:
                        body = text.split("{", 1)[1].rsplit("}", 1)[0]
                        symbols.update(re.findall(r"(?:^|,)\s*(\w+)", body))
                elif kind == "function":
                    if not is_config and not include_function_bodies:
                        continue
                    name = re.search(r"(\w+)\s*\(", text)
                    symbols = {name[1]} if name else set()
                else:
                    symbols = set(re.findall(r"\b(\w+)\s*(?:\[[^]]*\]\s*)*=", text))
                declarations.append(
                    (
                        file,
                        package[1],
                        begin,
                        end,
                        symbols,
                        is_config and kind != "typedef",
                        original[begin:end] if include_function_bodies and kind == "function" else text,
                    )
                )
    selected = set()
    while True:
        additions = [
            i
            for i, (_, _, _, _, symbols, root, _) in enumerate(declarations)
            if i not in selected and (root or symbols & needed)
        ]
        if not additions:
            break
        for i in additions:
            selected.add(i)
            needed.update(re.findall(r"\b[A-Za-z_$][\w$]*\b", declarations[i][-1]))
    result = {}
    for i in sorted(selected):
        file, package, begin, end, _, _, text = declarations[i]
        first = sources[file].count("\n", 0, begin) + 1
        full_function = include_function_bodies and text.lstrip().startswith("function")
        snippet = "\n".join(
            f"{first+n}: {line if full_function else line.strip()}"
            for n, line in enumerate(text.splitlines())
            if full_function or line.strip()
        )
        result.setdefault(file, {}).setdefault(package, []).append(snippet)
    return {
        file: {package: "\n".join(parts) for package, parts in packages.items()}
        for file, packages in result.items()
    }


def operation_location(node):
    if "operation_position" in node:
        p = node["operation_position"]
        return (p["file"], p["line"]) if p else ("", None)
    return node.get("file"), node.get("original_line")


class OperationPositions:
    """Map augmented assignments and guards back to original RTL locations."""

    def __init__(self, original, modified):
        self.original, self.modified = original, modified
        # Cache each file's mapping and assignments for repeated AST location queries.
        self._file_cache = {}

    def _file(self, file):
        if file in self._file_cache:
            return self._file_cache[file]
        old, new = self.original[file], self.modified[file]
        if old == new:
            result = ({n: n for n in range(1, len(old.splitlines()) + 1)}, [], [])
            self._file_cache[file] = result
            return result
        equal = {}
        for tag, a, b, c, d in SequenceMatcher(
            None, old.splitlines(), new.splitlines(), autojunk=False
        ).get_opcodes():
            if tag == "equal":
                equal.update({c + k + 1: a + k + 1 for k in range(d - c)})

        def records(text):
            loc = SourceLocator({file: text})
            out = []
            for module in loc.modules:
                blocks = loc.blocks(module)
                for r in loc.assignments(module):
                    named = [
                        (b - a, n)
                        for n, spans in blocks.items()
                        for a, b in spans
                        if a <= r["offset"] < b
                    ]
                    start = loc.clean[file].rfind("\n", 0, r["offset"]) + 1
                    continuous = re.match(
                        r"\s*assign\b", loc.clean[file][start : r["offset"]]
                    )
                    scope = (
                        min(named)[1] if named else ("assign" if continuous else None)
                    )
                    key = (module, scope, re.sub(r"\s+", "", r["lhs"]), r["sequential"])
                    out.append((r["line"], r["line_end"], key if scope else None))
            return out

        result = (equal, records(old), records(new))
        self._file_cache[file] = result
        return result

    def position(self, file, line):
        """Resolve an augmented operation to an unambiguous original-source coordinate."""
        if file not in self.original or not isinstance(line, int):
            return None
        equal, old, new = self._file(file)
        if line in equal:
            return {"file": file, "line": equal[line]}
        matches = [r for r in new if r[0] <= line <= r[1]]
        if len(matches) != 1 or matches[0][2] is None:
            return None
        prior = {r[0] for r in old if r[2] == matches[0][2]}
        return {"file": file, "line": prior.pop()} if len(prior) == 1 else None

    def decorate(self, row):
        row = dict(row)
        origin = row.get("assignment_origin", {})
        file, line = origin.get("file", row["file"]), origin.get("line", row["line"])
        row["operation_position"] = self.position(file, line)
        guards = []
        for guard in row.get("guards", []):
            g = dict(guard)
            gf, gl = g.get("file"), g.get("line")
            exact = self._file(gf)[0].get(gl) if gf in self.original else None
            g["operation_position"] = (
                {"file": gf, "line": exact}
                if exact is not None
                else row["operation_position"] if gf == file else None
            )
            guards.append(g)
        row["guards"] = guards
        return row
