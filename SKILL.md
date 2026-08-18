---
name: deep-think
description: "Run persistent, maximum-depth mathematical research through Azure OpenAI GPT-5.6 Sol with Microsoft Entra ID, Responses API pro mode, max reasoning effort, structured Markdown, local full-context replay, and automatic long-context rollover summaries. Use for open problems, difficult proofs, counterexample searches, novel constructions, deep theory development, or any long multi-turn math investigation that needs a roughly one-million-token context and version-controlled continuity."
---

# Deep Think

Delegate a hard mathematical investigation to GPT-5.6 Sol while preserving both
a human-readable transcript and the exact local API context needed for later
turns.

## Prepare

1. Read [references/research-protocol.md](references/research-protocol.md) before
   framing a new investigation.
2. Install the current SDKs if the runner reports missing dependencies:

   ```powershell
   python -m pip install --upgrade -r ".github\skills\deep-think\scripts\requirements.txt"
   ```

3. Authenticate with Microsoft Entra ID through `DefaultAzureCredential`. Use
   `az login` for local development or a managed identity in Azure. Never add an
   API key.
4. Keep the default endpoint and deployment unless the environment differs:

   ```powershell
   $env:AZURE_OPENAI_ENDPOINT = "https://aoai-l-eastus2.services.ai.azure.com/openai/v1"
   $env:AZURE_OPENAI_DEPLOYMENT = "gpt-5.6-sol"
   ```

Read [references/azure-openai.md](references/azure-openai.md) only when changing
the endpoint, authentication, model settings, or context policy.

## Run an investigation

Use one stable lowercase project slug for the entire investigation. Supply the
title only on the first turn.

```powershell
python ".github\skills\deep-think\scripts\deep_think.py" ask `
  --project "project-slug" `
  --title "Research title" `
  --prompt-file "path\to\problem.md"
```

Continue in the same context by reusing the project slug and omitting the title:

```powershell
python ".github\skills\deep-think\scripts\deep_think.py" ask `
  --project "project-slug" `
  --prompt "Audit the proposed proof of Lemma 4 and repair any gap."
```

Prefer `--prompt-file` for long statements, source excerpts, or LaTeX. The
runner prints the structured Markdown answer to stdout and the transcript path
to stderr.

## Retry and recover

Use the defaults unless the environment requires a different bounded policy:

```powershell
python ".github\skills\deep-think\scripts\deep_think.py" ask `
  --project "project-slug" `
  --prompt "Continue the proof audit." `
  --max-attempts 3 `
  --retry-base-delay 1 `
  --retry-max-delay 30
```

Allow retries for connection/timeouts, malformed responses, empty completed
responses, HTTP 408/409/429/5xx, and Azure transient response codes. Respect
`Retry-After`; otherwise use exponential backoff with jitter. Do not retry
authentication, authorization, ordinary validation errors, content refusals,
or other permanent 4xx failures. Validate every response field that later code
uses before leaving the application retry loop so malformed HTTP 200 payloads
cannot escape as late `TypeError` or `AttributeError` crashes.

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

## Continue rigorously

1. Read each answer and its listed gaps before choosing the next prompt.
2. Ask separate turns for construction, adversarial proof audit,
   counterexample search, and synthesis when the problem warrants them.
3. Treat `pro` mode and `max` effort as compute settings, not proof guarantees.
4. Require explicit epistemic labels and independent verification before
   claiming a theorem, construction, or open problem is settled.
5. Preserve the generated files. Do not hand-edit `state.json` or
   `*-context.json`.

## Preserve and roll over context

Find all records under:

```text
deep-think-transcripts/<project>/
|-- state.json
|-- 0001-transcript.md
|-- 0001-context.json
`-- ...
```

Commit these files when repository policy permits. The Markdown files are the
agent-readable research record; context files retain encrypted reasoning items
for exact stateless continuation and can be large.

Run only one writer per project. The runner creates `.deep-think.lock` while a
turn is active and verifies a deterministic `state.json` digest plus the
current context/transcript checksums before every continuation. Current state
files require all three digests; truly legacy state files are accepted once and
migrated on the next successful write. If a process crashes, remove a stale
lock only after confirming that no request for that project remains active.

Allow automatic rollover. The runner treats 900,000 tokens as a context ceiling,
caps each response to stay below it, and rolls over before fewer than 25,000
tokens remain for reasoning and output. It asks the current conversation for a
structured continuation summary, closes that Markdown volume, starts the next
volume, and seeds a fresh API context with the summary. This reserve is required
because the 1,050,000-token model window also has a 922,000-token input limit and
must contain reasoning and output tokens.

After bounded recovery is exhausted, stop on any authentication, API,
incomplete-response, state-integrity, or file error. Resolve the error
explicitly; never substitute a success-shaped result.
