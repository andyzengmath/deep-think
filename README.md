# Deep Think

`deep-think` is an agent skill for persistent mathematical research with Azure
OpenAI GPT-6 Astra, with GPT-5.6 Sol and GPT-5.4 Pro backups. It is intended for
difficult theorem proving, open-problem investigation, counterexample search,
novel constructions, and theory building.

The runner uses:

- Microsoft Entra ID authentication through `DefaultAzureCredential`
- Responses API `pro` mode with maximum reasoning effort
- Background execution and polling for long-running reasoning requests
- Conservative local context budgets, retained across model failover
- Structured Markdown answers and version-controlled local transcripts
- Stateless encrypted-context replay and automatic summarized rollover
- Bounded retries, malformed-response validation, integrity checks, and
  visible-transcript recovery
- Ordered error/rate-limit failover across two `gpt-6-astra` resources,
  `gpt-5.6-sol`, `gpt-5.6-sol-nofilters`, and `gpt-5.4-pro`

No API keys are accepted or stored. The repository contains no Azure resource
endpoint, access token, client secret, tenant identifier, or client identifier.

## Install in a project

Add this repository as the project's skill directory:

```powershell
git submodule add https://github.com/andyzengmath/deep-think.git ".github\skills\deep-think"
git submodule update --init --recursive
python -m pip install --upgrade -r ".github\skills\deep-think\scripts\requirements.txt"
```

Add this instruction to the project's root `CLAUDE.md` so it applies to all
descendant directories:

```markdown
For theorem proving, open-problem research, counterexample searches, difficult
mathematical constructions, or theory building, read and follow
`.github\skills\deep-think\SKILL.md` before doing substantive mathematical
reasoning.

For sustained research, follow the skill's `references\graph-search-workflow.md`.
Load the project's `research-graph.json` before continuing; update it after
each bounded research episode and before a handoff.
```

## Configure and authenticate

The Azure resource endpoint is intentionally not committed. Configure it in
the current shell, then authenticate with Microsoft Entra ID:

```powershell
az login
$env:AZURE_OPENAI_GPT6_ENDPOINT = Read-Host "GPT-6 primary Responses URL (East US)"
$env:AZURE_OPENAI_GPT6_BACKUP_ENDPOINT = Read-Host "GPT-6 backup Responses URL (South Central US)"
$env:AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT = "gpt-6-astra-nofilters"
$env:AZURE_OPENAI_ENDPOINT = Read-Host "Existing GPT-5.6/GPT-5.4 v1 endpoint"
```

Use v1 base URLs or full `/openai/responses?api-version=2025-04-01-preview`
URLs. No other query parameters, embedded credentials, or fragments are accepted.
Managed identity can replace `az login` in Azure environments.

The default priority is GPT-6 East US, GPT-6 South Central US, then the existing
`gpt-5.6-sol`, `gpt-5.6-sol-nofilters`, and `gpt-5.4-pro` deployments.
GPT-6 and GPT-5.6 use `pro` mode and `max` effort. GPT-5.4 Pro uses `xhigh`
and omits unsupported reasoning mode/context settings. Five total attempts
allow the entire chain to be tried on retryable errors. Every new logical
request starts on the primary, including rollover summaries. Accepted
background jobs stay on their original resource while polling.

Polling has a one-hour per-job budget (`--poll-timeout`); expiry stops without
resubmitting a potentially running job. For repeated terminal `server_error`
failures, `--recover-service-errors` opts into one smaller, chunked
visible-transcript recovery. Original volumes are retained; this mitigates
failures without claiming their service-side cause is known.

Existing GPT-5.6/GPT-5.4 projects automatically adopt GPT-6 on their next
successful default turn without discarding history. Explicit `--deployment`
overrides remain supported. Remove an old `AZURE_OPENAI_DEPLOYMENT` override
or set it to `gpt-6-astra` to use the new default.

See [CONFIGURATION.md](CONFIGURATION.md) for role assignment, local shell,
managed identity, CI, validation, and troubleshooting instructions.

## Run

Start an investigation:

```powershell
python ".github\skills\deep-think\scripts\deep_think.py" ask `
  --project "project-slug" `
  --title "Research title" `
  --prompt-file "path\to\problem.md"
```

Continue it by reusing the project slug:

```powershell
python ".github\skills\deep-think\scripts\deep_think.py" ask `
  --project "project-slug" `
  --prompt "Audit the proposed proof and repair every gap."
```

Research records are written to `deep-think-transcripts\<project>\`. Review
their contents before committing them.

## Recover interrupted runs

Every Azure submission is journaled in
`deep-think-transcripts\<project>\requests\journal.jsonl` before it is sent,
and its response ID is saved before polling. After a crash, lock message, or
polling timeout, inspect the project and continue without duplicating paid
requests:

```powershell
python ".github\skills\deep-think\scripts\deep_think.py" status --project "project-slug"
python ".github\skills\deep-think\scripts\deep_think.py" resume --project "project-slug"
```

`resume` polls known jobs on their original resources and commits cached
results. `cancel` stops running jobs, and `reconcile` records remote status or
explicit decisions about ambiguous submissions. Requests whose acceptance is
unknown are never resubmitted automatically. Never delete the lock by hand.

## Persistent proof-strategy workflow

For sustained projects, use a scope-aware AND/OR proof graph with best-first
action selection and bounded DFS episodes. Keep the progress graph at
`deep-think-transcripts\<project>\research-graph.json`, separate from the
runner's protected `state.json` and context files.

- `references\graph-search-workflow.md` defines startup, action selection,
  proof promotion, stopping, and handoff rules.
- `references\research-graph.schema.json` defines the version-1 JSON format.
- `references\research-graph.template.json` is a minimal new-project record;
  replace its example mission and slug before using it.

The graph is maintained by the research agent, not automatically by the API
runner. It records mathematical claims, artifact gaps, evidence provenance,
and proposed actions without treating finite calculations as a solved root
problem. No extra database or scheduler is required.

## Validate

```powershell
python -B -m unittest discover `
  -s ".github\skills\deep-think\tests" `
  -p "test_*.py"
ruff check ".github\skills\deep-think"
ruff format --check ".github\skills\deep-think"
```

See [SKILL.md](SKILL.md) for the complete agent workflow and
[CONFIGURATION.md](CONFIGURATION.md) for secure setup, and
[references/azure-openai.md](references/azure-openai.md) for the verified API
contract and context policy.
