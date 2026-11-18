# WIRE

WIRE localizes RTL bug root causes through requirement-guided test generation (FRTG), temporal backtracing with augmentation (TBA), and counterfactual validation (CV).

- [`WIRE/`](WIRE/): method implementation and prompts.
- [Benchmark](#benchmark): 341 PR tasks and 415 annotated bug root causes from OpenTitan, CVA6, Ibex and Caliptra-RTL.

## Run

Install Python 3.10 or newer, Verilator 5.050 or newer, and a C++ build toolchain. The Python implementation uses the standard library.

Set `MODEL_BASE_URL`, `MODEL_NAME` and `MODEL_API_KEY` for an OpenAI-compatible model endpoint. From the repository root:

```bash
python -m WIRE --case /path/to/case.json --out runs/example
```

A case supplies the specification, pre-fix RTL and build dependencies. See [`WIRE/INPUT.md`](WIRE/INPUT.md) for its format. `--task-id caliptra-rtl/pr-208` loads a task from the bundled benchmark.

The pipeline extracts and audits requirements, generates testbenches, traces mismatches and augments missing dependencies. CV evaluates independent edits using common observations, computes `FP + upstream_FP - PF`, and refines the candidate order with the model. Results are written to `cv/result.json` under the output directory.

## Benchmark

We construct a repository-level benchmark from bug-fix pull requests (PRs) in four open-source RTL projects: [OpenTitan](https://github.com/lowRISC/opentitan), [CVA6](https://github.com/openhwfoundation/cva6), [Ibex](https://github.com/lowRISC/ibex), and [Caliptra-RTL](https://github.com/chipsalliance/caliptra-rtl). The benchmark contains **341 PR tasks and 415 gold bug root-cause (BRC) entries**.

We retain PRs that fix functional RTL bugs and exclude feature additions, verification-only changes, and refactoring PRs. For each retained PR, we restore the pre-fix revision as the buggy repository.

An LLM drafts each specification from the project documentation and corrected RTL code. Five RTL engineers review each draft, align it with the corrected revision, and remove clues that reveal the edited locations.

Human annotators derive the gold BRC line ranges from the PR fix. Each PR defines one localization task that may span multiple RTL files and contain multiple BRC locations. The paper's reported results use the frozen 415-entry annotations.

Browse the task files in [`Benchmark/`](Benchmark/).

| Path | Content |
| --- | --- |
| `Benchmark/<repo>/pr-<number>/inputs/task.json` | Repository, PR identifier, target files and specifications |
| `Benchmark/<repo>/pr-<number>/inputs/repo/` | Pre-fix target RTL |
| `Benchmark/<repo>/pr-<number>/gold/gold.json` | BRC annotations for evaluation |

WIRE loads the specification and RTL from `inputs/`. Evaluation uses the annotations in `gold/`.

### Restore the full repository

Each task pins the upstream pre-fix commit in `inputs/task.json`. The
benchmark lists target files for localization and provides the complete
pinned repository as project context through the restoration command below.

Run the following command from the repository root with Python 3, Git, and network access.

```sh
python3 Benchmark/prepare.py opentitan/pr-284 --output /tmp/wire-opentitan-pr-284
```

The output contains `task.json` and a `repo/` checkout of the exact upstream
commit, including its pinned submodules. Use the original build files and
project documentation in that checkout with your own tools.

RTL files retain their original copyright and license notices.
