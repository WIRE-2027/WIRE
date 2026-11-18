"""Public input loading, source mapping and stage output validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib, json, os, re


class ContractError(ValueError):
    """Missing fields or ambiguous references in model output."""


def json_value(value):
    if not isinstance(value, str):
        return deepcopy(value)
    text = value.strip()
    match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.S | re.I)
    if match:
        text = match.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ContractError(f"Reply is not readable JSON: {error.msg}") from error


def _id(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError("Requirement ID must be a nonempty string")
    return value.strip()


def _ids(values, field):
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise ContractError(f"{field} must contain requirement IDs")
    return list(dict.fromkeys(_id(value) for value in values))


def _requirement(value, key=None):
    if not isinstance(value, dict):
        raise ContractError("Each requirement must be an object")
    result = deepcopy(value)
    result["id"] = _id(result.get("id", key))
    if key is not None and result["id"] != key:
        raise ContractError(
            f"Requirement key {key} disagrees with its id {result['id']}"
        )
    passage = result.get("requirement")
    if isinstance(passage, str):
        passage = [passage]
    if (
        not isinstance(passage, list)
        or not passage
        or not all(isinstance(part, str) and part.strip() for part in passage)
    ):
        raise ContractError(f"{result['id']} needs its specification passage")
    result["requirement"] = passage
    # Retain requirements whose source locations or trigger suggestions are unresolved.
    result.setdefault("loc", {})
    for field in ("trigger", "expected", "reason"):
        result.setdefault(field, "")
        if not isinstance(result[field], str):
            raise ContractError(f"{result['id']}.{field} must be text")
    return result


def normalize_requirements(value):
    value = json_value(value)
    if isinstance(value, dict) and "requirements" in value:
        value = value["requirements"]
    if not isinstance(value, list):
        raise ContractError("Requirements must be an array")
    by_id = {}
    for row in value:
        row = _requirement(row)
        if row["id"] in by_id and by_id[row["id"]] != row:
            raise ContractError(f"Conflicting requirements share ID {row['id']}")
        by_id[row["id"]] = row
    return list(by_id.values())


def normalize_review(value, requirements):
    """Validate requirement IDs and audit operations."""
    value = json_value(value)
    if not isinstance(value, dict) or "keep" not in value:
        raise ContractError("Review must contain update, add, and keep")
    existing = {row["id"]: row for row in normalize_requirements(requirements)}
    updates = value.get("update", {})
    if updates is None:
        updates = {}
    if not isinstance(updates, dict):
        raise ContractError("update must be an object keyed by requirement ID")
    normalized_updates = {}
    for key, row in updates.items():
        key = _id(key)
        if key not in existing:
            raise ContractError(
                f"update refers to unknown requirement {key}; new requirements belong in add"
            )
        if row is None and key not in _ids(value["keep"], "keep"):
            continue
        normalized_updates[key] = _requirement(row, key)
    additions = normalize_requirements(value.get("add") or [])
    overlap = set(existing).intersection(row["id"] for row in additions)
    if overlap:
        raise ContractError(f"add reuses existing IDs: {sorted(overlap)}")
    deletions = value.get("delete") or {}
    if not isinstance(deletions, dict):
        raise ContractError("delete must map existing requirement IDs to observations text")
    deletions = {_id(key): _id(reason) for key, reason in deletions.items()}
    unknown_deletions = set(deletions) - set(existing)
    if unknown_deletions:
        raise ContractError(
            f"delete refers to unknown requirements: {sorted(unknown_deletions)}"
        )
    # Explicit deletion takes precedence over keep.
    keep = [key for key in _ids(value["keep"], "keep") if key not in deletions]
    unknown = set(keep) - set(existing) - {row["id"] for row in additions}
    if unknown:
        raise ContractError(f"keep refers to unknown requirements: {sorted(unknown)}")
    return {
        "update": normalized_updates,
        "add": additions,
        "delete": deletions,
        "keep": keep,
    }


def apply_review(requirements, review):
    """Apply updates and additions, then return the final keep order."""
    requirements = normalize_requirements(requirements)
    review = normalize_review(review, requirements)
    by_id = {row["id"]: row for row in requirements}
    by_id.update(review["update"])
    by_id.update((row["id"], row) for row in review["add"])
    return [deepcopy(by_id[key]) for key in review["keep"]]


# Every requirement needs a group; different test conditions may require several groups.
def normalize_groups(value, requirements):
    value = json_value(value)
    if isinstance(value, dict) and "groups" in value:
        value = value["groups"]
    if not isinstance(value, list):
        raise ContractError("Groups must be an array")
    expected = {row["id"] for row in requirements}
    assigned, groups = set(), []
    for row in value:
        if not isinstance(row, dict):
            raise ContractError("Each group must contain requirements and plan")
        ids = _ids(row.get("requirements"), "group.requirements")
        if not ids:
            raise ContractError("A group needs at least one requirement")
        unknown = set(ids) - expected
        if unknown:
            raise ContractError(
                f"Group refers to unknown requirements: {sorted(unknown)}"
            )
        plan = row.get("plan")
        if not isinstance(plan, str):
            raise ContractError("Each group needs a text plan")
        assigned.update(ids)
        group = {"requirements": ids, "plan": plan}
        if "defines" in row:
            defines = row["defines"]
            if not isinstance(defines, list) or any(
                not isinstance(v, str) or not v for v in defines
            ):
                raise ContractError(
                    "group.defines must be an array of macro names or NAME=value strings"
                )
            group["defines"] = list(dict.fromkeys(defines))
        groups.append(group)
    if expected - assigned:
        raise ContractError(
            f"Requirements not assigned to a group: {sorted(expected - assigned)}"
        )
    return groups


def normalize_tb(value):
    value = json_value(value)
    if not isinstance(value, dict):
        raise ContractError("TB reply must contain top and code")
    for key in ("top", "code"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ContractError(f"TB reply needs nonempty {key}")
    result = {"top": value["top"].strip(), "code": value["code"]}
    if "conditions" in value:
        result["conditions"] = [
            {
                "requirement": row["requirement"],
                "condition": row["condition"],
                "checks": [c for c in _items(row.get("checks")) if isinstance(c, str)],
                **(
                    {"blocked": row["blocked"].strip()}
                    if isinstance(row.get("blocked"), str)
                    and row["blocked"].strip()
                    and not row.get("checks")
                    else {}
                ),
            }
            for row in _items(value["conditions"])
            if isinstance(row, dict)
            and isinstance(row.get("requirement"), str)
            and isinstance(row.get("condition"), str)
            and row["condition"].strip()
        ]
    return result


def declared_modules(source):
    clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
    return set(
        re.findall(r'\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_$][\w$]*)', clean)
    )


def provided_modules(sources, build):
    files = build.get("compile_files") or [
        name for name in sources if name.endswith((".sv", ".v"))
    ]
    return sorted(
        {name for file in files for name in declared_modules(sources.get(file, ""))}
    )


def provided_constants(sources, build):
    result = {}
    for file in build.get("compile_files") or sources:
        code = re.sub(r'/\*.*?\*/|//[^\n]*', '', sources.get(file, ''), flags=re.S)
        package = re.search(r'\bpackage\s+([A-Za-z_$][\w$]*)\s*;', code)
        if not package:
            continue
        constants = dict(
            re.findall(
                r'\b(?:parameter|localparam)\b[^;=]*?\b([A-Za-z_$][\w$]*_(?:OFFSET|SIZE))\s*=\s*([^;]+);',
                code,
            )
        )
        if constants:
            result[file] = {
                "package": package[1],
                "constants": {name: value.strip() for name, value in constants.items()},
            }
    return result


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.run.tmp")
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write('\n')
        if path.parent.name == 'calls':
            stream.flush()
            os.fsync(stream.fileno())
    temporary.replace(path)
    return value


def inside(root, name):
    path = (Path(root) / name).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError(f"Path escapes workspace: {name}")
    return path


def freeze(manifest, destination):
    """Build model input from explicitly supplied public files."""
    manifest = Path(manifest).resolve()
    config = json.loads(manifest.read_text())
    root = (manifest.parent / config.pop("root", ".")).resolve()
    if "specs" in config:
        if "spec" in config:
            raise ValueError("Use either spec or per-target specs in one case")
        records = config.pop("specs")
        targets = config.get("targets") or [config["target"]]
        if (
            not isinstance(records, list)
            or len(records) != len(targets)
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("file"), str)
                or (
                    isinstance(row.get("path"), str) == isinstance(row.get("spec"), str)
                )
                for row in records
            )
            or {row["file"] for row in records} != set(targets)
        ):
            raise ValueError("Per-target specs must name every target exactly once")
        specs = {
            row["file"]: (
                inside(root, row["path"]).read_text() if "path" in row else row["spec"]
            )
            for row in records
        }
        spec = "\n\n".join(
            f"Target file: {file}\n{paragraph}"
            for file, text in specs.items()
            for paragraph in text.split("\n\n")
        )
    else:
        specs = None
        spec = inside(root, config.pop("spec")).read_text()
    sources = {name: inside(root, name).read_text() for name in config.pop("files")}
    target = config["target"]
    if target not in sources:
        raise ValueError("target must be included in files")
    if any(name not in sources for name in config.get("targets", [target])):
        raise ValueError("All targets must be included in files")
    if "model_files" in config:
        visible = set(config["model_files"])
        if not set(config.get("targets") or [target]) <= visible or not visible <= set(
            sources
        ):
            raise ValueError(
                "model_files must include every target and refer only to declared files"
            )
    value = {"spec": spec, "sources": sources, "build": config}
    if specs is not None:
        value["specs"] = specs
    value["id"] = digest(value)
    path = Path(destination) / "input.json"
    if path.exists() and json.loads(path.read_text()) != value:
        raise ValueError("Frozen input differs; use a new run directory")
    save(path, value)
    return value


def patched(sources, edits):
    result = dict(sources)
    for edit in edits:
        name, old, new = edit["file"], edit["old"], edit["new"]
        if name not in result:
            raise ValueError(
                f"Unknown source file {name!r}; use exactly one of: {list(result)}"
            )
        offset = edit_offset(result[name], old, edit.get("line"))
        result[name] = result[name][:offset] + new + result[name][offset + len(old) :]
    return result


def location_ranges(rows):
    if isinstance(rows, int):
        rows = [[rows]]
    elif isinstance(rows, list) and rows and all(isinstance(x, int) for x in rows):
        rows = [rows]
    return [
        (r[0], r[-1])
        for r in rows
        if isinstance(r, list)
        and len(r) in (1, 2)
        and all(isinstance(n, int) for n in r)
        and 1 <= r[0] <= r[-1]
    ]


def original_lines(original, modified):
    """Map unchanged lines exactly and edited lines to their original anchors."""
    from difflib import SequenceMatcher

    mapping = {}
    for tag, a, b, c, d in SequenceMatcher(
        None, original.splitlines(), modified.splitlines(), autojunk=False
    ).get_opcodes():
        for offset in range(d - c):
            mapping[c + offset + 1] = (
                a + offset + 1
                if tag == "equal"
                else min(a + 1, len(original.splitlines()))
            )
    return mapping


def build_for_tb(build, tb):
    """Use the same Verilator configuration for simulation and AST extraction.

    An omitted defines list inherits the case settings; an empty list clears them.
    Instance parameters remain in the testbench."""
    result = {
        key: value
        for key, value in build.items()
        if key not in {'simulators', 'simulator', 'iverilog_flags'}
    }
    if 'defines' not in tb:
        return result
    result['defines'] = list(tb['defines'])
    # Extract legacy -D flags before applying group-specific macros.
    flags, clean = iter(build.get('verilator_flags', [])), []
    for flag in flags:
        if flag in ('-CFLAGS', '-LDFLAGS'):
            clean.extend((flag, next(flags)))
        elif flag in ('-D', '-U'):
            next(flags, None)
        elif not flag.startswith(('-D', '-U', '+define+')):
            clean.append(flag)
    result['verilator_flags'] = clean
    return result


def load_input(output, task_id=None, case=None, frozen_input=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if task_id:
        benchmark = Path(__file__).resolve().parents[1] / 'Benchmark'
        legacy_id = re.fullmatch(r'[^/]+__([^/]+)__pr-(\d+)', task_id)
        relative = (
            f'{legacy_id[1]}/pr-{legacy_id[2]}' if legacy_id else task_id
        )
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/pr-\d+', relative):
            raise ContractError('Task ID must be <repo>/pr-<number>')
        task_dir = inside(benchmark, relative) / 'inputs'
        task = json.loads((task_dir / 'task.json').read_text())['input']
        targets = task['rtl_files']
        sources = {
            name: inside(task_dir / 'repo', name).read_text() for name in targets
        }
        specs = {row['file']: row['spec'] for row in task['specs']}
        if set(specs) != set(targets):
            raise ContractError('公开 Spec 与目标文件不对应')
        # Target-only inputs support static analysis; full simulation dependencies come from --case.
        frozen = {
            'spec': '\n\n'.join(
                f'Target file: {name}\n{specs[name]}' for name in targets
            ),
            'specs': specs,
            'sources': sources,
            'build': {'target': targets[0], 'targets': targets, 'model_files': targets},
        }
        frozen['id'] = digest(frozen)
        existing = output / 'input.json'
        if existing.exists() and json.loads(existing.read_text()) != frozen:
            raise ContractError('输出目录已绑定其他输入')
        save(existing, frozen)
    elif case:
        frozen = freeze(case, output)
    else:
        path = frozen_input or output / 'input.json'
        if not path.exists():
            raise ContractError('首次运行请提供 --case、--frozen-input 或 --task-id')
        frozen = json.loads(path.read_text())
        if not {'id', 'spec', 'sources', 'build'} <= frozen.keys():
            raise ContractError('冻结输入缺少 id/spec/sources/build')
        # Only public input fields enter the model context.
        frozen = {
            k: frozen[k]
            for k in ('id', 'spec', 'sources', 'build', 'specs')
            if k in frozen
        }
        existing = output / 'input.json'
        if existing.exists() and json.loads(existing.read_text()) != frozen:
            raise ContractError('输出目录已绑定其他输入')
        save(existing, frozen)
    return frozen


def observations(result, requirement_ids):
    """Associate valid checks with requirement IDs; both passes and mismatches count as observed."""
    observed, invalid, accepted = set(), [], {}
    for backend, run in result.get("runs", {}).items():
        usable_run = run.get("status") == "ok" or run.get("observations_valid") is True
        checks = run.get("valid_checks", run.get("checks", []) if usable_run else [])
        accepted[backend] = []
        for check in checks:
            if not isinstance(check, dict) or not all(
                key in check
                for key in ("id", "requirement", "signal", "time", "expected", "actual")
            ):
                invalid.append(
                    f"{backend}: CHECK is missing required observation fields"
                )
                continue
            requirement = check["requirement"]
            if requirement not in requirement_ids:
                invalid.append(
                    f"{backend}: CHECK {check['id']} names unknown requirement {requirement}"
                )
                continue
            if (
                check.get("valid") is False
                or check.get("expected_valid", check.get("expected_known")) is False
            ):
                invalid.append(
                    f"{backend}: CHECK {check['id']} has no usable expected value"
                )
                continue
            observed.add(requirement)
            accepted[backend].append(check)
    return observed, invalid, accepted


def tb_feedback(result, pending, notes=()):
    """Summarize execution diagnostics and requirements without valid checks for testbench revision."""
    sections = []
    for backend, run in result.get("runs", {}).items():
        checks = run.get("checks", [])
        known_ids = {c.get("id") for c in checks if isinstance(c, dict) and c.get("id")}
        log_lines = []
        for line in run.get("log", "").splitlines():
            # Keep malformed CHECK lines as diagnostics; remove only parsed structured records.
            if line.strip().startswith("CHECK "):
                try:
                    check = json.loads(line.strip()[6:])
                    if isinstance(check, dict) and check.get("id") in known_ids:
                        continue
                except (ValueError, TypeError):
                    pass
            log_lines.append(line)
        log_lines.extend(e for e in run.get("checker_errors", []) if e not in log_lines)
        sections.append(
            f"{backend}: {run.get('status', 'unknown')}\n" + "\n".join(log_lines)
        )
        valid = run.get("valid_checks", checks if run.get("status") == "ok" else [])
        counts = {}
        for check in valid:
            row = counts.setdefault(check["requirement"], {"valid": 0, "mismatches": 0})
            row["valid"] += 1
            row["mismatches"] += int(check.get("passed") is False)
        if counts:
            sections.append(
                "Valid checks by requirement (mismatches are DUT observations): "
                + json.dumps(counts, ensure_ascii=False)
            )
        invalid = [c for c in checks if c not in valid]
        if invalid:
            sections.append(
                "Checks requiring repair: " + json.dumps(invalid, ensure_ascii=False)
            )
    if result.get("error"):
        sections.append(result["error"])
    sections.extend(notes)
    return {"log": "\n\n".join(sections), "pending": list(pending)}


def measured_test(result, accepted):
    """Retain the testbench, waveforms and valid observations for TBA."""
    measured = deepcopy(result)
    for backend, run in measured.get("runs", {}).items():
        valid_ids = {check["id"] for check in accepted[backend]}
        run["checks"] = accepted[backend]
        run["valid_checks"] = accepted[backend]
        run["mismatches"] = [
            check for check in run.get("mismatches", []) if check.get("id") in valid_ids
        ]
        run["observations_valid"] = bool(accepted[backend])
    return measured


def _items(value):
    return [] if value is None else value if isinstance(value, list) else [value]


def normalize_entries(value, entries):
    """Assign every mismatch entry to exactly one validated group."""
    groups = value.get("groups") if isinstance(value, dict) else value
    if groups is None:
        raise ValueError("Return an object with a groups array")
    groups = _items(groups)
    known = {e["id"] for e in entries}
    validated, signatures = [], set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError(
                "Each group must be an object with representative and members"
            )
        representative = group.get("representative")
        members = list(dict.fromkeys(_items(group.get("members"))))
        if (
            representative not in known
            or representative not in members
            or not set(members) <= known
        ):
            raise ValueError(
                "Each group must name a supplied representative and members including it"
            )
        signature = (representative, frozenset(members))
        if signature in signatures:
            continue
        signatures.add(signature)
        validated.append(
            {
                "representative": representative,
                "members": members,
                "reason": group.get("reason", ""),
            }
        )
    counts = {e: sum(e in g["members"] for g in validated) for e in known}
    output, used = [], set()
    for group in validated:
        if any(counts[e] > 1 for e in group["members"]):
            # Analyze observations separately until their shared upstream cause is established.
            for entry in group["members"]:
                if entry not in used:
                    output.append(
                        {
                            "representative": entry,
                            "members": [entry],
                            "reason": "Conflicting model group membership; analyze this entry independently.",
                        }
                    )
                    used.add(entry)
        else:
            output.append(group)
            used.update(group["members"])
    output.extend(
        {
            "representative": e["id"],
            "members": [e["id"]],
            "reason": "Not included in a shared analysis",
        }
        for e in entries
        if e["id"] not in used
    )
    return output


def _analysis_fields(value):
    if not isinstance(value, dict) or not set(value).intersection(
        {
            'candidates',
            'explained',
            'boundaries',
            'unresolved',
            'tb_errors',
            'superseded',
        }
    ):
        raise ContractError(
            'Return an analysis object with candidates, boundaries, explained or unresolved'
        )


def normalize_proposals(value):
    if not isinstance(value, dict) or 'proposals' not in value:
        raise ContractError(
            'Return an object with a proposals array and optional deferred array'
        )
    for field in ('proposals', 'deferred'):
        rows = value.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ContractError(f'{field} must be an array of objects')
    return value


def normalize_analysis(value, sources, members, entry_records=()):
    """Validate original-source candidate ranges and entry and record references."""
    _analysis_fields(value)
    result = {
        key: _items(value.get(key))
        for key in ("candidates", "explained", "boundaries", "unresolved")
    }

    def entry_ids(values):
        return [
            part
            for item in _items(values)
            for part in (
                re.split(r"\s*[,/]\s*", item.strip())
                if isinstance(item, str)
                else [item]
            )
        ]

    for candidate in result["candidates"]:
        if not isinstance(candidate, dict):
            raise ValueError("Each candidate must give file, lines and related entries")
        file = candidate.get("file")
        lines = candidate.get("lines", candidate.get("line", []))
        if isinstance(lines, int):
            lines = [[lines, lines]]
        elif (
            isinstance(lines, list)
            and len(lines) == 2
            and all(type(n) is int for n in lines)
        ):
            lines = [lines]
        if isinstance(lines, list):
            lines = [
                (
                    [span[0], span[0]]
                    if isinstance(span, list)
                    and len(span) == 1
                    and type(span[0]) is int
                    else span
                )
                for span in lines
            ]
        if (
            file not in sources
            or not lines
            or any(
                not isinstance(span, list)
                or len(span) != 2
                or not all(isinstance(n, int) for n in span)
                or not 1 <= span[0] <= span[1] <= len(sources[file].splitlines())
                for span in lines
            )
        ):
            raise ValueError(
                "Candidate file and line ranges must refer to the original supplied RTL"
            )
        candidate["lines"] = lines
        candidate["entries"] = entry_ids(
            candidate.get("entries", candidate.get("entry", list(members)))
        )
        # Accept legacy material aliases when resuming old outputs.
        candidate["records"] = _items(
            candidate.get(
                "records",
                candidate.get(
                    "record_ids",
                    candidate.get(
                        "materials",
                        candidate.get("material_ids", candidate.get("related_materials")),
                    ),
                ),
            )
        )
        candidate.pop("materials", None)
        candidate.pop("material_ids", None)
        candidate.pop("related_materials", None)
        if any(not isinstance(record_id, str) for record_id in candidate["records"]):
            raise ValueError("Candidate records must contain returned record IDs")
        if not set(candidate["entries"]) <= set(members):
            raise ValueError(
                "Candidate entries must belong to the supplied analysis group"
            )
    explained, explanation_notes = [], []
    for item in result["explained"]:
        if isinstance(item, dict):
            ids = item.get("entries", item.get("entry", item.get("id")))
            if ids is None:
                raise ValueError("explained objects must name entries or entry")
            explained.extend(entry_ids(ids))
        elif isinstance(item, str):
            named = re.match(r"^(E\d+(?:\s*[-/,]\s*E\d+)*)(?=$|\s|:|'s)", item.strip())
            if named:
                label = named.group(1)
                for part in re.split(r"\s*[,/]\s*", label):
                    span = re.fullmatch(r"E(\d+)\s*-\s*E(\d+)", part)
                    if span:
                        first, last = map(int, span.groups())
                        if not 0 <= last - first < len(members):
                            raise ValueError(
                                "explained range must contain supplied entry IDs"
                            )
                        explained.extend(f"E{i}" for i in range(first, last + 1))
                    else:
                        explained.append(part)
                if item.strip() != label:
                    explanation_notes.append(item)
            else:
                explanation_notes.append(item)
        else:
            explained.append(item)
    if explanation_notes:
        result["explanation_notes"] = explanation_notes
    if any(not isinstance(entry, str) or entry not in members for entry in explained):
        raise ValueError("explained must contain supplied entry IDs")
    result["explained"] = list(dict.fromkeys(explained))
    boundaries = []
    for boundary in result["boundaries"]:
        if not isinstance(boundary, dict):
            raise ValueError(
                "Each boundary must be an object with entry and missing dependency"
            )
        if "line" not in boundary and (
            spans := location_ranges(boundary.get("lines", []))
        ):
            boundary = {**boundary, "line": spans[0][0]}
        if (
            boundary.get("file") not in sources
            or not isinstance(boundary.get("line"), int)
            or not 1 <= boundary["line"] <= len(sources[boundary["file"]].splitlines())
        ):
            raise ValueError(
                "Boundary must give an original file and explicit line or lines"
            )
        # Expand a missing dependency to every explicitly linked entry.
        ids = entry_ids(boundary.get("entry", boundary.get("entries")))
        if (
            not ids
            or not isinstance(ids, list)
            or any(entry not in members for entry in ids)
        ):
            raise ValueError("An augmentation boundary must refer to a supplied entry")
        boundaries.extend({**boundary, "entry": entry} for entry in dict.fromkeys(ids))
    result["boundaries"] = boundaries
    for index, item in enumerate(result["unresolved"]):
        if isinstance(item, str):
            item = {"entries": list(members), "reason": item}
            result["unresolved"][index] = item
        if not isinstance(item, dict):
            raise ValueError("Each unresolved item must give entries and reason")
        item["entries"] = entry_ids(
            item.get("entries", item.get("entry", list(members)))
        )
        if "reason" not in item and "question" in item:
            item["reason"] = item["question"]
        if not set(item["entries"]) <= set(members):
            raise ValueError(
                "Unresolved entries must belong to the supplied analysis group"
            )
    if value.get("tb_errors"):
        result["tb_errors"] = []
        records = (
            entry_records.values() if isinstance(entry_records, dict) else entry_records
        )
        records = [e for e in records if e["id"] in members]
        for item in _items(value["tb_errors"]):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("reason"), str)
                or not item["reason"].strip()
            ):
                continue
            ids = [
                e
                for e in entry_ids(item.get("entries", item.get("entry")))
                if isinstance(e, str) and e in members
            ]
            if not ids and not item.get("entries", item.get("entry")):
                tests = {
                    t.strip()
                    for raw in _items(item.get("test"))
                    if isinstance(raw, str)
                    for t in raw.split(",")
                }
                checks = set(
                    c for c in _items(item.get("checks")) if isinstance(c, str)
                )
                ids = [
                    e["id"]
                    for e in records
                    if e["test"] in tests
                    and checks.intersection(
                        e.get(
                            "checks",
                            [o["check"]["id"] for o in e.get("observations", [])],
                        )
                    )
                ]
            reason = item["reason"]
            if isinstance(item.get("correction"), str) and item["correction"].strip():
                reason += "\nCorrection: " + item["correction"].strip()
            if ids:
                result["tb_errors"].append(
                    {"entries": list(dict.fromkeys(ids)), "reason": reason}
                )
                result["explained"] = list(dict.fromkeys([*result["explained"], *ids]))
            else:
                result["unresolved"].append(
                    {"entries": list(members), "reason": reason}
                )
        invalid = {e for item in result["tb_errors"] for e in item["entries"]}
        result["boundaries"] = [
            b for b in result["boundaries"] if b["entry"] not in invalid
        ]
    # Explanation text does not change structured entry status.
    if isinstance(value.get("notes"), str):
        result["notes"] = value["notes"]
    return result


def entry_input(entry, material_id):
    """Keep entry summaries and reference full observations by material ID."""
    wave_checks = [o["check"].get("waveform_valid") for o in entry["observations"]]
    waveform_valid = (
        False
        if False in wave_checks
        else (True if all(v is True for v in wave_checks) else None)
    )
    return {
        **{
            key: value
            for key, value in entry.items()
            if key not in {"observations", "backend", "run_status", "cycle", "bits"}
        },
        "checks": list(dict.fromkeys(o["check"]["id"] for o in entry["observations"])),
        "waveform_valid": waveform_valid,
        "material_id": material_id,
    }


def edit_offset(source, old, line=None):
    """Locate an exact replacement using original lines and source text."""
    offsets, start = [], 0
    while old and (offset := source.find(old, start)) >= 0:
        first = source[:offset].count("\n") + 1
        if line is None or first <= int(line) <= first + old.count("\n"):
            offsets.append(offset)
        start = offset + 1
    if len(offsets) != 1:
        raise ValueError("Edit must match once at its supplied original anchor")
    return offsets[0]


def original_groups(payload):
    return [payload['group']]


def normalize_group_analysis(value, sources, payload):
    """Expand explicit group references before validating candidate locations and entries."""
    _analysis_fields(value)
    aliases = {
        g['id']: g['members'] for g in [payload['group'], *original_groups(payload)]
    }
    if isinstance(value, dict):
        rows = value.get('explained') or []
        rows = rows if isinstance(rows, list) else [rows]
        explained = []
        for row in rows:
            label = row.get('entry') if isinstance(row, dict) else row
            if isinstance(label, str) and label in aliases:
                explained.extend(aliases[label])
            else:
                explained.append(row)
        value = {**value, 'explained': explained}
    return normalize_analysis(
        value, sources, [e['id'] for e in payload['entries']], payload['entries']
    )
