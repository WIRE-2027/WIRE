"""Stream declared VCD signals and sample at native ticks."""

from __future__ import annotations

from collections import deque
from pathlib import Path


class _HistoryView(list):
    """Read-only prefix view over compact waveform events."""

    def __init__(self, owner, size):
        self.owner, self.size = owner, size

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.size))]
        if index < 0:
            index += self.size
        if index < 0 or index >= self.size:
            raise IndexError(index)
        return [self.owner.ticks[index], self.owner.values[self.owner.ids[index]]]

    def __iter__(self):
        for i in range(self.size):
            yield self[i]

    def __eq__(self, other):
        return list(self) == list(other)

    def __ne__(self, other):
        return not self == other


class _PackedHistory:
    """Store exact native ticks with shared values."""

    def __init__(self):
        from array import array

        self.ticks, self.ids = array('q'), array('I')
        self.values, self.index = [], {}

    def append(self, event):
        tick, value = event
        slot = self.index.get(value)
        if slot is None:
            slot = len(self.values)
            self.values.append(value)
            self.index[value] = slot
        self.ticks.append(tick)
        self.ids.append(slot)

    def snapshot(self):
        return _HistoryView(self, len(self.ticks))


def _header(stream):
    scopes, names, timescale = [], {}, ""
    for raw in stream:
        line = raw.strip()
        if line.startswith("$timescale"):
            timescale = line
            while "$end" not in timescale:
                timescale += " " + next(stream).strip()
        elif line.startswith("$scope"):
            scopes.append(line.split()[2])
        elif line.startswith("$upscope"):
            scopes.pop()
        elif line.startswith("$var"):
            parts = line.split()
            name = ".".join([*scopes, parts[4]]).removeprefix("TOP.")
            names[name] = {"code": parts[3], "width": int(parts[2]), "kind": parts[1]}
        elif line.startswith("$enddefinitions"):
            break
    return names, timescale


def list_signals(path, contains=""):
    path = Path(path)
    if not path.is_file():
        return {"error": "VCD not available", "signals": []}
    with path.open() as stream:
        names, scale = _header(stream)
    return {
        "timescale": scale,
        "signals": [
            {"name": name, **{k: v for k, v in info.items() if k != "code"}}
            for name, info in names.items()
            if contains in name
        ],
    }


# A history_limit of None or zero retains the full preceding history.
def read_wave(path, signals, times, history_limit=8, compact_history=False):
    """Sample native VCD ticks; accept shared or per-signal query times."""
    path = Path(path)
    if not path.is_file():
        return {"error": "VCD not available", "samples": {}}
    signals = list(dict.fromkeys(signals))
    if isinstance(times, dict):
        requested = {
            name: sorted(set(int(tick) for tick in times.get(name, []))) for name in signals
        }
    else:
        shared = sorted(set(int(tick) for tick in times))
        if not shared or shared[0] < 0:
            raise ValueError("Provide nonnegative VCD sample times")
        requested = {name: shared for name in signals}
    queries = sorted((tick, name) for name, ticks in requested.items() for tick in ticks)
    if queries and queries[0][0] < 0:
        raise ValueError("Provide nonnegative VCD sample times")
    current, query_index = 0, 0
    samples = {name: [] for name in signals}
    with path.open() as stream:
        names, scale = _header(stream)
        codes, events = {}, {}
        for name in signals:
            if name in names and requested[name]:
                code = names[name]["code"]
                codes.setdefault(code, []).append(name)
                events.setdefault(
                    code,
                    (
                        _PackedHistory()
                        if compact_history and not history_limit
                        else deque(maxlen=history_limit or None)
                    ),
                )

        # Collect samples before the next tick to obtain the current tick's final value.
        def collect_before(next_tick):
            nonlocal query_index
            snapshots = {}
            while query_index < len(queries) and queries[query_index][0] < next_tick:
                tick, name = queries[query_index]
                code = names.get(name, {}).get("code")
                # VCD aliases share one event sequence and read-only snapshots.
                history = snapshots.setdefault(code, None)
                if history is None:
                    source = events.get(code, ())
                    history = (
                        source.snapshot() if isinstance(source, _PackedHistory) else list(source)
                    )
                    snapshots[code] = history
                samples[name].append(
                    {"time": tick, "value": history[-1][1] if history else None, "history": history}
                )
                query_index += 1

        for raw in stream:
            line = raw.strip()
            if line.startswith("#"):
                next_tick = int(line[1:])
                collect_before(next_tick)
                if query_index == len(queries):
                    break
                current = next_tick
            elif line and line[0] in "01xXzZbB":
                val, code = line[0].lower(), line[1:].strip()
                if line[0] in "bB":
                    val, code = line[1:].split()
                    val = val.lower()
                if code in events:
                    events[code].append([current, val])
        collect_before(float('inf'))
    return {
        "timescale": scale,
        "samples": samples,
        "widths": {name: names[name]["width"] for name in signals if name in names},
        "unresolved": [name for name in signals if name not in names],
    }


def query_declared_wave(path, signals, times, history_limit=8):
    """Sample declared names; model queries may resolve a unique leaf-name alias."""
    declared = {row['name'] for row in list_signals(path).get('signals', [])}
    leaves = {}
    for name in declared:
        leaves.setdefault(name.rsplit('.', 1)[-1], []).append(name)
    resolved = {}
    for name in signals:
        matches = leaves.get(name.rsplit('.', 1)[-1], [])
        resolved[name] = name if name in declared or len(matches) != 1 else matches[0]
    names = list(dict.fromkeys(resolved.values()))
    if isinstance(times, dict):
        mapped_times = {name: [] for name in names}
        for name, target in resolved.items():
            mapped_times[target].extend(times.get(name, []))
        times = mapped_times
    result = read_wave(path, names, times, history_limit=history_limit)
    corrections = {name: target for name, target in resolved.items() if name != target}
    if corrections:
        result['signal_resolution'] = corrections
    return result


def compact_wave(result):
    """Share repeated histories while preserving exact query results through range indices."""
    histories, samples = {}, {}
    for signal, rows in result.get("samples", {}).items():
        events, compact = [], []
        for row in rows:
            history = row.get("history", [])
            overlap = next(
                (
                    n
                    for n in range(min(len(events), len(history)), 0, -1)
                    if events[-n:] == history[:n]
                ),
                0,
            )
            start = len(events) - overlap
            events.extend(history[overlap:])
            compact.append(
                {
                    **{k: v for k, v in row.items() if k != "history"},
                    "history_range": [start, len(events)],
                }
            )
        histories[signal], samples[signal] = events, compact
    return {**result, "samples": samples, "histories": histories}
