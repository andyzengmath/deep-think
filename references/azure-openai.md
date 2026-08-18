# Azure OpenAI contract

Use this reference when maintaining or debugging the runner.

## Runtime configuration and fixed request settings

- Endpoint: required from `AZURE_OPENAI_ENDPOINT` or `--endpoint`; no resource
  URL is committed.
- Deployment: `AZURE_OPENAI_DEPLOYMENT`, defaulting to `gpt-5.6-sol`.
- API: Responses
- Authentication: `DefaultAzureCredential` and
  `get_bearer_token_provider(..., "https://ai.azure.com/.default")`
- Reasoning: `{"mode": "pro", "effort": "max", "context": "all_turns",
  "summary": "auto"}`
- Storage: `store=False`
- Output: `max_output_tokens=128000`, `text.verbosity=high`
- Truncation: disabled
- SDK retries: disabled with `max_retries=0`; the runner owns retry policy

Pass the bearer token provider callable to `OpenAI(api_key=...)`. Do not read,
accept, or persist API keys.

The endpoint must use HTTPS and include a host. The runner rejects endpoints
containing user information, query parameters, or fragments so credentials
cannot be smuggled through the URL. The fixed Entra scope above is a public
protocol identifier, not a credential.

## Retry policy

Make at most three total attempts by default. Retry:

- Connection and timeout exceptions.
- Malformed SDK responses, including malformed HTTP 200 payloads detected by
  explicit application-level validation before the response leaves the retry
  loop.
- Empty completed responses.
- HTTP 408, 409, 429, and 5xx responses.
- Azure `server_error`, `too_many_requests`, `rate_limit_exceeded`,
  `no_capacity`, `timeout`, and `temporarily_unavailable` response codes.

Honor `x-should-retry`, `Retry-After`, and `retry-after-ms`. Otherwise use
exponential backoff with bounded jitter. Never retry refusals, authentication or
authorization failures, ordinary bad requests, or other permanent 4xx errors.
Persist only the final complete response.

## Context policy

GPT-5.6 Sol has a 1,050,000-token context window, a 922,000-token maximum input,
and a 128,000-token maximum output. Reasoning tokens consume the same context
window and count as output tokens.

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
- [Azure OpenAI reasoning models](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning)
- [GPT-5.6 Sol model limits](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- [GPT-5.6 pro mode and max effort](https://developers.openai.com/api/docs/guides/latest-model)
- [Reasoning context and summaries](https://developers.openai.com/api/docs/guides/reasoning)
- [Long-conversation compaction](https://developers.openai.com/api/docs/guides/compaction)
