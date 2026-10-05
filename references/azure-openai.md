# Azure OpenAI contract

Use this reference when maintaining or debugging the runner.

## Runtime configuration and fixed request settings

- Endpoints: primary `AZURE_OPENAI_GPT6_ENDPOINT` (else
  `AZURE_OPENAI_ENDPOINT`), optional backup `AZURE_OPENAI_GPT6_BACKUP_ENDPOINT`,
  and fallback `AZURE_OPENAI_FALLBACK_ENDPOINT` (else `AZURE_OPENAI_ENDPOINT`,
  else the primary). CLI overrides are `--endpoint`, `--backup-endpoint`, and
  `--fallback-endpoint`; no resource URL is committed. Settings may also come
  from the private env file loaded by `cli()` (see CONFIGURATION.md); the
  process environment wins.
- Primary deployment: `AZURE_OPENAI_DEPLOYMENT`, defaulting to
  `gpt-6-astra`.
- Backup deployment: `AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT` or
  `--backup-deployment`, defaulting to the primary deployment name.
- Error/rate-limit fallbacks: the GPT-6 backup resource, then
  `AZURE_OPENAI_FALLBACK_DEPLOYMENTS` on the fallback resource (default
  `gpt-5.6-sol`, then `gpt-5.4-pro`; `none` disables them).
- API: Responses
- Sign-in methods: `entra`, `azure-key`, and `openai-key`, ordered by `--auth`
  or `DEEP_THINK_AUTH`; without either, the methods are detected (Azure when an
  Azure endpoint variable or endpoint option is set, as `azure-key,entra` when
  an Azure key is set and `entra` otherwise). OpenAI uses `OPENAI_API_KEY`,
  optional `OPENAI_BASE_URL`, and the chain `gpt-6-astra` then
  `OPENAI_FALLBACK_MODELS` (default `gpt-5.6-sol`, `gpt-5.4-pro`). Later
  providers' chains follow earlier ones. Journal `submitting` records include
  `provider` (absent means Azure; other non-provider values are invalid);
  recovery refuses jobs whose provider is not listed, and credentials are
  refused for the other provider's hosts and configured endpoints.
- Authentication: Azure Entra ID through
  `DefaultAzureCredential(process_timeout=60)` and
  `get_bearer_token_provider(..., "https://ai.azure.com/.default")`; the longer
  subprocess timeout accommodates slow Azure CLI token commands. Azure API keys
  (`azure-key`) are sent only in the `api-key` header (the SDK key is empty, so
  no `Authorization` header is sent). OpenAI keys use bearer auth. Several
  Azure methods share one `AuthFallbackClient` per resource, which moves to
  the next method after a credential error or HTTP 401/403.
- GPT-6 and GPT-5.6 reasoning: `{"mode": "pro", "effort": "max",
  "context": "all_turns", "summary": "auto"}`
- GPT-5.4 Pro fallback reasoning: `{"effort": "xhigh",
  "summary": "auto"}`; mode and all-turns context are not configurable there.
- Storage: `store=False`
- Execution: `background=True`; poll every two seconds while status is
  `queued` or `in_progress`
- Output: `max_output_tokens=128000`, `text.verbosity=high`
- Truncation: disabled
- Client request timeout: 3,600 seconds
- SDK retries: disabled with `max_retries=0`; the runner owns retry policy

Pass the Entra bearer token provider callable to `OpenAI(api_key=...)`. Read
API keys only from environment variables; never accept them as command-line
arguments, and never log or persist them.

The endpoint must use HTTPS and include a host. The runner rejects endpoints
containing user information, arbitrary query parameters, or fragments. The
only query exception is a single date-formatted `api-version` on a full
`/openai/responses` URL. Normalize it to `/openai/` and pass the version as
the SDK's `default_query`, preserving it on both create and retrieve.
The fixed Entra scope above is a public protocol identifier, not a credential.

## Retry policy

By default, make one submission attempt per routed model, and at least five
(at most ten). Retry:

- Connection failures before the request is sent (`httpx.ConnectError`,
  `ConnectTimeout`, or `PoolTimeout` beneath the SDK exception).
- Malformed SDK responses after acceptance, including malformed HTTP 200
  payloads detected by explicit application-level validation before the
  response leaves the retry loop.
- Empty completed responses.
- HTTP 408, 409, 429, and non-gateway 5xx responses.
- Submission-level HTTP 404 with exact code `DeploymentNotFound` (Azure), or
  HTTP 403/404 with code `model_not_found` (OpenAI, including a project without
  access to the model).
- Azure `server_error`, `too_many_requests`, `rate_limit_exceeded`,
  `no_capacity`, `timeout`, and `temporarily_unavailable` response codes.

Do not retry an ambiguous submission. Record `submission_unknown` and stop for:
read/write timeouts and disconnects after sending, gateway HTTP 502/504,
interrupts during submission, and malformed success responses without a
readable response ID. Azure documents no `Idempotency-Key` or client request
correlation for Responses, and the OpenAI Python SDK sends no idempotency
header, so a replacement could run the same paid request twice. Resolve it only
with telemetry (`reconcile --attempt ATTEMPT --response-id ID`) or explicit
operator confirmation (`reconcile --confirm-no-remote-job --reason TEXT`).
Mandatory journaling cannot close the window in which Azure accepts a request
but the client never receives its ID.

On every retryable submission failure, advance through the primary GPT-6
resource, the optional backup GPT-6 resource, the configured fallback
deployments, and then the next listed provider's chain; cycle to the primary
only if the attempt budget permits. A credential error or HTTP 401/403 skips
straight to the next listed provider when every method of the current one has
failed; with no other provider it is terminal.
A new logical request always starts on its primary, including rollover and
visible-transcript summaries. Apply failover before acceptance or after a
terminal transient/malformed/empty response. Transient failures while polling
retry retrieval of the same response ID on the same client/resource, never a
new job. Exhausted polling retries stop explicitly rather than resubmitting.
Retry diagnostics identify the next deployment and target ordinal.

Submit long-running responses in background mode.  After the initial request
returns an ID, retry transient polling failures against that ID rather than
creating a duplicate response.  Treat `completed`, `failed`, `cancelled`, and
`incomplete` as terminal states and pass them through the ordinary validation
and recovery policy.

Use a monotonic 3,600-second deadline for each accepted job, configurable with
`--poll-timeout`. Successful polls do not refresh it. Cap sleeps, retry delays,
and each SDK retrieval timeout to the remaining budget. Check the deadline
between operations; in-flight credential/transport phases can finish later.
On deadline or retrieval exhaustion, stop with the original response ID,
deployment, and target ordinal; never cancel or resubmit automatically.
The budget applies separately to answer, rollover, and chunk-summary jobs.

For every retryable terminal response, report its ID, deployment, target,
output budget, canonical request byte length and SHA-256, and the service error
message. These diagnostics do not log the request content or authentication
headers. A response-object `server_error` is distinct from an HTTP 500.

Honor `x-should-retry`, `Retry-After`, and `retry-after-ms`, except that HTTP
429 remains retryable even if `x-should-retry` is `false` so deployment
failover and same-ID polling recovery cannot be disabled. Otherwise use
exponential backoff with bounded jitter. Never retry refusals, ordinary bad
requests, or other permanent 4xx errors, even when `x-should-retry` is `true`.
Sign-in failures (a credential error, or HTTP 401/403 other than
`model_not_found`) are not retried on the same method; they move to the next
listed `--auth` method, then to another listed provider that has not failed
sign-in (wrapping around), and are otherwise terminal. The exact
submission-level `DeploymentNotFound` 404 and `model_not_found` 403/404 are
target-specific and allow failover. Clients never follow HTTP redirects, so
headers such as `api-key` cannot be forwarded to another host.
Persist only the final complete response.

## Context policy

GPT-5.6 Sol has a 1,050,000-token context window, a 922,000-token maximum input,
and a 128,000-token maximum output. Reasoning tokens consume the same context
window and count as output tokens.

The GPT-6 upgrade retains these local budgets rather than assuming a larger
context window. GPT-6-specific limits are not independently established here;
service-side context errors still follow the bounded recovery policy.
Existing projects whose primary is a default or configured fallback deployment
can adopt the new default:
verify state and file checksums first, retain all history, record the primary
upgrade, and persist it only through the ordinary successful-turn commit.
When service-error recovery is explicitly enabled, a completed rollover is
also a persistence checkpoint, even if the following answer fails.

Persist every Responses output item locally, including encrypted reasoning
items, and replay them with the next user message. Use 900,000 tokens as the
volume ceiling, dynamically cap normal responses below it, and roll over before
the remaining response budget falls below 25,000 tokens. This leaves room for
the continuation-summary request and its output. Start a fresh local context
from that summary rather than relying on remote response retention. Do not
pay for a proactive rollover that cannot help: fail fast when even the smallest
possible carried summary would leave too little room for the prompt, or when
the same turn is retried from its own rollover checkpoint.

If Azure still returns a context error, force one rollover and replay the
uncommitted prompt exactly once. If opaque reasoning items cannot be replayed,
summarize the complete visible transcript instead. Treat the exact HTTP 400
code `invalid_encrypted_content` as the opaque replay signal only when the
request input actually contains an encrypted reasoning/compaction item; do not
trigger visible-transcript recovery for unrelated 400s. Process a visible
transcript that exceeds one request in 400,000-byte chunks and synthesize the
chunk summaries; never silently truncate it. A proactive rollover does not
consume the single reactive recovery allowance. Treat a second reactive context
rejection after rollover as terminal. Checkpoints record whether they followed
a proactive or reactive rollover, so the reactive allowance stays used when a
turn resumes from its reactive checkpoint. If the answer after a reactive
rollover fails deterministically (output or context limit, or invalid encrypted
context), keep the completed rollover so a narrower prompt can continue from
the new volume.

The optional `--recover-service-errors` policy allows a terminal
`status="failed"` / `error.code="server_error"` after bounded failover to trigger
one visible-transcript rollover for an existing volume. It also applies to the
ordinary full-context rollover-summary request. It does not handle submission
HTTP errors, polling failures, first turns, or rate limits alone. Once a fresh
volume exists, another service error stops instead of starting recovery again.
Use a 400,000-byte upper bound on the visible summary prompt, reducing larger
transcripts in 200,000-byte chunks. Keep the existing four-round reduction
limit and reject summaries that fail to reduce their input. Original volumes
remain intact; record that hidden reasoning was unavailable and that the
service-side cause is unknown. With this option enabled, persist completed
rollovers before attempting the answer so a later failure does not discard
that continuation or its usage accounting.

If a context-constrained answer exhausts its output budget, retry it after
rollover only when the fresh-volume `max_output_tokens` is strictly larger than
the exhausted original budget. Otherwise persist the successful rollover and
require the caller to split or narrow the request.

Use a per-project lock to prevent concurrent writers. Store SHA-256 checksums of
the current context, transcript, and canonicalized state body in `state.json`;
reject mismatches so an interrupted multi-file commit or manual edit cannot
silently corrupt continuation state. Current schema-version state files require
all three digests; accept truly legacy state files once and migrate them on the
next successful write.

## Request journal and recovery

Keep local writer state separate from remote request state:

- `.deep-think.lock` records `lock_id`, PID, host, process start time, creation
  time, and command. A lock whose process is gone (or whose PID now belongs to a
  newer process) is stale and is replaced atomically. A lock from another host
  or an unverifiable process requires `reconcile --release-lock`. Never probe
  liveness with `os.kill` on Windows; it terminates processes there.
- `requests/journal.jsonl` is append-only, one fsynced JSON record per event.
  Before every submission, record the logical and attempt request SHA-256,
  byte count, output budget, resource, deployment, and target ordinal. Record
  the response ID before polling, each status change, the terminal outcome,
  and the cached completed payload before the turn commits. A torn final line
  from a crash is discarded and recorded; a corrupt complete line blocks work.
- A dead writer does not prove its Azure job ended. Attempts with an intent but
  no outcome become `submission_unknown` when their stale lock is recovered. A
  stale pre-journal lock is recorded as an unresolved unknown writer.
- Treat the journal as untrusted input; it may arrive through a shared
  repository. Accept only the bare artifact names the runner writes
  (`<response-id>.json`, `<attempt-id>.response.json`, `<turn-id>.prompt.txt`)
  and require each resolved path to stay directly inside `requests/`; any other
  name marks the journal as corrupt. The CLI sends credentials only to
  endpoints configured for the job's provider: for Azure, the GPT-6, backup,
  and fallback endpoint variables, `AZURE_OPENAI_ENDPOINT`, or explicit
  endpoint options; for OpenAI, `OPENAI_BASE_URL` (default
  `https://api.openai.com/v1/`) or an explicit `--endpoint` with a
  single-provider `--auth` list. It refuses any other recorded resource and any
  endpoint configured for the other provider. An OpenAI 404 makes the attempt
  unknown (`remote_not_visible`) rather than finished, because OpenAI hides
  responses from other projects.

Each turn journals its prompt, deployment, and starting state digest. Re-running
the identical prompt, or `resume`, continues that turn: a matching request
fingerprint reuses a cached completion, polls an accepted ID on its original
resource without an initial wait, and refuses to continue past an unresolved
unknown submission. Failures that redirect the turn (output or context limits,
invalid encrypted context, and service errors followed by visible-transcript
recovery) are journaled and replayed without network calls, so the resumed
turn follows the same path to its running job. Before any new submission,
refuse if the turn still has a running job that the replay did not reach. A
different prompt may supersede an unfinished turn only when no running,
unknown, or completed-but-uncommitted request remains. Mid-turn checkpoints,
including rollovers kept after a prompt proves too large, record the new state
digest and mark completed results as persisted.

`status` reads the lock, committed state, and journal without writing. `cancel`
calls the background cancel endpoint on the original resource. Azure returns
HTTP 400 ("Cannot cancel a completed response") for finished jobs, so retrieve
the job after a failed cancel and record its actual state, caching completed
output for `resume`. `reconcile`
retrieves each active ID once, records terminal states, caches completed
results for `resume`, and treats HTTP 404 on the journaled original Azure
resource as no longer running. For OpenAI, a 404 cannot distinguish a deleted
response from one owned by another project, so the attempt becomes unknown
(`remote_not_visible`) instead. A refused endpoint, missing credential, or
failed sign-in is reported for that job alone; the other jobs are still
processed. An operator-attached ID must use the attempt's recorded
resource; if it returns 404 before any successful observation, the attachment
is rejected and the submission stays unknown. Background responses with
`store=false` are retained for roughly 10 minutes after completion, so later
results may be unrecoverable. A 404 for an ID outside the journal does not
resolve anything, because the resource may be wrong. A retired stale lock is
kept until its evidence is journaled, so a failed recovery cannot erase a
pre-journal writer.

## Authoritative documentation

Verified 2026-08-18:

- [Azure OpenAI Responses API](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses)
- [OpenAI Responses background mode](https://developers.openai.com/api/docs/guides/background)
- [Azure OpenAI reasoning models](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning)
- [GPT-5.6 Sol model limits](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- [GPT-5.6 pro mode and max effort](https://developers.openai.com/api/docs/guides/latest-model)
- [Reasoning context and summaries](https://developers.openai.com/api/docs/guides/reasoning)
- [Long-conversation compaction](https://developers.openai.com/api/docs/guides/compaction)
