# Azure OpenAI contract

Use this reference when maintaining or debugging the runner.

## Runtime configuration and fixed request settings

- Endpoints: primary `AZURE_OPENAI_GPT6_ENDPOINT`, backup
  `AZURE_OPENAI_GPT6_BACKUP_ENDPOINT`, and legacy `AZURE_OPENAI_ENDPOINT`
  (overridable by `AZURE_OPENAI_FALLBACK_ENDPOINT`). CLI overrides are
  `--endpoint`, `--backup-endpoint`, and `--fallback-endpoint`; no resource URL
  is committed. See CONFIGURATION.md for single-resource compatibility.
- Primary deployment: `AZURE_OPENAI_DEPLOYMENT`, defaulting to
  `gpt-6-astra`.
- Backup deployment: `AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT` or
  `--backup-deployment`, defaulting to the primary deployment name.
- Error/rate-limit fallbacks: GPT-6 backup resource, `gpt-5.6-sol`,
  `gpt-5.6-sol-nofilters`, then `gpt-5.4-pro` on the legacy resource.
- API: Responses
- Authentication: `DefaultAzureCredential` and
  `get_bearer_token_provider(..., "https://ai.azure.com/.default")`
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

Pass the bearer token provider callable to `OpenAI(api_key=...)`. Do not read,
accept, or persist API keys.

The endpoint must use HTTPS and include a host. The runner rejects endpoints
containing user information, arbitrary query parameters, or fragments. The
only query exception is a single date-formatted `api-version` on a full
`/openai/responses` URL. Normalize it to `/openai/` and pass the version as
the SDK's `default_query`, preserving it on both create and retrieve.
The fixed Entra scope above is a public protocol identifier, not a credential.

## Retry policy

Make at most five total submission attempts by default. Retry:

- Connection and timeout exceptions.
- Malformed SDK responses, including malformed HTTP 200 payloads detected by
  explicit application-level validation before the response leaves the retry
  loop.
- Empty completed responses.
- HTTP 408, 409, 429, and 5xx responses.
- Submission-level HTTP 404 with exact code `DeploymentNotFound`.
- Azure `server_error`, `too_many_requests`, `rate_limit_exceeded`,
  `no_capacity`, `timeout`, and `temporarily_unavailable` response codes.

On every retryable submission failure, advance through the primary GPT-6
resource, backup GPT-6 resource, `gpt-5.6-sol`, `gpt-5.6-sol-nofilters`, and
`gpt-5.4-pro`; cycle to the primary only if the attempt budget permits.
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
exponential backoff with bounded jitter. Never retry refusals, authentication
or authorization failures, ordinary bad requests, or other permanent 4xx
errors, even when `x-should-retry` is `true`. The exact submission-level
`DeploymentNotFound` exception is target-specific and allows failover.
Persist only the final complete response.

## Context policy

GPT-5.6 Sol has a 1,050,000-token context window, a 922,000-token maximum input,
and a 128,000-token maximum output. Reasoning tokens consume the same context
window and count as output tokens.

The GPT-6 upgrade retains these local budgets rather than assuming a larger
context window. GPT-6-specific limits are not independently established here;
service-side context errors still follow the bounded recovery policy.
Existing projects on built-in older deployments can adopt the new default:
verify state and file checksums first, retain all history, record the primary
upgrade, and persist it only through the ordinary successful-turn commit.
When service-error recovery is explicitly enabled, a completed rollover is
also a persistence checkpoint, even if the following answer fails.

Persist every Responses output item locally, including encrypted reasoning
items, and replay them with the next user message. Use 900,000 tokens as the
volume ceiling, dynamically cap normal responses below it, and roll over before
the remaining response budget falls below 25,000 tokens. This leaves room for
the continuation-summary request and its output. Start a fresh local context
from that summary rather than relying on remote response retention.

If Azure still returns a context error, force one rollover and replay the
uncommitted prompt exactly once. If opaque reasoning items cannot be replayed,
summarize the complete visible transcript instead. Treat the exact HTTP 400
code `invalid_encrypted_content` as the opaque replay signal only when the
request input actually contains an encrypted reasoning/compaction item; do not
trigger visible-transcript recovery for unrelated 400s. Process a visible
transcript that exceeds one request in 400,000-byte chunks and synthesize the
chunk summaries; never silently truncate it. A proactive rollover does not
consume the single reactive recovery allowance. Treat a second reactive context
rejection after rollover as terminal.

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

## Authoritative documentation

Verified 2026-08-18:

- [Azure OpenAI Responses API](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses)
- [OpenAI Responses background mode](https://developers.openai.com/api/docs/guides/background)
- [Azure OpenAI reasoning models](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning)
- [GPT-5.6 Sol model limits](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- [GPT-5.6 pro mode and max effort](https://developers.openai.com/api/docs/guides/latest-model)
- [Reasoning context and summaries](https://developers.openai.com/api/docs/guides/reasoning)
- [Long-conversation compaction](https://developers.openai.com/api/docs/guides/compaction)
