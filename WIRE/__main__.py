"""Run the WIRE framework on public RTL and specification inputs."""

from __future__ import annotations

from .contracts import build_for_tb, digest, load_input, save
from .model import Model, Session, load_config
from pathlib import Path
from .pipeline import run
import argparse, json, sys


def main(argv=None):

    parser = argparse.ArgumentParser(description='WIRE FRTG + TBA + CV')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--case', type=Path, help='Public Spec, RTL and build configuration')
    source.add_argument('--frozen-input', type=Path, help='Previously frozen public input.json')
    source.add_argument('--task-id', help='Benchmark task, e.g. caliptra-rtl/pr-208')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, help='Model configuration JSON')
    parser.add_argument('--stop-after', choices=['m1', 'm2-original', 'm2', 'cv'], default='cv',
                        help='Final stage; m2-original stops before augmentation')
    parser.add_argument('--m1-result', type=Path, help='Reuse FRTG output for the same public input')
    parser.add_argument('--m2-result', type=Path, help='Reuse TBA output for the same public input')
    args = parser.parse_args(argv)
    if args.stop_after == 'm1' and args.m2_result:
        parser.error('--m2-result cannot be used with --stop-after m1')
    args.out = args.out.expanduser().resolve()
    code_root = Path(__file__).parent.resolve()
    if args.out.is_relative_to(code_root):
        parser.error('Choose an output directory outside the WIRE source directory')
    args.out.mkdir(parents=True, exist_ok=True)

    frozen = load_input(args.out, args.task_id, args.case, args.frozen_input)
    provider = load_config(args.model_config)

    implementation = digest(
        {
            str(p.relative_to(code_root)): p.read_text()
            for p in sorted(code_root.rglob('*'))
            if p.is_file() and p.suffix in {'.py', '.md'}
        }
    )
    config = {
        'input_id': frozen['id'],
        'implementation': implementation,
        'provider': provider,
        'simulator': 'verilator',
        'prompt_language': 'en',
        'stop_after': args.stop_after,
        'reused_results': {
            stage: {'path': str(path.expanduser().resolve()),
                    'content_id': digest(json.loads(path.expanduser().read_text()))}
            for stage, path in [('m1', args.m1_result), ('m2', args.m2_result)] if path
        },
    }
    saved = args.out / 'run.json'
    if saved.exists() and json.loads(saved.read_text()) != config:
        parser.error('Inputs or implementation changed; use a new output directory')
    save(saved, config)
    runtime_input = json.loads(json.dumps(frozen))

    runtime_input['build'] = build_for_tb(runtime_input['build'], {})

    model = Model(
        args.out,
        provider['base_url'],
        provider['model'],
        provider['api_key_env'],
        request_options=provider['request_options'],
    )
    save(args.out / 'status.json', {'state': 'running'})
    try:
        ctx = Session(runtime_input, args.out, model)
        result = run(ctx, stop_after=args.stop_after,
                     m1_result=args.m1_result, m2_result=args.m2_result)
    except Exception as error:
        save(
            args.out / 'status.json',
            {'state': 'error', 'error_type': type(error).__name__, 'error': str(error)},
        )
        print(f'Run incomplete: {args.out / "status.json"}', file=sys.stderr)
        return 1
    unresolved = result.get('unresolved', [])
    save(
        args.out / 'status.json',
        {'state': 'stage_complete', 'scope': args.stop_after, 'unresolved': unresolved},
    )
    print(json.dumps({'state': 'stage_complete', 'output': str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
