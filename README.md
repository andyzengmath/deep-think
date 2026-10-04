# Deep Think

`deep-think` is an agent skill for persistent mathematical research with
GPT-6 Astra, with GPT-5.6 Sol and GPT-5.4 Pro backups, through Azure OpenAI or
the OpenAI API. It is intended for difficult theorem proving, open-problem
investigation, counterexample search, novel constructions, and theory building.
It follows the open [Agent Skills](https://agentskills.io) format, so the same
folder works in GitHub Copilot CLI, OpenAI Codex, Claude Code, opencode, pi, and
other compatible agents.

The runner uses:

- Azure OpenAI with Microsoft Entra ID (default) or an API key, or OpenAI with
  an API key
- Responses API `pro` mode with maximum reasoning effort
- Background execution and polling for long-running reasoning requests
- Conservative local context budgets, retained across model failover
- Structured Markdown answers and version-controlled local transcripts
- Stateless encrypted-context replay and automatic summarized rollover
- Bounded retries, malformed-response validation, integrity checks, and
  visible-transcript recovery
- Ordered error/rate-limit failover: on Azure, two `gpt-6-astra` resources,
  `gpt-5.6-sol`, `gpt-5.6-sol-nofilters`, and `gpt-5.4-pro`; on OpenAI,
  `gpt-6-astra`, `gpt-5.6-sol`, and `gpt-5.4-pro`

Credentials come only from your environment. API keys are never accepted as
command-line arguments, logged, or written to transcripts or journals. The
repository contains no endpoint, key, token, tenant, or client identifier.

## Install for your agent

Clone the skill into a folder your agent scans, then install its dependencies.
One personal copy in `~/.agents/skills` serves Copilot CLI, Codex, opencode,
and pi; Claude Code reads `~/.claude/skills`.

| Agent | Personal skills | Project skills |
| --- | --- | --- |
| GitHub Copilot CLI | `~/.copilot/skills`, `~/.agents/skills` | `.github/skills`, `.agents/skills`, `.claude/skills` |
| OpenAI Codex | `~/.agents/skills` | `.agents/skills` |
| Claude Code | `~/.claude/skills` | `.claude/skills` |
| opencode | `~/.config/opencode/skills`, `~/.agents/skills`, `~/.claude/skills` | `.opencode/skills`, `.agents/skills`, `.claude/skills` |
| pi | `~/.pi/agent/skills`, `~/.agents/skills` | `.pi/skills`, `.agents/skills` |

macOS or Linux:

```bash
git clone https://github.com/andyzengmath/deep-think.git ~/.agents/skills/deep-think
ln -s ~/.agents/skills/deep-think ~/.claude/skills/deep-think   # Claude Code
python3 -m pip install --upgrade -r ~/.agents/skills/deep-think/scripts/requirements.txt
```

Windows PowerShell:

```powershell
git clone https://github.com/andyzengmath/deep-think.git "$HOME\.agents\skills\deep-think"
New-Item -ItemType Junction -Path "$HOME\.claude\skills\deep-think" -Target "$HOME\.agents\skills\deep-think"   # Claude Code
python -m pip install --upgrade -r "$HOME\.agents\skills\deep-think\scripts\requirements.txt"
```

Create the parent `skills` folder first if it does not exist. To share the
skill with a project instead, add it as a submodule in a project folder from the
table, for example `git submodule add https://github.com/andyzengmath/deep-think.git .agents/skills/deep-think`.
Restart the agent (or reload its skills) after installing.

To make agents use the skill for mathematics, add this to the project's
`AGENTS.md` (Codex, Copilot, opencode, pi) or `CLAUDE.md` (Claude Code),
adjusting the path to your installation:

```markdown
For theorem proving, open-problem research, counterexample searches, difficult
mathematical constructions, or theory building, use the deep-think skill
(read its SKILL.md) before doing substantive mathematical reasoning.

For sustained research, follow the skill's `references/graph-search-workflow.md`.
Load the project's `research-graph.json` before continuing; update it after
each bounded research episode and before a handoff.
```

## Configure and authenticate

Choose one provider. The runner selects Azure when an Azure endpoint variable
or endpoint option is set, otherwise OpenAI when `OPENAI_API_KEY` is set;
override with `--provider azure|openai` or `DEEP_THINK_PROVIDER`. Credentials
never cross providers: each job records its provider, and recovery commands act
on it only through that provider.

**OpenAI API key** (simplest):

```bash
export OPENAI_API_KEY=...        # PowerShell: $env:OPENAI_API_KEY = Read-Host "OpenAI API key"
# Optional: export OPENAI_BASE_URL=https://api.openai.com/v1/
```

**Azure OpenAI with an API key:** set the endpoints below and
`AZURE_OPENAI_API_KEY` (or per-resource `AZURE_OPENAI_GPT6_API_KEY`,
`AZURE_OPENAI_GPT6_BACKUP_API_KEY`, `AZURE_OPENAI_FALLBACK_API_KEY`). Key
authentication is selected automatically when a key is set, or explicitly with
`--azure-auth key`.

**Azure OpenAI with Microsoft Entra ID** (default for Azure, no keys): set the
endpoints, then sign in:

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
python "<skill-dir>/scripts/deep_think.py" ask `
  --project "project-slug" `
  --title "Research title" `
  --prompt-file "path\to\problem.md"
```

Continue it by reusing the project slug:

```powershell
python "<skill-dir>/scripts/deep_think.py" ask `
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
python "<skill-dir>/scripts/deep_think.py" status --project "project-slug"
python "<skill-dir>/scripts/deep_think.py" resume --project "project-slug"
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
  -s "<skill-dir>/tests" `
  -p "test_*.py"
ruff check "<skill-dir>"
ruff format --check "<skill-dir>"
```

See [SKILL.md](SKILL.md) for the complete agent workflow and
[CONFIGURATION.md](CONFIGURATION.md) for secure setup, and
[references/azure-openai.md](references/azure-openai.md) for the verified API
contract and context policy.
