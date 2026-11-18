"""Compile and simulate RTL, validate CHECK records and retain waveforms."""

from __future__ import annotations

from collections import Counter
from ..contracts import build_for_tb, digest, inside, declared_modules, provided_modules
from copy import deepcopy
from pathlib import Path
from .source import SourceLocator, _clean, _balanced, annotate_actual_sources
from .waveform import list_signals, read_wave
import hashlib, json, re, os, shutil, signal, subprocess, tempfile


def command(argv, directory, timeout=120):
    """Run an argument vector; terminate the process group on timeout and preserve output."""
    try:
        process = subprocess.Popen(
            argv,
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as error:
        return {"code": -1, "log": str(error)}
    try:
        output, _ = process.communicate(timeout=timeout)
        return {"code": process.returncode, "log": output}
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        return {"code": -1, "log": output + "\nTIMEOUT"}


def materialize(directory, sources):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in sources.items():
        path = inside(directory, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return directory


def flags(build, backend=None):
    values = ["-I" + name for name in build.get("include_dirs", [])]
    values += ["-D" + item for item in build.get("defines", [])]
    extra = list(build.get(f"{backend}_flags", [])) if backend else []
    return values + extra


def number(value):
    """Parse numeric literals; return None for unknown or unresolved values."""
    text = str(value).strip().lower().replace("_", "")
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    match = re.fullmatch(r"(?:(\d+))?'([s]?)([hbod])([0-9a-f+-]+)", text)
    if match:
        width, signed, base, digits = match.groups()
        n = int(digits, {"h": 16, "b": 2, "o": 8, "d": 10}[base])
        if width:
            bits = int(width)
            n %= 1 << bits
            if signed and n & (1 << (bits - 1)):
                n -= 1 << bits
        return n
    if re.fullmatch(r"[+-]?0(?:x[0-9a-f]+|b[01]+|o[0-7]+)", text):
        return int(text, 0)
    return None


def value(value):
    return str(value).strip().lower()


def expected_bits(value):
    """Preserve full-width four-state expectations, including explicit X/Z bits."""
    match = re.fullmatch(
        r"([1-9]\d*)'b([01xz]+)", str(value).lower().replace("_", "").strip()
    )
    if match and len(match[2]) == int(match[1]) and any(c in match[2] for c in "xz"):
        return match[2]
    return None


def parse_checks(log, errors=None):
    """Parse CHECK records and retain malformed-line diagnostics separately."""
    errors = errors if errors is not None else []
    checks, ids = [], set()
    for line_number, line in enumerate(log.splitlines(), 1):
        if not line.lstrip().startswith("CHECK "):
            continue
        try:
            row = json.loads(line.lstrip()[6:])
            required = ("id", "requirement", "signal", "time", "expected", "actual")
            if not isinstance(row, dict) or any(k not in row for k in required):
                raise ValueError(
                    "CHECK 缺少 id/requirement/signal/time/expected/actual"
                )
            if any(
                not isinstance(row[k], str) or not row[k]
                for k in ("id", "requirement", "signal")
            ):
                raise ValueError("CHECK 的编号、需求 ID 和信号必须是非空字符串")
            tick = float(row["time"])
            if tick < 0 or not tick.is_integer():
                raise ValueError("CHECK.time 应为非负 VCD tick 整数")
            row["time"] = int(tick)
            expected, actual = number(row["expected"]), number(row["actual"])
            row.update(
                expected_known=expected is not None,
                expected_valid=expected is not None
                or expected_bits(row["expected"]) is not None,
                known=expected is not None and actual is not None,
                passed=(
                    expected == actual
                    if expected is not None and actual is not None
                    else value(row["expected"]) == value(row["actual"])
                ),
                valid=True,
            )
            if not row["expected_valid"]:
                row["valid"] = False
                row["validation_error"] = (
                    f"expected={row['expected']!r} has no recognized numeric encoding; "
                    "use decimal, 0x hex, a Verilog integer literal, or a full-width binary X/Z literal such as 4'b10xz"
                )
                errors.append(f"CHECK {row['id']}: {row['validation_error']}")
            if row["id"] in ids:
                errors.append(f'line {line_number}: 重复 CHECK ID {row["id"]}')
            ids.add(row["id"])
            checks.append(row)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            errors.append(f"line {line_number}: {error}")
    duplicates = {
        identity
        for identity, count in Counter(c["id"] for c in checks).items()
        if count > 1
    }
    for row in checks:
        if row["id"] in duplicates:
            row["valid"] = False
            row["validation_error"] = "CHECK ID is not unique"
    return checks


def _last_vcd_tick(path):
    """Find the last VCD timestamp for sampling bounds."""
    with Path(path).open("rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        suffix = b""
        while position:
            start = max(0, position - 65536)
            stream.seek(start)
            data = stream.read(position - start) + suffix
            # Skip partial lines at chunk boundaries when locating the final timestamp.
            lines = data.splitlines()
            for line in reversed(lines if start == 0 else lines[1:]):
                if re.fullmatch(rb"#[0-9]+", line.strip()):
                    return int(line.strip()[1:])
            suffix, position = data, start
    return 0


def _bits(value, width):
    value = value.lower()
    fill = value[0] if value and value[0] in "xz" else "0"
    return value.rjust(width, fill)[-width:]


def _matches_wave(actual, waveform, width):
    """Return True for a match, False for a difference, or None for an unresolved encoding."""
    if waveform is None:
        return False
    bits = _bits(waveform, width)
    numeric = number(actual)
    if numeric is not None:
        return (
            not any(c in bits for c in "xz")
            and -(1 << (width - 1)) <= numeric < (1 << width)
            and numeric % (1 << width) == int(bits, 2)
        )
    text = value(actual).replace("_", "")
    # Formatted unknown values may be a single x or z; the VCD must still contain that state.
    if text == "x":
        return "x" in bits
    if text == "z":
        return "z" in bits and "x" not in bits
    match = re.fullmatch(r"(?:(\d+))?'s?([hbo])([0-9a-fxz]+)", text)
    if match:
        size, radix, digits = match.groups()
    else:
        match = re.fullmatch(r"0([xbo])([0-9a-fxz]+)", text)
        if not match:
            return None
        radix, digits = match.groups()
        radix, size = {"x": "h", "b": "b", "o": "o"}[radix], None
    step, base = {"h": (4, 16), "b": (1, 2), "o": (3, 8)}[radix]
    try:
        parts = [
            c * step if c in "xz" else format(int(c, base), f"0{step}b") for c in digits
        ]
    except ValueError:
        return None
    supplied = "".join(parts)
    if size:
        supplied = _bits(supplied, int(size))
    return _bits(supplied, width) == bits


def validate_wave_checks(checks, path, errors):
    """Accept CHECK observations only when their actual values agree with the VCD."""
    candidates = [row for row in checks if row.get("valid")]
    if not candidates:
        return
    try:
        declaration = list_signals(path)
        if declaration.get("error"):
            raise ValueError(declaration["error"])
        widths = {row["name"]: row["width"] for row in declaration["signals"]}
        times = {}
        for row in candidates:
            times.setdefault(row["signal"], []).append(row["time"])
        wave = read_wave(path, list(times), times, 2)
        if wave.get("error"):
            raise ValueError(wave["error"])
        samples = {
            name: {row["time"]: row for row in rows}
            for name, rows in wave["samples"].items()
        }
        last_tick = _last_vcd_tick(path)
    except (OSError, ValueError, KeyError, IndexError, StopIteration) as error:
        for row in candidates:
            row["valid"], row["waveform_valid"] = False, False
            row["validation_error"] = "Waveform unavailable or unreadable: " + str(
                error
            )
        errors.append("Waveform unavailable or unreadable: " + str(error))
        return
    for row in candidates:
        reason = None
        signal_name, tick = row["signal"], row["time"]
        if signal_name not in widths:
            reason = "signal does not exist in VCD"
        elif tick > last_tick:
            reason = f"time {tick} exceeds VCD end tick {last_tick}"
        else:
            sample = samples.get(signal_name, {}).get(tick, {})
            observed = sample.get("value")
            row["waveform_value"] = observed
            matched = _matches_wave(row["actual"], observed, widths[signal_name])
            if matched is None:
                reason = (
                    f"actual={row['actual']!r} has no recognized numeric encoding; "
                    "print decimal with %0d, hex with 0x%0h, or a Verilog literal"
                )
            elif not matched:
                reason = "actual does not match VCD at the recorded tick; check the signal, units, and stable sampling"
                history = sample.get("history", [])
                if (
                    len(history) >= 2
                    and history[-1][0] == tick
                    and _matches_wave(
                        row["actual"], history[-2][1], widths[signal_name]
                    )
                ):
                    reason += (
                        "; actual matches the preceding value and the signal changes at this tick. "
                        "Sample after updates; move subsequent reset/input changes to a later tick, "
                        "including resets at the start of the next scenario"
                    )
            if (
                reason is None
                and (pattern := expected_bits(row["expected"])) is not None
            ):
                if len(pattern) != widths[signal_name]:
                    reason = (
                        "four-state expected width must equal the observed signal width"
                    )
                else:
                    # Compare all waveform bits, including mixed known and unknown values.
                    row["passed"] = pattern == _bits(observed, widths[signal_name])
        row["waveform_valid"] = reason is None
        if reason:
            row["valid"], row["validation_error"] = False, reason
            errors.append(f'CHECK {row["id"]}: {reason}')


def compile_diagnostics(log):
    """Summarize compilation errors while retaining the complete log on disk."""
    if len(log) <= 12000:
        return log
    lines = log.splitlines()
    errors = [
        "\n".join(lines[i : i + 5])
        for i, line in enumerate(lines)
        if line.startswith("%Error")
    ]
    return "\n".join(errors) if errors else log


def simulate(sources, build, tb, directory):
    """Run a testbench in a temporary build directory to support output paths containing spaces."""
    output = Path(directory).resolve()
    if not any(char.isspace() for char in str(output)):
        return _simulate(sources, build, tb, output)
    with tempfile.TemporaryDirectory(prefix="wire-build-", dir="/tmp") as temporary:
        work = Path(temporary).resolve()
        try:
            result = _simulate(sources, build, tb, work)
        finally:
            shutil.copytree(work, output, dirs_exist_ok=True)
        for run in result.get("runs", {}).values():
            if run.get("vcd"):
                run["vcd"] = str(output / Path(run["vcd"]).resolve().relative_to(work))
        return result


def _simulate(sources, build, tb, directory):
    build = build_for_tb(build, tb)

    compile_sources, build, adaptation = adapt_build(sources, build, tb, directory)
    directory = materialize(directory, compile_sources)
    filename = "__wire_tb.sv"
    if filename in sources:
        raise ValueError("生成 TB 文件名与输入冲突")
    clean = re.sub(r"/\*.*?\*/|//[^\n]*", "", tb["code"], flags=re.S)
    dut_modules = set(provided_modules(sources, build))
    tb_modules = declared_modules(tb["code"])
    if tb["top"] not in tb_modules:
        raise ValueError(f"TB 未定义指定顶层 {tb['top']}；请使 top 与 TB 模块名一致")
    duplicated = dut_modules & tb_modules
    if duplicated:
        raise ValueError(
            "TB 重复定义了工程已有模块："
            + ", ".join(sorted(duplicated))
            + "。这些模块已由构建文件提供，请删除 TB 中的重复定义，保留实例调用。"
        )
    if re.search(r"\b(?:force|release)\s+[\w$]+\.", clean):
        raise ValueError(
            "TB 请通过 DUT 输入建立状态，不使用内部 force/release 改变被测行为"
        )
    (directory / filename).write_text(tb["code"])
    files = build.get("compile_files") or [
        f for f in sources if f.endswith((".sv", ".v"))
    ]
    work = directory / "verilator"
    work.mkdir(exist_ok=True)
    compile_args = [
        "verilator",
        "--binary",
        "--timing",
        "--trace",
        "-Wno-fatal",
        "--top-module",
        tb["top"],
        "--Mdir",
        str(Path("verilator") / "obj"),
        "-o",
        "sim",
        *flags(build, "verilator"),
        *files,
        filename,
    ]
    compiled = command(
        compile_args,
        directory,
        int(os.environ.get("WIRE_COMPILE_TIMEOUT", build.get("compile_timeout", 180))),
    )
    (work / "compile.log").write_text(compiled["log"])
    if compiled["code"]:
        internal = re.search(
            r"(?m)^%Error[^\n]*\bInternal (?:Error|Fault)\b|Verilator internal fault",
            compiled["log"],
            re.I,
        )
        result = {
            "status": "toolchain_error" if internal else "compile_error",
            "log": compile_diagnostics(compiled["log"]),
            "checks": [],
            "valid_checks": [],
            "mismatches": [],
            "observations_valid": False,
        }
    else:
        for asset in build.get("assets", []):
            path = inside(work, asset)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sources[asset])
        executed = command(
            [str(work / "obj/sim")],
            work,
            int(
                os.environ.get(
                    "WIRE_SIMULATION_TIMEOUT", build.get("simulation_timeout", 30)
                )
            ),
        )
        (work / "simulation.log").write_text(executed["log"])
        errors = []
        checks = parse_checks(executed["log"], errors)
        validate_wave_checks(checks, work / "wave.vcd", errors)
        annotate_actual_sources(checks, sources, tb)
        valid = [c for c in checks if c["expected_valid"] and c["valid"]]
        # TEST_DONE must start a line, and the process must also exit normally.
        finished = executed["code"] == 0 and bool(
            re.search(r"(?m)^[ \t]*TEST_DONE(?=$|[ \t\r])", executed["log"])
        )
        status = (
            "ok"
            if finished and checks and not errors
            else "invalid_checker" if errors else "incomplete"
        )
        # Unknown outputs against known expectations remain mismatches.
        mismatches = [c for c in valid if not c["passed"]]
        result = {
            "status": status,
            "checks": checks,
            "valid_checks": valid,
            "mismatches": mismatches,
            "observations_valid": bool(valid),
            "log": executed["log"] + ("\n" + "\n".join(errors) if errors else ""),
            "checker_errors": errors,
            "vcd": str(work / "wave.vcd"),
        }
    return {
        "id": tb["id"],
        "tb": tb,
        "runs": {"verilator": result},
        "source_id": digest(sources),
        "build": build,
        "build_adaptation": adaptation,
    }


def _unique_valid_checks(run):
    raw = run.get("checks", run.get("valid_checks", []))
    counts = Counter(row.get("id") for row in raw)
    return {
        row["id"]: row
        for row in run.get("valid_checks", raw)
        if counts[row["id"]] == 1
        and row.get("valid") is True
        and row.get("waveform_valid") is True
        and row.get("expected_valid", row.get("expected_known")) is True
    }


def compare(before, after):
    """Compare unique waveform-validated CHECK identities; track execution completeness separately."""
    rows = {}
    for backend, original in before["runs"].items():
        current = after["runs"].get(backend, {})
        original_checks, after_checks = _unique_valid_checks(
            original
        ), _unique_valid_checks(current)
        fixed, remaining, missing, regressions = [], [], [], []
        timing_changes = {}
        for c in original_checks.values():
            new = after_checks.get(c["id"])
            if new is None or any(
                c.get(k) != new.get(k) for k in ("requirement", "signal", "expected")
            ):
                missing.append(c["id"])
            elif not c["passed"] and new["passed"]:
                fixed.append(c["id"])
            elif not c["passed"]:
                remaining.append(c["id"])
            elif not new["passed"]:
                regressions.append(c["id"])
        for cid, old_check in original_checks.items():
            new_check = after_checks.get(cid)
            if (
                new_check is not None
                and cid not in missing
                and any(old_check.get(k) != new_check.get(k) for k in ("time", "cycle"))
            ):
                timing_changes[cid] = {
                    "before": {k: old_check.get(k) for k in ("time", "cycle")},
                    "after": {k: new_check.get(k) for k in ("time", "cycle")},
                }
        complete = (
            bool(original_checks)
            and original.get("status") == "ok"
            and current.get("status") == "ok"
            and not missing
        )
        rows[backend] = {
            "fixed": fixed,
            "remaining": remaining,
            "missing": missing,
            "regressions": regressions,
            "status": current.get("status", "missing"),
            "complete": complete,
            "timing_changes": timing_changes,
        }
    return rows


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _under_simulation_guard(prefix):
    stack = []
    for m in re.finditer(
        r"(?m)^\s*`(ifdef|ifndef|else|elsif|endif)\b\s*([^\n]*)", prefix
    ):
        op, tail = m.groups()
        if op in ("ifdef", "ifndef"):
            stack.append((tail.split()[0] if tail.split() else "", op == "ifndef"))
        elif op in ("else", "elsif") and stack:
            name, neg = stack[-1]
            stack[-1] = (name, not neg if op == "else" else False)
        elif op == "endif" and stack:
            stack.pop()
    return ("SYNTHESIS", True) in stack


def adapt_build(sources, build, tb, directory):
    """Adapt a compilation copy and retain mappings to the supplied original source."""
    locator = SourceLocator(sources, tb)
    reachable = set()
    pending = [tb["top"]]
    while pending:
        module = pending.pop()
        if module in reachable:
            continue
        reachable.add(module)
        for rows in locator.instances(module).values():
            pending.extend(
                row["module"] for row in rows if row["module"] not in reachable
            )
    original_files = list(
        build.get("compile_files") or [f for f in sources if f.endswith((".v", ".sv"))]
    )
    files = []
    removed = []
    for file in original_files:
        declared = {
            name
            for name, records in locator.modules.items()
            if any(f == file for f, _, _ in records)
        }
        formal_harness = bool(
            re.search(r"(^|/)formal/.*(?:^|/)formal_tb\.[sv]+$", file)
        )
        if formal_harness and not declared.intersection(reachable):
            removed.append(
                {
                    "file": file,
                    "modules": sorted(declared),
                    "reason": "Formal harness is not instantiated by the actual TB/DUT closure",
                }
            )
            continue
        files.append(file)
    added = []
    for module in sorted(reachable):
        record = locator._module(module)
        if record and record[0] in sources and record[0] not in files:
            file = record[0]
            files.append(file)
            added.append(
                {
                    "file": file,
                    "module": module,
                    "reason": "Unique supplied definition required by actual module closure",
                }
            )
    compiled = dict(sources)
    changes = []
    wrapper_changes = []
    for file in files:
        original = sources.get(file, "")
        text = original
        if file.startswith("__wire_build/"):
            clean = _clean(original)
            declarations = list(
                re.finditer(
                    r"\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_$][\w$]*)\b",
                    clean,
                )
            )
            endings = list(re.finditer(r"\bendmodule\s*:\s*([A-Za-z_$][\w$]*)", clean))
            # Adapt only recognized generated aliases with an unambiguous module and label.
            if len(declarations) == len(endings) == 1:
                name, label = declarations[0][1], endings[0][1]
                if (
                    name == Path(file).stem
                    and label.startswith("prim_generic_")
                    and label.replace("prim_generic_", "prim_", 1) == name
                    and len(name) <= len(label)
                ):
                    begin, end = endings[0].span(1)
                    replacement = name.ljust(len(label))
                    text = text[:begin] + replacement + text[end:]
                    wrapper_changes.append(
                        {
                            "file": file,
                            "line": original.count("\n", 0, begin) + 1,
                            "original_label": label,
                            "compiled_label": name,
                            "reason": "Generated generic alias renamed module declaration without its matching endmodule label; body and ports unchanged",
                        }
                    )
        for match in reversed(list(re.finditer(r"\$display\s*\(", _clean(original)))):
            first = match.start()
            open_paren = match.end() - 1
            end = _balanced(original, open_paren)
            if end is None:
                continue
            semicolon = re.match(r"\s*;", original[end + 1 :])
            if semicolon is None:
                continue
            last = end + 1 + semicolon.end()
            prefix = original[:first]
            if not _under_simulation_guard(prefix):
                continue
            off = [m.start() for m in re.finditer(r"//[^\n]*\btranslate_off\b", prefix)]
            on = [m.start() for m in re.finditer(r"//[^\n]*\btranslate_on\b", prefix)]
            if not off or (on and on[-1] > off[-1]):
                continue
            statement = original[first:last]
            if re.search(r"CHECK|TEST_DONE", statement):
                continue
            args = re.sub(r'"(?:\\.|[^"\\])*"', " ", original[open_paren + 1 : end])
            # Preserve calls, assignments, increments and random draws with side effects.
            if re.search(
                r"\b[\w$]+\s*\(|\+\+|--|(?<![=!<>])=(?!=)|\$(?!time\b|realtime\b)[\w$]+",
                args,
            ):
                continue
            roots = {
                m[1]
                for m in re.finditer(
                    r"(?<![\w$.])([A-Za-z_$][\w$]*)\.[A-Za-z_$][\w$]*", args
                )
            }
            absent = sorted(
                root
                for root in roots
                if root in locator.modules and root not in reachable
            )
            if not absent:
                continue
            masked = ";" + "".join("\n" if c == "\n" else " " for c in statement[1:])
            text = text[:first] + masked + text[last:]
            changes.append(
                {
                    "file": file,
                    "first_line": original.count("\n", 0, first) + 1,
                    "last_line": original.count("\n", 0, last) + 1,
                    "original_statement": statement,
                    "compiled_statement": masked,
                    "uninstantiated_module_roots": absent,
                    "reason": "Pure diagnostic display in SYNTHESIS-negative translate_off block; XMR root absent from actual unit-TB closure",
                }
            )
        if text != original:
            assert len(text) == len(original) and text.count("\n") == original.count(
                "\n"
            )
            compiled[file] = text
    for change in [*changes, *wrapper_changes]:
        change.update(
            original_source_sha256=_sha(sources[change["file"]]),
            compiled_source_sha256=_sha(compiled[change["file"]]),
        )
    effective = deepcopy(build)
    effective["compile_files"] = files
    report = {
        "version": 1,
        "source_policy": "Compilation copies only; no SYNTHESIS define; public sources/TB/CHECK unchanged",
        "line_mapping": {
            "kind": "identity",
            "rule": "compiled line n equals original line n for every file; character count and newlines preserved",
        },
        "actual_module_closure": sorted(reachable),
        "removed_compile_files": removed,
        "added_compile_files": added,
        "diagnostic_changes": changes,
        "generated_wrapper_changes": wrapper_changes,
        "original_compile_files": original_files,
        "effective_compile_files": files,
    }
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "build-adaptation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    return compiled, effective, report


def recovered_test_inputs(tb, sources, connections):
    """Connect added DUT inputs to existing testbench drivers while preserving stimuli and checks."""
    if not connections:
        return tb
    result = deepcopy(tb)
    code = result["code"]
    for connection in connections:
        instance, port, signal = (
            connection[key] for key in ("instance", "port", "signal")
        )
        if instance.startswith(tb["top"] + "."):
            instance = instance[len(tb["top"]) + 1 :]
        if not all(
            re.fullmatch(r"[A-Za-z_$][\w$]*", value) for value in (instance, port)
        ):
            raise ValueError("Use a direct named testbench instance and input port")
        if not re.fullmatch(
            r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\[\d+(?::\d+)?\])*", signal
        ):
            raise ValueError("Connect an existing testbench signal")
        clean = _clean(code)
        if not re.search(
            r"\b" + re.escape(re.split(r"[.\[]", signal)[0]) + r"\b", clean
        ):
            raise ValueError("Recovery input signal is absent from the saved TB")
        locator = SourceLocator(sources, {**tb, "code": code})
        instances = locator.instances(tb["top"]).get(instance, [])
        if len(instances) != 1 or not locator._direction(
            instances[0]["module"], port, "input"
        ):
            raise ValueError("Recovery input port has no unique supplied declaration")
        # Inspect actual port connections before treating shorthand or empty ports as driven.
        instance_matches = list(
            re.finditer(r"\b" + re.escape(instance) + r"\s*\(", clean)
        )
        if len(instance_matches) != 1:
            raise ValueError("Recovery testbench instance is ambiguous")
        inst_start = instance_matches[0].end() - 1
        inst_end = _balanced(clean, inst_start)
        if inst_end is None:
            raise ValueError("Recovery instance port list is incomplete")
        named = list(
            re.finditer(
                r"\." + re.escape(port) + r"\s*\(", clean[inst_start : inst_end + 1]
            )
        )
        if len(named) > 1:
            raise ValueError("Recovery input has duplicate named TB bindings")
        empty_binding = None
        for binding in named:
            begin = inst_start + binding.end() - 1
            finish = _balanced(clean, begin)
            if finish is not None and not clean[begin + 1 : finish].strip():
                empty_binding = (begin, finish)
        if port in instances[0]["ports"] and empty_binding is None:
            if instances[0]["ports"][port] == signal:
                continue
            raise ValueError(
                "Recovery connection would replace an existing stimulus connection"
            )
        if empty_binding is not None:
            begin, finish = empty_binding
            code = code[: begin + 1] + signal + code[finish:]
            continue
        start, end = inst_start, inst_end
        comma = "," if clean[start + 1 : end].strip() else ""
        code = code[: start + 1] + f".{port}({signal}){comma}" + code[start + 1 :]
    result["code"] = code
    return result
