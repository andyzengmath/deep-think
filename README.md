# Deep Think

`deep-think` is an agent skill for persistent mathematical research with Azure
OpenAI GPT-5.6 Sol. It is intended for difficult theorem proving, open-problem
investigation, counterexample search, novel constructions, and theory building.

The runner uses:

- Microsoft Entra ID authentication through `DefaultAzureCredential`
- Responses API `pro` mode with maximum reasoning effort
- A 1,050,000-token model context with conservative local budget enforcement
- Structured Markdown answers and version-controlled local transcripts
- Stateless encrypted-context replay and automatic summarized rollover
- Bounded retries, malformed-response validation, integrity checks, and
  visible-transcript recovery

No API keys are accepted or stored.

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
```

## Authenticate

Use Microsoft Entra ID locally:

```powershell
az login
$env:AZURE_OPENAI_ENDPOINT = "https://aoai-l-eastus2.services.ai.azure.com/openai/v1"
$env:AZURE_OPENAI_DEPLOYMENT = "gpt-5.6-sol"
```

Managed identity can replace `az login` in Azure environments.

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

## Validate

```powershell
python -B -m unittest discover `
  -s ".github\skills\deep-think\tests" `
  -p "test_*.py"
ruff check ".github\skills\deep-think"
ruff format --check ".github\skills\deep-think"
```

See [SKILL.md](SKILL.md) for the complete agent workflow and
[references/azure-openai.md](references/azure-openai.md) for the verified API
contract and context policy.
