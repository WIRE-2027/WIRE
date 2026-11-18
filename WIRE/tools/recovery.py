"""Prepare augmentation tests and analysis of reconstructed dependency paths."""

from copy import copy, deepcopy
from functools import partial

from ..contracts import ContractError, digest, entry_input, normalize_analysis, normalize_proposals
from .analysis import (
    analysis_payload, boundary_context, compact_packet, family, joined, merge_entries,
    pack_traces, tool_view,
)
from .execution import MAX_TBA_REPAIRS, repair_test
from .simulation import _unique_valid_checks


class ReplayView:
    """Default queries to the augmented variant while retaining explicit variant selection."""

    def __init__(self, box, variant):
        self.box, self.variant = box, variant

    def __getattr__(self, name):
        value = getattr(self.box, name)
        if name in ('graph', '_test', '_run', 'read_test', 'read_source',
                    'query_ast', 'query_waveform', 'trace_temporal'):
            return partial(value, variant=self.variant)
        return value


def recovery_jobs(ctx, state):
    """Prepare augmentation jobs from mismatch groups and unchecked requirements."""
    box, excluded = state['box'], state['excluded']
    jobs = []
    for records in state['by_family'].values():
        analysis = joined([deepcopy(a) for _, a in records])
        members = list(dict.fromkeys(
            e['id'] for p, _ in records for e in p['entries']
            if e['id'] not in excluded
        ))
        analysis['boundaries'] = [b for b in analysis['boundaries'] if b['entry'] in members]
        if members:
            jobs.append({'analysis': analysis, 'members': members, 'hints': []})
    # Unchecked requirements can enter augmentation analysis before a valid mismatch exists.
    hints = getattr(ctx, 'requirement_hints', [])
    if hints:
        jobs.append({'analysis': joined([]), 'members': [], 'hints': deepcopy(hints)})
    for job in jobs:
        members = job['members']
        test_ids = list(dict.fromkeys(box.entries[e]['test'] for e in members))
        job['tests'] = [box._test(t) for t in test_ids] if members else state['tests']
    return jobs


def proposal_input(ctx, box, job, requirements, source_input):
    analysis, members = job['analysis'], job['members']
    located = {e for c in analysis['candidates'] for e in c.get('entries', [])}
    return {
        **source_input,
        **analysis,
        'requirements': requirements,
        'requirement_hints': job['hints'],
        'entries': [entry_input(box.entries[e], box._record('entry', box.entries[e])) for e in members],
        'unlocated_entries': [e for e in members if e not in located],
        'boundary_ast': boundary_context(box, analysis['boundaries']),
        'tests': [{'id': t['id'], 'tb': t.get('tb', {}),
                   'runs': {b: {'status': r.get('status'), 'checker_errors': r.get('checker_errors', [])}
                            for b, r in t.get('runs', {}).items()}}
                  for t in job['tests']],
    }


def normalize_recovery_proposals(value, job, requirements):
    """Validate proposal references for mismatch and requirement starting points."""
    value = normalize_proposals(value)
    allowed = {r['id'] for r in requirements}
    pending = {h['requirement'] for h in job['hints']}
    tests = {t['id'] for t in job['tests']}
    for proposal in value['proposals']:
        ids = proposal.get('requirements') or []
        if not isinstance(ids, list) or any(not isinstance(r, str) or r not in allowed for r in ids):
            raise ContractError('requirements must contain supplied requirement IDs')
        entry = proposal.get('entry')
        if entry:
            if entry not in job['members']:
                raise ContractError('entry must be one of the supplied entries')
            members = proposal.get('entries') or [entry]
            if not isinstance(members, list) or entry not in members or any(e not in job['members'] for e in members):
                raise ContractError('entries must include entry and refer to supplied entries')
        elif not ids or not set(ids) <= pending:
            raise ContractError('A requirement-start proposal needs pending requirement IDs')
        if proposal.get('test') and proposal['test'] not in tests:
            raise ContractError('test must be an existing supplied TB ID; omit it to generate a new TB')
        if not isinstance(proposal.get('anchor'), dict) or not isinstance(proposal.get('relation'), dict):
            raise ContractError('Each proposal needs anchor and relation objects')
        if not isinstance(proposal.get('edits'), list) or not proposal['edits']:
            raise ContractError('Each proposal needs exact RTL edits')
    return value


def register_observations(box, test, requirement_ids):
    """Register passing checks as check_observation and failing checks as mismatch."""
    selected = deepcopy(test)
    for run in selected.get('runs', {}).values():
        checks = []
        for c in _unique_valid_checks(run).values() if run.get('status') == 'ok' else []:
            ids = c.get('requirement', [])
            ids = [ids] if isinstance(ids, str) else ids
            if set(ids) & set(requirement_ids) and all(
                c.get(k) is True for k in ('valid', 'waveform_valid', 'expected_known')
            ):
                checks.append(c)
        run.pop('accepted_mismatch_subset', None)
        run['mismatches'] = checks
    members = merge_entries([selected])
    for entry in members:
        entry['id'] = f'E{len(box.entries) + 1}'
        entry['kind'] = ('mismatch' if any(o['check'].get('passed') is False
                                         for o in entry['observations']) else 'check_observation')
        for i, observation in enumerate(entry['observations']):
            observation['mismatch_id'] = f"{entry['id']}_check_{i + 1}"
        box.entries[entry['id']] = entry
    return members


def prepare_proposal(ctx, box, proposal, job, requirements, state, cache, name):
    """Establish original-RTL observations before replaying the same test on an augmentation."""
    allowed = {r['id'] for r in requirements}
    ids = proposal.get('requirements') or []
    if not isinstance(ids, list) or not set(ids) <= allowed:
        raise ValueError('Proposal requirements must be supplied requirement IDs')
    chosen_entry = proposal.get('entry')
    if chosen_entry:
        if chosen_entry not in job['members']:
            raise ValueError('Proposal entry must belong to this augmentation task')
        members = proposal.get('entries') or [chosen_entry]
        if not set(members) <= set(job['members']):
            raise ValueError('Related entries must belong to this augmentation task')
        if any(box.entries[e]['test'] != box.entries[chosen_entry]['test'] for e in members):
            raise ValueError('Related entries must use the same baseline test')
    else:
        pending = {h['requirement'] for h in job['hints']}
        if not ids or not set(ids) <= pending:
            raise ValueError('A requirement-start proposal must name pending requirement IDs')
        test_id = proposal.get('test')
        tests = [t for t in job['tests'] if not test_id or t['id'] == test_id]
        if test_id and not tests:
            raise ValueError('Proposal test must name a supplied test')
        relevant = [t for t in tests if set(ids) & set(
            t.get('assigned_requirements') or [c['requirement'] for c in t.get('tb', {}).get('conditions', [])]
        )]
        suitable = relevant or (tests if test_id else [])
        test = deepcopy(suitable[-1]) if suitable else {'id': name, 'tb': {}, 'runs': {}}
        key = digest([test['id'], ids, proposal.get('test_plan', ''), job['hints']])
        if key not in cache:
            # Derive expectations from the requirement before applying augmentation edits.
            issues = [{'requirements': ids, 'reason': proposal.get('test_plan') or
                       'Exercise the pending requirement conditions on the original RTL.',
                       'pending_conditions': [h for h in job['hints'] if h['requirement'] in ids]}]
            assigned = (set(test.get('assigned_requirements') or []) | set(ids) |
                        {c['requirement'] for c in test.get('tb', {}).get('conditions', [])})
            test['assigned_requirements'] = [r['id'] for r in requirements if r['id'] in assigned]
            cache[key] = []
            for revision in range(MAX_TBA_REPAIRS + 1):
                test = repair_test(ctx, test, requirements, issues, f'{name}_baseline_r{revision + 1}')
                state['tests'].append(test)
                state['repairs'].append(test)
                box.variants['original']['tests'][test['id']] = test
                observed = register_observations(box, test, ids)
                covered = {r for e in observed for r in e['requirements']}
                if set(ids) <= covered:
                    cache[key] = [e['id'] for e in observed]
                    state.setdefault('batches', []).append({
                        'group': {'id': 'G_pending_' + key[:10], 'family': family(box, observed[0]),
                                  'representative': observed[0]['id'], 'members': cache[key]},
                        'entries': observed,
                    })
                    break
                issues = [{'requirements': ids, 'reason': 'Produce valid CHECKs for the requested conditions.',
                           'feedback': test.get('repair_notes', []),
                           'runs': {b: {'status': r.get('status'), 'log': r.get('log', '')}
                                    for b, r in test.get('runs', {}).items()}}]
        members = cache[key]
        if not members:
            raise ValueError('The original RTL test produced no valid observation for this proposal')
        chosen_entry = next((e for e in members if box.entries[e]['kind'] == 'mismatch'), members[0])
    fields = ('anchor', 'relation', 'edits', 'reason', 'test_connections')
    return {**{k: deepcopy(proposal[k]) for k in fields if k in proposal},
            'entry': chosen_entry, 'entries': list(members)}


def replay_analysis_input(ctx, box, outcome, requirements):
    """Combine augmented AST and waveform results with original observations and source mappings."""
    variant = outcome['variant']
    test = box._test(box.entries[outcome['proposal']['entry']]['test'], variant)
    run = test['runs'].get('verilator', {})
    if run.get('status') != 'ok' or not run.get('valid_checks'):
        raise ValueError('Augmented test needs a completed simulation with valid CHECKs')
    members = [box.entries[e] for e in outcome['proposal']['entries']]
    # Share the material store while defaulting queries to this augmented variant.
    view = copy(box)
    view.entries = deepcopy(box.entries)
    checks = _unique_valid_checks(run)
    for entry in members:
        aligned = [checks[o['check']['id']] for o in entry['observations']
                   if o['check']['id'] in checks and
                   all(checks[o['check']['id']].get(k) == o['check'].get(k)
                       for k in ('requirement', 'signal', 'expected'))]
        if len(aligned) != len(entry['observations']):
            raise ValueError('Original and augmented CHECK identities must align')
        times = {c['time'] for c in aligned}
        if len(times) != 1:
            raise ValueError('Entry CHECKs must share one replay time')
        view.entries[entry['id']]['time'] = next(iter(times))
    data = analysis_payload(ctx, ReplayView(view, variant), requirements, members)
    extra = []
    for mid in [*(outcome.get('boundary_materials') or []), outcome.get('continued_material')]:
        if mid:
            extra.append((mid, box.materials[mid]))
    data['recovery'] = {
        'variant': variant,
        'proposal': outcome['proposal'],
        'comparisons': outcome['comparisons'],
        'entry_results': outcome['entry_results'],
        'dependency_added': outcome['dependency_added'],
        'path_active': outcome['path_active'],
        'path_suppressed_by_new_control': outcome['path_suppressed_by_new_control'],
        'trace_context': compact_packet(pack_traces(extra)),
        'original_line_maps': outcome['original_line_maps'],
        'rtl': {f: '\n'.join(f'{i}: {s}' for i, s in enumerate(text.splitlines(), 1))
                for f, text in box._variant(variant)['sources'].items()},
    }
    data['group'] = {'id': variant, 'representative': outcome['proposal']['entry']}
    bindings = view.model_bindings(members=[e['id'] for e in members])
    for name, binding in bindings.items():
        function = binding['call']
        if name != 'read_material':
            binding['schema']['parameters']['properties']['variant'].update(
                enum=[variant], description=f'本次查询使用增补副本 {variant}；省略时也使用此副本。'
            )
            def call(_function=function, **arguments):
                selected = arguments.pop('variant', variant)
                if selected != variant:
                    raise ValueError(f'Use this analysis variant: {variant}')
                return _function(**arguments, variant=variant)
            binding['call'] = call
        binding['present'] = tool_view
    return data, bindings


def normalize_replay_analysis(value, ctx, entries, requirements):
    """Return candidates at original source locations with valid entry references."""
    result = normalize_analysis(value, ctx.sources, [e['id'] for e in entries], entries)
    allowed = {r['id'] for r in requirements}
    for candidate in result['candidates']:
        if not candidate['entries']:
            raise ContractError('A replay candidate needs its supplied observation entry IDs')
        if not set(candidate.get('requirements', [])) <= allowed:
            raise ContractError('Use supplied requirement IDs for replay candidates')
    invalid = {e for issue in result.get('tb_errors', []) for e in issue['entries']}
    for candidate in result['candidates']:
        candidate['entries'] = [e for e in candidate['entries'] if e not in invalid]
    result['candidates'] = [c for c in result['candidates'] if c['entries']]
    result['origin'] = 'augmented_analysis'
    return result
