# Deep Think

**Persistent, maximum-depth mathematical research for the coding agent you
already use.**

Deep Think is an [Agent Skill](https://agentskills.io) that lets GitHub Copilot
CLI, OpenAI Codex, Claude Code, opencode, pi, and other compatible agents hand
hard mathematics to GPT-6 Astra in Responses API `pro` mode at `max` reasoning
effort. It keeps a single investigation going for days, through new turns and
sessions, crashes, rate limits, and full context windows.

Use it for open problems, difficult proofs, counterexample searches, novel
constructions, and theory building.

## Why Deep Think

- **Maximum depth without babysitting.** Each question runs as a background
  job that can reason for up to an hour while Deep Think polls it for you.
- **A built-in backup plan.** If GPT-6 is rate-limited or failing, requests
  fall back to GPT-5.6 Sol and then GPT-5.4 Pro. You can also add a backup
  sign-in method or provider.
- **Never loses the thread.** Every project keeps a readable Markdown
  transcript plus the exact API context. When the context window fills, Deep
  Think writes a structured summary and continues in a fresh volume.
- **Crash-safe.** Every request is journaled before it is sent. After a crash,
  `resume` reattaches to the running job instead of paying for a duplicate.
- **Answers that show their work.** Results separate proofs from conjectures
  and list failure modes and open gaps, ready for the next turn or another
  agent.
- **Your account, your secrets.** Connect with an OpenAI API key or Azure
  OpenAI, using Microsoft Entra ID or an API key. Settings stay in your
  environment or a private local file. Keys are never logged or written to
  transcripts.

## Quick start

### 1. Install

One copy in `~/.agents/skills` serves Copilot CLI, Codex, opencode, and pi.
Link it into `~/.claude/skills` for Claude Code.

macOS or Linux:

```bash
git clone https://github.com/andyzengmath/deep-think.git ~/.agents/skills/deep-think
python3 -m pip install --upgrade -r ~/.agents/skills/deep-think/scripts/requirements.txt
mkdir -p ~/.claude/skills && ln -s ~/.agents/skills/deep-think ~/.claude/skills/deep-think   # Claude Code
```

Windows PowerShell:

```powershell
git clone https://github.com/andyzengmath/deep-think.git "$HOME\.agents\skills\deep-think"
python -m pip install --upgrade -r "$HOME\.agents\skills\deep-think\scripts\requirements.txt"
New-Item -ItemType Directory -Force "$HOME\.claude\skills" | Out-Null   # Claude Code
New-Item -ItemType Junction -Path "$HOME\.claude\skills\deep-think" -Target "$HOME\.agents\skills\deep-think"
```

Restart your agent or reload its skills. For other folders and project
installs, see [Where agents look for skills](#where-agents-look-for-skills).

### 2. Connect a model

Copy the settings template into your private configuration folder.

macOS or Linux:

```bash
mkdir -p ~/.config/deep-think
cp ~/.agents/skills/deep-think/.env.example ~/.config/deep-think/.env
chmod 600 ~/.config/deep-think/.env
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.config\deep-think" | Out-Null
Copy-Item "$HOME\.agents\skills\deep-think\.env.example" "$HOME\.config\deep-think\.env"
```

Open the copied `.env` file and uncomment one option:

| You have | Set | Sign-in |
| --- | --- | --- |
| An OpenAI API key | `OPENAI_API_KEY` | The key itself |
| An Azure OpenAI resource with `gpt-6-astra` deployed | `AZURE_OPENAI_ENDPOINT` | `az login` or a managed identity (Microsoft Entra ID), or `AZURE_OPENAI_API_KEY` |

Variables already set in your shell take precedence, so CI systems and secret
managers keep working. Never commit the filled-in file.

### 3. Ask

Ask your agent in plain language:

> Use deep-think to investigate the Erdős–Straus conjecture for primes
> p ≡ 1 (mod 24). Survey the known reductions, attempt a proof, and list the
> remaining gaps.

The agent runs the bundled runner, reads the structured answer, and continues
the same project in later turns. You can also run the runner yourself (on
Windows, use `python` and `$HOME\.agents\skills\...` paths):

```bash
python3 ~/.agents/skills/deep-think/scripts/deep_think.py ask \
  --project erdos-straus --title "Erdos-Straus hard residues" \
  --prompt "Survey the known reductions for primes congruent to 1 mod 24."
```

Answers and context are saved under `deep-think-transcripts/<project>/` in the
current folder.

## Sign-in methods and the backup plan

Deep Think detects one sign-in method automatically:

1. If an Azure endpoint is configured, it uses the **Azure API key** when one
   is set, and **Microsoft Entra ID** otherwise.
2. If not, it uses the **OpenAI API key** when `OPENAI_API_KEY` is set.

To choose a method explicitly or add backups, list methods in priority order
in your `.env` file, or pass the same list with `--auth`:

```dotenv
DEEP_THINK_AUTH=entra,azure-key,openai-key
```

Each request then follows one ordered chain, just like the model backup plan:

```text
1. entra        gpt-6-astra → gpt-6-astra on a backup resource (optional) → gpt-5.6-sol → gpt-5.4-pro
2. azure-key    the same Azure resources, used when Entra ID sign-in fails
3. openai-key   gpt-6-astra → gpt-5.6-sol → gpt-5.4-pro on the OpenAI API
```

- Rate limits, server errors, and missing deployments move a request to the
  next model. After the last Azure model, the next provider in the list takes
  over.
- A failed sign-in (no Entra ID token, or HTTP 401/403) switches to the next
  method immediately.
- Deep Think never sends a prompt to a provider you did not list.
- Change the model fallbacks with `AZURE_OPENAI_FALLBACK_DEPLOYMENTS` and
  `OPENAI_FALLBACK_MODELS`; `none` disables them.

GPT-6 Astra and GPT-5.6 Sol run in `pro` mode at `max` effort. GPT-5.4 Pro runs
at `xhigh`, its highest setting. Every new request starts at the top of the
chain. An accepted job always stays on the resource that accepted it.

## Long investigations

- **Continue** a project by reusing its slug; supply `--title` only on the
  first turn.
- **Context rollover** is automatic. Before a project's context reaches
  900,000 tokens (the window holds about 1 million), the model writes a
  structured continuation summary and the next volume starts from it. Earlier
  volumes stay on disk.
- **Recover** after a crash, a lock message, or a polling timeout:

  ```bash
  python3 ~/.agents/skills/deep-think/scripts/deep_think.py status --project erdos-straus
  python3 ~/.agents/skills/deep-think/scripts/deep_think.py resume --project erdos-straus
  ```

  `resume` keeps polling running jobs and commits cached results. `cancel`
  stops running jobs, and `reconcile` records remote status or an explicit
  decision. A request whose acceptance is unknown is never resubmitted
  automatically. Never delete the lock or journal by hand.
- **Plan multi-session research** with the proof-graph workflow in
  [references/graph-search-workflow.md](references/graph-search-workflow.md).
  It keeps a scope-aware AND/OR proof graph, selects the next action
  best-first, and records progress in
  `deep-think-transcripts/<project>/research-graph.json`.

## Where agents look for skills

| Agent | Personal skills | Project skills |
| --- | --- | --- |
| GitHub Copilot CLI | `~/.copilot/skills`, `~/.agents/skills` | `.github/skills`, `.agents/skills`, `.claude/skills` |
| OpenAI Codex | `~/.agents/skills` | `.agents/skills` |
| Claude Code | `~/.claude/skills` | `.claude/skills` |
| opencode | `~/.config/opencode/skills`, `~/.agents/skills`, `~/.claude/skills` | `.opencode/skills`, `.agents/skills`, `.claude/skills` |
| pi | `~/.pi/agent/skills`, `~/.agents/skills` | `.pi/skills`, `.agents/skills` |

To share the skill with a project, add it as a submodule in one of the project
folders above, for example
`git submodule add https://github.com/andyzengmath/deep-think.git .agents/skills/deep-think`.

So that agents use the skill without being asked, add this to the project's
`AGENTS.md` (Codex, Copilot, opencode, pi) or `CLAUDE.md` (Claude Code):

```markdown
For theorem proving, open-problem research, counterexample searches, difficult
mathematical constructions, or theory building, use the deep-think skill
(read its SKILL.md) before doing substantive mathematical reasoning.

For sustained research, follow the skill's `references/graph-search-workflow.md`.
Load the project's `research-graph.json` before continuing; update it after
each bounded research episode and before a handoff.
```

## Privacy and security

- Settings come only from your environment and your private env file:
  `~/.config/deep-think/.env`, or the path in `DEEP_THINK_ENV_FILE`. Shell
  variables win, and the file may set only `AZURE_*`, `OPENAI_*`, and
  `DEEP_THINK_*` variables.
- Keys are never accepted as command-line arguments, logged, or written to
  transcripts or journals. Azure keys are sent only in the `api-key` header.
- Credentials never cross providers. OpenAI keys are refused for Azure hosts,
  Azure credentials are refused for OpenAI hosts, and each journaled job is
  recovered only through the provider that accepted it.
- Transcripts and journals contain your prompts and answers. Review them
  before committing.

This repository contains no endpoint, key, tenant, or client identifier.

## Learn more

- [SKILL.md](SKILL.md): the workflow your agent follows.
- [CONFIGURATION.md](CONFIGURATION.md): every setting, Azure roles, managed
  identity, CI, and troubleshooting.
- [references/research-protocol.md](references/research-protocol.md): how to
  run a rigorous investigation.
- [references/azure-openai.md](references/azure-openai.md): the API contract,
  retry policy, and context budget.

## Develop

```bash
python -m pip install --upgrade -r scripts/requirements.txt ruff
python -B -m unittest discover -s tests -p "test_*.py"
ruff check . && ruff format --check .
```
