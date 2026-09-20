# Secure configuration

`deep-think` uses Microsoft Entra ID through `DefaultAzureCredential`. It does
not accept API keys. Resource-specific values and identity credentials must be
supplied at runtime; none belong in this repository.

## Prerequisites

You need:

1. Two Azure OpenAI resources deploying `gpt-6-astra`, plus the existing
   GPT-5.6/GPT-5.4 resource.
2. Each resource's v1 base endpoint or full preview Responses URL.
3. An Entra identity allowed to invoke that deployment. Assign the narrowest
   suitable data-plane role, normally **Cognitive Services OpenAI User**, at
   the resource or deployment scope.
4. Python dependencies installed from `scripts/requirements.txt`.

## Local PowerShell setup

Authenticate interactively, then enter the resource settings into the current
process environment:

```powershell
az login
$env:AZURE_OPENAI_GPT6_ENDPOINT = Read-Host "GPT-6 primary Responses URL (East US)"
$env:AZURE_OPENAI_GPT6_BACKUP_ENDPOINT = Read-Host "GPT-6 backup Responses URL (South Central US)"
$env:AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT = "gpt-6-astra-nofilters"
$env:AZURE_OPENAI_ENDPOINT = Read-Host "Existing GPT-5.6/GPT-5.4 v1 endpoint"
```

Copy endpoints from the Azure portal or deployment output. The runner accepts
HTTPS v1 base URLs and full `/openai/responses?api-version=2025-04-01-preview`
URLs. For the latter it preserves `api-version` for both creation and polling,
without appending a second `responses` path. No other query parameters,
credentials, or fragments are accepted.

The primary deployment defaults to `gpt-6-astra`; an explicit `--deployment`
or `AZURE_OPENAI_DEPLOYMENT` overrides it. Remove an old deployment override
or set it to `gpt-6-astra` to activate the new default.

The ordered chain is:

1. `gpt-6-astra` on `AZURE_OPENAI_GPT6_ENDPOINT` (East US).
2. `gpt-6-astra` on `AZURE_OPENAI_GPT6_BACKUP_ENDPOINT` (South Central US).
3. `gpt-5.6-sol` on the existing resource.
4. `gpt-5.6-sol-nofilters` on the existing resource.
5. `gpt-5.4-pro` on the existing resource.

Both GPT-6 deployment names default to `gpt-6-astra`. If the backup resource
uses a different deployment name, set `AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT`
or pass `--backup-deployment`; the GPT-6 `pro`/`max` profile is unchanged.

Set `AZURE_OPENAI_FALLBACK_ENDPOINT` to override the existing resource
(`AZURE_OPENAI_ENDPOINT`). If neither is set, older models use the primary
resource. If no dedicated GPT-6 endpoint is set, `AZURE_OPENAI_ENDPOINT` also
serves as the primary. Omitting the backup endpoint selects single-resource
primary operation; configure both GPT-6 endpoints for the full five-target chain.
Explicit legacy `--deployment` selections use the legacy resource and start
at that model in the chain, without promoting back to GPT-6.

Five total submission attempts are allowed by default. Retryable service,
connection, timeout, malformed/empty response errors, and submission-level
`DeploymentNotFound` 404s advance to the next target. Authentication,
authorization, refusals, and ordinary invalid requests remain terminal.
GPT-6 and GPT-5.6 use `pro`/`max`; GPT-5.4 Pro uses `xhigh` without
unsupported reasoning mode/context options. Polling an accepted job never
changes resources or resubmits the job, even if polling retries are exhausted.

The values above last only for the current PowerShell process. If you automate
them, use an operating-system, CI, or cloud configuration store outside the
repository. Do not create a tracked setup script or `.env` file.

An already-running terminal or agent process does not inherit user-level
environment variables added later. Restart that terminal or agent after
persisting `AZURE_OPENAI_ENDPOINT`, or set it explicitly in the current
PowerShell session.

To persist the new endpoint settings for future Windows processes:

```powershell
[Environment]::SetEnvironmentVariable("AZURE_OPENAI_GPT6_ENDPOINT", $env:AZURE_OPENAI_GPT6_ENDPOINT, "User")
[Environment]::SetEnvironmentVariable("AZURE_OPENAI_GPT6_BACKUP_ENDPOINT", $env:AZURE_OPENAI_GPT6_BACKUP_ENDPOINT, "User")
[Environment]::SetEnvironmentVariable("AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT", $env:AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT, "User")
```

Keep the existing fallback endpoint configured as well. Restart terminals and
agents after changing user-level settings; existing processes do not reload them.

## Managed identity

In Azure, enable a system-assigned or user-assigned managed identity and grant
it the required data-plane role on every resource. Configure the endpoint
variables above in the service environment. `DefaultAzureCredential`
will select the managed identity automatically.

For a user-assigned managed identity, supply `AZURE_CLIENT_ID` through the
service configuration. A client ID identifies the managed identity; it is not
a secret, but it still should remain environment-specific rather than committed
here.

## CI and workload identity

Prefer workload identity federation or the CI platform's Azure login action.
Store endpoint and deployment values as protected environment configuration.
Let the platform obtain short-lived Entra tokens. Never:

- commit a token, API key, client secret, certificate, or credential file;
- put credentials in the endpoint URL;
- print access tokens in logs;
- commit `.env`, `*.local`, `*.key`, `*.pem`, or `*.pfx` files;
- commit generated transcripts without reviewing their prompts and outputs.

The repository `.gitignore` excludes common local configuration and credential
artifacts, but ignore rules are not a substitute for secret scanning.

## Command-line override

`--endpoint`, `--backup-endpoint`, and `--fallback-endpoint` override the
corresponding resources for an isolated run:

```powershell
python "scripts\deep_think.py" ask `
  --endpoint (Read-Host "Azure OpenAI v1 endpoint") `
  --project "project-slug" `
  --prompt "Audit the construction."
```

Prefer the environment variable for routine use so the endpoint is not copied
into shell scripts or command history.

## Validate configuration

Confirm that the required setting exists without printing its value:

```powershell
if (-not $env:AZURE_OPENAI_GPT6_ENDPOINT -or -not $env:AZURE_OPENAI_GPT6_BACKUP_ENDPOINT) {
  throw "Both GPT-6 endpoints must be configured for regional failover."
}
az account show --output none
python "scripts\deep_think.py" --help
```

When installed as a repository skill, prefix paths with
`.github\skills\deep-think\`.

## Troubleshooting

- **Missing endpoint:** set `AZURE_OPENAI_GPT6_ENDPOINT` or pass `--endpoint`.
- **Endpoint exists at user scope but the runner says it is missing:** restart
  the terminal or agent process so it inherits the updated environment.
- **HTTP 401:** refresh `az login` or check the managed/workload identity.
- **HTTP 403:** verify the identity's data-plane role and resource scope.
- **Deployment not found:** set `AZURE_OPENAI_DEPLOYMENT` to the deployment
  name, not the base model name unless they are identical. For the GPT-6
  backup, use `AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT` or `--backup-deployment`.
  An available model listing does not prove a deployment with that name exists.
- **HTTP 429 or transient errors:** the runner advances through the ordered
  chain above and starts the next logical request on the primary deployment.
- **Old model still selected:** clear an old `AZURE_OPENAI_DEPLOYMENT`
  override or set it to `gpt-6-astra`, then restart the terminal/agent.
- **Credential chain selects the wrong account:** inspect `az account show`,
  choose the intended subscription, and log in again. Do not work around the
  issue by adding an API key.
