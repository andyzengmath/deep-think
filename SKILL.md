---
name: deep-think
description: "Run persistent, maximum-depth mathematical research with GPT-6 Astra (GPT-5.6 Sol and GPT-5.4 Pro backups) through Azure OpenAI (Microsoft Entra ID or API key) or the OpenAI API, using Responses API pro mode, max reasoning effort, structured Markdown, local full-context replay, automatic long-context rollover summaries, and journaled crash recovery (status, resume, cancel, reconcile). Use for open problems, difficult proofs, counterexample searches, novel constructions, deep theory development, any long multi-turn math investigation needing version-controlled continuity, or recovering an interrupted, locked, or still-running deep-think request without duplicating paid work."
---

# Deep Think

Delegate a hard mathematical investigation to GPT-6 Astra while preserving both
a human-readable transcript and the exact local API context needed for later
turns.

## Prepare

Run bundled scripts from this skill's own folder, not the working repository.
In the commands below, `<skill-dir>` means that folder (for example
`~/.agents/skills/deep-think`, `~/.claude/skills/deep-think`, or a project's
`.agents/skills/deep-think`); use `python3` where `python` is unavailable. Keep
the working directory and transcript root in the research project. Model
credentials come only from the shell environment; your agent's own login does
not authenticate model requests.

1. Read [references/research-protocol.md](references/research-protocol.md) before
   starting or resuming an investigation.
   For sustained research, also follow
   `references/graph-search-workflow.md`: locate and read the project's
   `research-graph.json` before selecting another mathematical action.
   Initialize a new graph only for a genuinely new project, not because the
   current working directory changed.
2. Install the current SDKs if the runner reports missing dependencies:

   ```bash
   python -m pip install --upgrade -r "<skill-dir>/scripts/requirements.txt"
   ```

3. Confirm a provider is configured (see [CONFIGURATION.md](CONFIGURATION.md)).
   Never ask for, print, or write a key; keys stay in environment variables.
   - **OpenAI:** `OPENAI_API_KEY` (optional `OPENAI_BASE_URL`).
   - **Azure, API key:** the endpoints below plus `AZURE_OPENAI_API_KEY` or a
     per-resource key variable.
   - **Azure, Microsoft Entra ID (default for Azure):** the endpoints below and
     `az login` or a managed identity.

   Azure endpoint variables have no committed default:

   ```powershell
   $env:AZURE_OPENAI_GPT6_ENDPOINT = Read-Host "GPT-6 primary Responses URL (East US)"
   $env:AZURE_OPENAI_GPT6_BACKUP_ENDPOINT = Read-Host "GPT-6 backup Responses URL (South Central US)"
   $env:AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT = "gpt-6-astra-nofilters"
   $env:AZURE_OPENAI_ENDPOINT = Read-Host "Existing GPT-5.6/GPT-5.4 v1 endpoint"
   ```

   Both GPT-6 names default to `gpt-6-astra`; use
   `AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT` for a differently named backup.
   Accept v1 base URLs or full preview Responses URLs with only an
   `api-version` query parameter. Override detection with
   `--provider azure|openai` and `--azure-auth entra|key`; an explicit
   `--endpoint` selects Azure unless `--provider openai` is also given.
   Remove stale `AZURE_OPENAI_DEPLOYMENT` overrides to use the new default.

Read [references/azure-openai.md](references/azure-openai.md) only when changing
the endpoint, authentication, model settings, or context policy.

## Run an investigation

Use one stable lowercase project slug for the entire investigation. Supply the
title only on the first turn.

```powershell
python "<skill-dir>/scripts/deep_think.py" ask `
  --project "project-slug" `
  --title "Research title" `
  --prompt-file "path\to\problem.md"
```

Continue in the same context by reusing the project slug and omitting the title:

```powershell
python "<skill-dir>/scripts/deep_think.py" ask `
  --project "project-slug" `
  --prompt "Audit the proposed proof of Lemma 4 and repair any gap."
```

Prefer `--prompt-file` for long statements, source excerpts, or LaTeX. The
runner prints the structured Markdown answer to stdout and the transcript path
to stderr.

## Retry and recover

Use the defaults unless the environment requires a different bounded policy:

```powershell
python "<skill-dir>/scripts/deep_think.py" ask `
  --project "project-slug" `
  --prompt "Continue the proof audit." `
  --max-attempts 5 `
  --retry-base-delay 1 `
  --retry-max-delay 30
```

Allow retries for connection failures before a request is sent, malformed
responses that still carry a response ID, empty completed responses, HTTP
408/409/429/500/503 and other non-gateway 5xx responses, and Azure transient
response codes. Respect `Retry-After`; otherwise use exponential backoff with
jitter. Do not retry authentication, authorization, ordinary validation errors,
content refusals, or other permanent 4xx failures. Validate every response field
that later code uses before leaving the application retry loop so malformed HTTP
200 payloads cannot escape as late `TypeError` or `AttributeError` crashes.

Never resubmit an ambiguous submission. Read timeouts, disconnects after
sending, gateway HTTP 502/504, interrupted submissions, and success responses
without a readable ID are recorded as `submission_unknown`: Azure may already be
running that request, and Azure documents no idempotency key for Responses.

For retryable submission failures or terminal transient response errors, route
bounded attempts in this order: `gpt-6-astra` primary resource,
`gpt-6-astra` backup resource, `gpt-5.6-sol`, `gpt-5.6-sol-nofilters`,
then `gpt-5.4-pro`. Also advance on a submission-level `DeploymentNotFound`
404; other permanent 4xx errors remain terminal. If attempts remain, cycle
back to the primary. Every new logical request starts on the primary.
Preserve `pro` mode and `max` effort on both GPT-6 and both GPT-5.6 targets. For the
5.4 fallback only, omit unsupported reasoning mode/context settings and use
`xhigh`, its maximum supported effort. Record the deployment and effective
reasoning profile in the transcript.

Normal and rollover requests run with Responses API background mode enabled.
Poll the returned response ID while its status is `queued` or `in_progress`.
Retry transient retrieval failures without resubmitting the original job; this
prevents duplicate maximum-effort requests and avoids long synchronous HTTP
timeouts. Never switch deployments while polling an already-created response.
Use `--poll-timeout` to set the per-job polling budget (default 3,600 seconds).
Expiry is terminal locally, not a cancellation: record the response ID and
target, and reconcile that job on its original resource before resubmitting.
SDK retrieval timeouts and waits use the remaining budget; in-flight credential
or transport phases can finish after the deadline.

After bounded failover ends with a terminal response-object `server_error`,
`--recover-service-errors` explicitly allows one visible-transcript recovery
for an existing volume. Do not enable it silently: summaries lose hidden
reasoning and are not exact replay. Use smaller 200,000-byte chunks when the
visible input exceeds 400,000 bytes, retaining the complete original volume.
Completed rollovers are checkpointed before attempting the answer when this
option is enabled. Never use service-error recovery for polling failures,
authentication errors, rate limits alone, submission-level HTTP errors, or
first turns; do not repeat it after a fresh volume has been produced.
Preserve terminal diagnostics, including response ID, target, request hash,
byte count, output budget, and service/support message. The internal Azure
failure remains undiagnosed; do not label it proven context exhaustion.

Allow one reactive recovery. If Azure rejects a committed context or a
context-constrained answer exhausts its output budget, summarize the last
committed volume and replay the prompt in a new volume. A proactive rollover
does not consume this one reactive recovery. If a context replay fails with the
exact HTTP 400 code `invalid_encrypted_content` while the request contains
stored encrypted reasoning items, skip opaque replay and recover directly from
the complete visible transcript. Do not use that fallback for unrelated 400s.
Summarize oversized visible transcripts in bounded chunks without silent
truncation. After an output-budget exhaustion, retry only when the fresh-volume
`max_output_tokens` is strictly larger than the exhausted original budget;
otherwise persist the successful rollover and split or narrow the request.
Resuming a turn never grants a second reactive recovery, and a rollover is never
paid for when it cannot make room for the prompt. If the answer after a
reactive rollover fails on its output or context limit, the rollover is kept:
continue with a narrower prompt.

## Recover interrupted requests

Every request is journaled automatically in
`deep-think-transcripts\<project>\requests\journal.jsonl`: the request
fingerprint, resource, and deployment before submission; the response ID,
fsynced, before polling; and each completed result before the turn commits.
Journaling is mandatory; if it cannot be written, nothing is submitted.

After an interruption, a lock message, or any error that says a job may still be
running, inspect the project first. `status` is read-only and safe while another
writer runs:

```powershell
$runner = "<skill-dir>/scripts/deep_think.py"
python $runner status --project "project-slug"
python $runner resume --project "project-slug"
python $runner cancel --project "project-slug"
python $runner reconcile --project "project-slug"
```

- `resume` finishes the unfinished turn from its journaled prompt. It polls
  known response IDs on their original resources, reuses cached completions,
  and never resubmits a known or ambiguous request. Re-running the identical
  `ask` behaves the same way.
- `cancel` stops active background jobs on their original resources. If a job
  has already finished, it records the result instead and caches completed
  output for `resume`.
- `reconcile` records each active job's current remote status and caches
  completed results for `resume`. Use `--attempt ATTEMPT --response-id ID` for
  an ID found in Azure telemetry, `--confirm-no-remote-job --reason TEXT` only
  after verifying that an unknown submission left no running job,
  `--abandon-turn --reason TEXT` to discard an unfinished turn with no running
  work, and `--release-lock` only for a lock whose owner cannot be verified.

A dead writer process does not prove its Azure job ended. Locks left by dead
processes are recovered automatically, but running, unknown, or completed but
uncommitted requests block a different prompt until resolved. A pre-journal
stale lock is recorded as an unresolved unknown submission. Never delete the
lock or journal by hand. Background responses stored with `store=false` are
retained only briefly after completion, so resume promptly; an HTTP 404 on the
original resource is recorded as no longer running, and its output is lost. The
journal contains prompts and resource endpoint names but no credentials; review
it before committing, as with transcripts. Treat journals from other people as
untrusted: the runner rejects artifact names that would leave `requests\` and
sends credentials only to endpoints configured for the selected provider. Each
job records its provider; recover it with that `--provider` (shown in
`status`). After changing endpoint configuration, pass an old recorded endpoint
explicitly with `--endpoint` to resume, cancel, or reconcile its job.

## Continue rigorously

1. Read each answer and its listed gaps before choosing the next prompt.
2. Ask separate turns for construction, adversarial proof audit,
   counterexample search, and synthesis when the problem warrants them.
3. Treat `pro` mode and `max` effort as compute settings, not proof guarantees.
4. Require explicit epistemic labels and independent verification before
   claiming a theorem, construction, or open problem is settled.
5. Preserve the generated files. Do not hand-edit `state.json` or
   `*-context.json`.

For sustained research, maintain
`deep-think-transcripts\<project>\research-graph.json` using
`references\research-graph.schema.json`. This is the agent-maintained search
record, separate from the runner's protected API state. Load it on resumption
and update it after each bounded episode and before every handoff.

Use scope-aware AND/OR obligations, best-first action selection, and bounded
DFS episodes. Track mathematical status separately from artifact readiness
and running jobs. Repeated finite-order repairs require an abstraction
checkpoint; completed tasks never imply that the root problem is solved.
See `references\graph-search-workflow.md` for the selection, evidence, JSON
update, and recovery contract. The runner does not update this graph
automatically, and this workflow does not authorize additional spending.

## Preserve and roll over context

Find all records under:

```text
deep-think-transcripts/<project>/
|-- state.json
|-- research-graph.json
|-- requests/journal.jsonl
|-- 0001-transcript.md
|-- 0001-context.json
`-- ...
```

Commit these files when repository policy permits. The Markdown files are the
agent-readable research record; context files retain encrypted reasoning items
for exact stateless continuation and can be large.

Existing GPT-5.6/GPT-5.4 projects adopt GPT-6 on their next successful default
turn. Preserve history and checksums; let the runner record the upgrade.
Use an explicit `--deployment` to keep an older primary when required.

Run only one writer per project. The runner creates `.deep-think.lock` while a
turn is active and verifies a deterministic `state.json` digest plus the
current context/transcript checksums before every continuation. Current state
files require all three digests; truly legacy state files are accepted once and
migrated on the next successful write. Recover crashed writers with the
commands above, not by deleting the lock.

Allow automatic rollover. The runner treats 900,000 tokens as a context ceiling,
caps each response to stay below it, and rolls over before fewer than 25,000
tokens remain for reasoning and output. It asks the current conversation for a
structured continuation summary, closes that Markdown volume, starts the next
volume, and seeds a fresh API context with the summary. This reserve is required
because the retained shared budget reserves space for reasoning and output.
Do not infer GPT-6 deployment limits from the GPT-5.6 budget; service-side
context rejections still use the bounded recovery policy.

After bounded recovery is exhausted, stop on any authentication, API,
incomplete-response, state-integrity, or file error. Resolve the error
explicitly; never substitute a success-shaped result.
