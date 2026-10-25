# Timing and usage measurement contract — schema 2

These are client-observed operational measurements for OpenRouter. They supplement the paper's AnsAcc/EqValueAcc without changing their definitions. No judge is invoked during inference or scoring. Downstream judge/human analysis must preserve baseline outputs and scores, with its own token, cost, and timing records.

## Timing boundaries

Durations use Python's monotonic `time.perf_counter()` in seconds. UTC timestamps are provenance fields; durations are not calculated by subtracting wall-clock timestamps.

| Field | Start → end | Included / excluded |
| --- | --- | --- |
| Attempt `response_seconds` | Immediately before opening/sending the request → full response body read, or transport failure | Includes client connection/proxy/network/provider queueing and generation. Does not include local JSON decoding, retry waits, or another attempt. HTTP error-body reading is included. |
| Attempt `response_processing_seconds` | Response body read/failure → local response decoding and validation complete | Local JSON processing/validation, not model computation. |
| Attempt `retry_wait_seconds` | Immediately before retry sleep → sleep returns | Actual elapsed wait; `retry_wait_planned_seconds` separately preserves the requested delay. Zero on terminal attempts. |
| Logical `request_seconds` | Before the first attempt → completion/final failure after decoding/validation | Includes all attempts, actual backoff waits, and local processing within that logical call. Payload construction occurs before this timer. |
| Problem `initial_pass_processing_seconds` | Before prompt rendering → prediction parsing and correctness calculation complete | Includes translation and solving for English runs; excludes dataset loading, run startup, writing predictions to disk, and aggregate summarization. |
| Problem `recovery_processing_seconds` | Start → finish of the recovery inference for that problem | No new call for a successful translation replayed from the parent. Includes local replay overhead. |
| Problem `cumulative_processing_seconds` | Sum of initial processing duration and recovery processing duration | Accumulated work duration, **not** elapsed time from the original request to eventual completion. Interpass idle time is excluded. |

Attempt records are stored at `predictions.jsonl` → `calls[*].metadata.attempt_records[*]`. Each includes attempt index, UTC start/completion timestamps, HTTP status when available, outcome/error, finish reason, response evidence and usage. Completion timestamps refer to the end of response processing, before any subsequent retry sleep.

The containing call's `metadata.timing` holds logical-request timing. Each row's `timing` holds problem timing. The old raw `latency_seconds` field is retained as a compatibility alias: logical-call duration in calls; duration of the most recent `infer` invocation in rows. Use the explicitly named schema-2 fields for reporting.

### Populations and summaries

- `summary.json` → `total_usage.timing` and `telemetry.translation/solving.timing`: per-attempt response and per-logical-request time, split into `all`, `success`, and `failure` populations.
- Success means the API returned an accepted string-content completion, **regardless of mathematical correctness or final-line formatting**. An empty string is accepted at transport level; downstream pipeline validity can still fail. Null/non-string content, malformed responses, HTTP errors and transport errors are failures.
- `problem_timing.initial_pass_processing_seconds` splits by original pipeline status (`ok` vs other statuses), independent of answer correctness. Recovery does not relabel an initially failed attempt as an initially successful one.
- `problem_timing.recovery_processing_seconds` covers only submitted recovery problems, split by their recovery pipeline status.
- `problem_timing.cumulative_processing_seconds` describes cumulative work over all problems with known durations. It is not called end-to-end latency.
- Every distribution includes `n`, mean, median, and nearest-rank p95 (`sorted[ceil(.95*n)-1]`). Empty populations have `n=0` and null statistics. Means/percentiles for a whole pipeline are calculated from per-problem measurements, not by adding stage percentiles.
- Failed logical-request time includes its retries. Per-attempt time excludes waits. They are different views of the same work and must not be added together.

No time to first token (TTFT), server-only inference time, or hardware benchmark is measured. The nonstreaming API and provider/network delays prevent those claims. Comparisons must specify model, pinned provider, prompt/output budget, load conditions, sample size, and retry policy; these measurements do not isolate model compute speed. A socket timeout is not a guaranteed total wall-time deadline for all retries.

## Usage accounting

The source is the provider's OpenRouter `usage` object, recorded for each attempt, including HTTP error bodies or invalid/missing-content responses when usage is actually supplied.

| Summary field | Provider field |
| --- | --- |
| `prompt_tokens` | `usage.prompt_tokens` |
| `completion_tokens` | `usage.completion_tokens` |
| `total_tokens` | `usage.total_tokens` |
| `cost` | `usage.cost` (USD as reported by OpenRouter) |
| `reasoning_tokens` | `usage.completion_tokens_details.reasoning_tokens` |
| `cached_prompt_tokens` | `usage.prompt_tokens_details.cached_tokens` |
| `cache_write_tokens` | `usage.prompt_tokens_details.cache_write_tokens` |

For each field, summaries expose `reported_sum`, `reported_attempts`, `missing_attempts`, and `complete`. An explicit zero is a known value. Missing/null/invalid/negative/nonfinite values are unknown. No missing value is imputed as zero. `complete` requires at least one attempt and coverage for every recorded attempt. Partial runs still have unrun problems, regardless of their usage coverage.

Reasoning and cache details are subdivisions, **not extra tokens to add to total**. The reported total is preserved rather than silently reconstructed. `token_consistency` counts attempts on which prompt + completion differs from the reported total; it flags the discrepancy without modifying the original numbers. Raw usage can contain other provider-specific fields and is retained.

A missing reasoning count does not mean reasoning is disabled or unsupported. A reported zero means zero reported reasoning tokens for that attempt, not a model capability claim. This implementation leaves reasoning settings to provider defaults and records `reasoning_policy=provider_default`. Once models/policies are selected, any explicit changes must be separately configured and documented before final evaluation. No reasoning budget or mode is silently enabled to fill missing data.

`total_usage` combines all translation/solving attempts. `telemetry.translation` and `telemetry.solving` separate them. Metadata's final `usage` and its final attempt's usage refer to the same response; summaries read the attempt ledger and **do not count both**. A recovery summary includes the original attempt history plus new recovery attempts; locally replayed translation is never billed/counted again. For run totals after recovery, use the recovery summary alone, not parent + recovery totals.

`cost_per_correct_reported_usd` divides known reported costs by correct problems; it is partial when coverage is incomplete and null when no correct problems or no reported costs exist. Unknown usage/charges on failed requests cannot be recovered by assuming zero. Tokenizer differences also limit interpreting raw token counts as a model-independent unit of work.

## Old artifacts and persistence limits

Schema 2 applies to newly generated records. Original benchmark files and historical smoke artifacts are not rewritten. Offline re-scoring can summarize old records, but cannot recreate their missing per-attempt timings or usage. For a legacy call with several attempts, the final known usage is counted once and earlier attempts remain explicitly unknown; `legacy_attempts` and missing-timing counts expose that gap. Legacy problem durations lacking the new boundaries are not invented or relabeled.

Records are persisted when a problem finishes, as in the existing runner. An interruption before that record is written can leave in-flight attempts unrecorded; no billing completeness claim is made for such a window. This change does not introduce a transactional provider billing ledger. Attempt traces never contain request authentication headers or API keys.

## Offline verification

Deterministic fake-clock and transport tests verify exact attempt times, JSON-processing boundaries, actual versus planned retry waits, exhausted retries, null-content evidence, HTTP-error usage, reasoning/cache subsets, coverage, token-total discrepancies, nonfinite values, legacy records, and recovery without duplicated translation usage or mislabeled cumulative latency. No paid requests or judge calls are made by these tests.

Schema migration: the ambiguous summary key `end_to_end_latency_seconds` is replaced by `problem_timing`. Usage-summary `reported_calls`/`missing_calls` are replaced by `reported_attempts`/`missing_attempts`; `calls` still counts logical requests and `attempts` counts actual/known historical attempt slots. The old mixed usage `latency_seconds` distribution is replaced by the explicitly partitioned `timing` object. Consumers must check `telemetry_schema_version` rather than assuming schema-1 field meanings.
