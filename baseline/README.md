# Baseline implementation

Inference-only PMWP evaluation through **OpenRouter**, with separate code, prompts, tests, and result artifacts. The [root README](../README.md) documents the base paper, its reported results, and reproduction gaps.

**Implemented and tested offline. Live model compatibility, billing fields, and benchmark performance remain unverified until an authorized API smoke run.** Python 3.11+ on Linux/macOS is supported; run-directory locking uses POSIX `flock`. No weights are downloaded and no model is trained.

## Layout

```text
baseline/
├── configs/example.json
├── src/pmwp_baseline/
│   ├── config.py          # validated settings
│   ├── data.py            # JSON / JSONL loading
│   ├── prompts.py
│   ├── prompts/           # English originals and declared local prompts
│   ├── client.py          # HTTPS OpenRouter transport
│   ├── parsing.py         # restricted arithmetic parser
│   ├── evaluation.py      # paper metrics and diagnostics
│   ├── telemetry.py
│   ├── runner.py          # durable output and strict resume
│   └── cli.py
├── tests/{unit,integration,fixtures}/
└── results/<run-id>/
```

## Install and verify

From the repository root, with [uv](https://docs.astral.sh/uv/) installed:

```bash
cd baseline
uv sync --frozen --extra dev
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src
uv run --frozen pmwp-baseline validate-data ../validset.json
```

The runtime uses the Python standard library; developer tools are pinned in `pyproject.toml` and `uv.lock`. If the default uv cache is read-only, set `UV_CACHE_DIR` to a writable directory such as `/tmp/pmwp-uv-cache`. Reuse the existing checkout in cloud tasks; no separate worktree is needed.

## Configure an experiment

Copy `configs/example.json` to a local configuration file. Replace the placeholder model and provider with exact available OpenRouter identifiers. Do not assume the historical model snapshots in the paper remain available. For each model, create the four combinations:

| `language` | `mode` | Pipeline |
| :--- | :--- | :--- |
| `fa` | `numeric` | Persian → answer |
| `fa` | `equation` | Persian → expression |
| `en` | `numeric` | Same model translates → English → answer |
| `en` | `equation` | Same model translates → English → expression |

Temperature is required to be **zero**, matching the paper. The example's `max_tokens=2048`, timeout, retry settings, and numerical tolerances are **our explicit choices**, not values recovered from the paper. Keep them fixed across compared runs. Unspecified decoding parameters use provider defaults; record that limitation when reporting results. Provider routing is pinned with `only`, fallback is disabled, and parameter support is required. The API's returned model/provider identifiers and finish reason are saved.

Enter `OPENROUTER_API_KEY` through secure cloud environment settings. Never commit or paste it into configuration files. The CLI reads it only for `run`; offline validation, evaluation, comparison, and tests need no credentials. HTTPS access to `openrouter.ai` is required.

## Run and resume

The commands below send paid API requests. Start with a small **validation** smoke run once the key and selected model/provider are available:

```bash
uv run --frozen pmwp-baseline run \
  --config configs/local.json --dataset ../validset.json \
  --split validation --limit 3 --output results/validation-smoke-001
```

For final evaluation, freeze the method and use the whole test file:

```bash
uv run --frozen pmwp-baseline run \
  --config configs/local.json --dataset ../testset.json \
  --split test --output results/test-001
```

Resume using the same command plus `--resume`. The manifest must match the dataset bytes, selected IDs/order, configuration, prompt hashes, and source hashes. Concurrent writers to the same directory are rejected. Progress is sequential and flushed/fsynced after each example. API failures remain in the denominator and are not silently retried in later resumes. Network-level retries are bounded; a timeout may still have incurred provider charges.

The split label is a user declaration; use the correct file. A limited run is recorded as a subset, not a full-test evaluation. Expected request counts before retries: `N` for Persian, `2N` for translated English; all four configurations use `6N` calls per model. For the supplied 1,701-example test set this is **10,206 calls per model** before retries. No cost estimate is claimed without current model pricing and token measurements.

## Prompts and fidelity

- `numeric.en.txt` and `equation.en.txt`: English templates published in Section 4.1, with normalized PDF line wrapping.
- `numeric.fa.txt` and `equation.fa.txt`: our faithful Persian translations of those templates; original Persian wording was not published.
- `system.txt`: our problem-solver instruction; the authors describe the role but do not publish its exact text.
- `translation.txt`: our translation-only instruction; its wording is not provided in the paper.

Only `Problem.text` is interpolated. Gold `answer`, gold `equation`, numeric mappings, and templates never enter request context. The same files and settings are used for all models. English translation uses the same configured model/provider in a separate request, followed by a fresh solving request; context reuse was not specified by the paper. Translation and solving have separate telemetry. We do not cache translations across output modes; each configuration is an independent pipeline.

## Scoring policy

**AnsAcc** compares the final numerical answer with the gold `answer`. **EqValueAcc** evaluates the generated `x = expression` and compares its value with that same gold answer. It does not require exact equation-string matching.

Declared local decisions, because the paper does not publish evaluator details:

- Extract the last nonempty line after trimming outer whitespace; require lowercase `x = ...` (flexible internal spacing for scoring).
- Numeric mode accepts a signed decimal or scientific-notation literal. It does not accept an unevaluated expression or fraction.
- Normalize Persian/Arabic digits, decimal separator `٫`, and `−`, `×`, `÷`. Thousands separators, percent notation, Markdown fences, and LaTeX are not repaired.
- Equation mode permits bounded decimal/scientific literals, parentheses, unary signs, and `+ - * /`. Names, function calls, powers, and arbitrary code are rejected. Arithmetic uses exact rational intermediates before conversion for comparison.
- Use `math.isclose` with the configured absolute and relative tolerances. Invalid expressions, missing output, API failures, and truncated/unparseable output count as incorrect. A parseable truncated output is scored by value and its finish reason remains recorded.
- Report full-population accuracy even during interruption, with unrun count and partial status; confidence intervals are withheld until completion. Never present a partial run as final.
- Formatting compliance and an unsimplified-expression syntax diagnostic are separate from numerical correctness. The syntax diagnostic does not prove that an expression represents genuine reasoning.

Re-score saved content offline against the original dataset:

```bash
uv run --frozen pmwp-baseline evaluate results/test-001 --dataset ../testset.json
uv run --frozen pmwp-baseline compare results/test-fa results/test-en
```

Re-scoring writes a new `reevaluated-summary.json` without rewriting raw predictions. Comparison requires complete runs with matching data, IDs, split, metric, and tolerances; it uses saved per-example scores. Compare only runs evaluated with the same evaluator version. It reports paired wins/losses, accuracy difference, and a deterministic 2,000-resample paired bootstrap interval (seed 0). Intervals describe sampling across examples, not variability across repeated API runs; augmented examples may not be statistically independent.

## Diagnostics and measurement limits

Beyond the paper metrics, summaries provide:

- Wilson 95% accuracy interval; invalid output, format compliance, API failure, and retry counts.
- Mean, median, and nearest-rank p95 full-response latency per stage and end-to-end. Retry waits are included. Nonstreaming means time to first token is not measured.
- Provider-reported input/output/total tokens and cost, per stage and combined. Unknown values remain `null` with missing-call counts; reasoning/cache details are preserved in raw usage when provided.
- Reported cost per correct answer. This can undercount charges when failed requests have no usage response; it is not a complete billing audit.
- Accuracy grouped by `pure_template`, text-length bins, number count, and operation-symbol count in `pure_template`. The latter is a structural diagnostic and includes unary minus symbols.

These group fields are used only after inference for analysis, never as model inputs. `failures.jsonl` includes all incorrect cases, not only API errors. Artifacts and retention rules are described in [results/README.md](results/README.md).

## Tests and CI

Unit tests cover data encodings and invalid data, prompt isolation, config validation, exact/bounded arithmetic, unsafe syntax rejection, output extraction, tolerances, unknown usage, payload construction, authentication failures, and bounded retries. Integration tests use an injected fake transport/client to exercise translation → solving, scoring, persistence, interruption/resume, changed-config rejection, corrupted artifacts, offline re-scoring, paired comparison, and CLI dispatch. They make **no external requests**.

GitHub Actions is configured to run lint, formatting, strict type checking, tests, and benchmark validation on Python 3.11–3.13. Local validation does not establish that remote CI has run. Live API smoke testing is a separate manual step; no test sends paid requests automatically.

## Infrastructure-only recovery pass

After finishing the original pass, recover transient network/service failures into a **new** directory:

```bash
uv run --frozen pmwp-baseline recover-network results/test-001 \
  --output results/test-001-network-recovery
```

Only `transport_error` and HTTP 408/429/500/502/503/504 are selected. Incorrect answers, parsing/format failures, `missing_content`, `invalid_response`, and authentication/configuration errors are never selected. Successful translation stages are replayed locally rather than submitted again. The original run remains unchanged; cumulative calls, usage, and latency are retained in the recovered run. This command supports one bounded recovery pass from an original completed run. It does not resume an interrupted recovery directory; preserve that directory if interrupted. Existing within-request bounded retries still apply only to transport and eligible HTTP failures.

New responses without usable content retain available response metadata (`usage`, choices, finish reason, and identifiers) for investigation, without being retried. Old missing-content records cannot be retroactively reconstructed. These are adapter status labels, not statements produced by the model or proof of incomplete problem input.
