# Results

Each run writes its own directory here. Generated runs are ignored by Git by default; no experimental results have been produced yet.

- `manifest.json`: configuration, prompt texts/hashes, code hashes, dataset fingerprint, selected IDs, timestamp, and Git revision.
- `predictions.jsonl`: one durable record per problem, including raw model content, request messages, parsed prediction, score, timing, and provider-reported usage.
- `summary.json`: paper metric and separate diagnostics, including incomplete-run status.
- `failures.jsonl`: incorrect or invalid predictions, including API failures.
- `reevaluated-summary.json`: optional offline re-scoring output.

Failed records are terminal within a run. Resume skips them along with successful records; changing the failure policy requires a new run, not overwriting the old one. A crash after a provider accepted a request but before its record was saved may cause a duplicate paid call on resume. A torn final JSONL record is rejected rather than silently discarded.

For publication, review complete runs for secrets and sensitive content, then explicitly add selected artifacts with `git add -f`. Do not publish the API key, environment files, or authentication headers. Do not present mock test outputs as benchmark results.
