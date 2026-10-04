# Secure configuration

`deep-think` supports three ways to reach a model. All credentials and
resource-specific values are supplied at runtime through the environment; none
belong in this repository, a transcript, a journal, or a command line.

| Provider and auth | Required variables | Optional variables |
| --- | --- | --- |
| Azure OpenAI, Microsoft Entra ID (default for Azure) | Azure endpoint variables below; `az login`, managed identity, or workload identity | `DEEP_THINK_AZURE_AUTH=entra` |
| Azure OpenAI, API key | Azure endpoint variables; `AZURE_OPENAI_API_KEY` | Per-resource keys `AZURE_OPENAI_GPT6_API_KEY`, `AZURE_OPENAI_GPT6_BACKUP_API_KEY`, `AZURE_OPENAI_FALLBACK_API_KEY`; `DEEP_THINK_AZURE_AUTH=key` |
| OpenAI, API key | `OPENAI_API_KEY` | `OPENAI_BASE_URL` (default `https://api.openai.com/v1/`), `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID` |

Selection: `--provider` or `DEEP_THINK_PROVIDER` wins; otherwise Azure is used
when an `--endpoint`, `--backup-endpoint`, or `--fallback-endpoint` option or
any Azure endpoint variable is set, else OpenAI when `OPENAI_API_KEY` is set.
For Azure, `--azure-auth` or `DEEP_THINK_AZURE_AUTH` wins; otherwise key
authentication is used when any Azure key variable is set, else Entra ID. A
per-resource key applies to the endpoint in its matching variable; any other
resource uses `AZURE_OPENAI_API_KEY`. Azure keys are sent only in the
`api-key` header; OpenAI keys use the standard bearer header.

Credentials never cross providers. OpenAI keys are refused for Azure hosts
(`*.azure.com`, `*.azure.us`, `*.azure.cn`), and Azure keys or Entra tokens are
refused for `openai.com` hosts. Each journaled job records its provider, and
`resume`, `cancel`, and `reconcile` act on it only through that provider.

On OpenAI the model chain is `gpt-6-astra`, `gpt-5.6-sol`, then
`gpt-5.4-pro` on one base URL; use `--deployment` to choose another primary
model. Prefer Entra ID where available. Store keys in an OS keychain, secret
manager, or CI secret, export them only into the process environment, and
rotate any key that is exposed.

## Prerequisites (Azure)

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

Five total submission attempts are allowed by default. Retryable service
errors, connection failures before the request is sent, malformed/empty
response errors, and submission-level `DeploymentNotFound` 404s advance to the
next target. Authentication, authorization, refusals, and ordinary invalid
requests remain terminal. Ambiguous submissions—read timeouts or disconnects
after sending, gateway HTTP 502/504, interruptions, or a success response
without a readable ID—stop as `submission_unknown` and are never resubmitted
automatically. GPT-6 and GPT-5.6 use `pro`/`max`; GPT-5.4 Pro uses `xhigh`
without unsupported reasoning mode/context options. Polling an accepted job
never changes resources or resubmits the job, even if polling retries are
exhausted.

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
corresponding resources for an isolated run. They select Azure unless
`--provider openai` is also given; with OpenAI, `--endpoint` replaces the base
URL, and the backup and fallback options do not apply:

```powershell
python "scripts\deep_think.py" ask `
  --endpoint (Read-Host "Azure OpenAI v1 endpoint") `
  --project "project-slug" `
  --prompt "Audit the construction."
```

Prefer the environment variable for routine use so the endpoint is not copied
into shell scripts or command history.

## Polling and service-error recovery

Each accepted background job has a **3,600-second polling budget**, configurable
with `--poll-timeout`. Successful `queued`/`in_progress` retrievals do not reset
the deadline. Poll intervals, retry delays, and SDK retrieval timeouts are
capped to the remaining budget. The deadline is checked between operations;
in-flight credential or transport phases can finish after it. This is a
per-job budget, not a deadline for the entire turn or all its summary requests.

On expiry or exhausted retrieval retries, the runner stops and reports the
response ID, deployment, and target ordinal. It does not cancel the remote job,
switch resources, or submit a replacement. The job may still be running: the
request journal keeps its ID and original resource, so continue polling it with
`resume` (or stop it with `cancel`) instead of starting another attempt.

For an existing project whose accepted jobs repeatedly finish with
`status="failed"` and `error.code="server_error"`, explicitly enable recovery:

```powershell
python "scripts\deep_think.py" ask `
  --project "project-slug" `
  --prompt "Continue the investigation." `
  --poll-timeout 7200 `
  --recover-service-errors
```

The normal ordered failover policy runs first. After the submission-attempt
budget ends with a terminal response-object `server_error`, this option permits
one visible-transcript rollover and one answer attempt in the fresh volume
(with its normal bounded failover). It also covers a failed ordinary rollover
summary. Recovery uses a 400,000-byte visible-input ceiling, splitting larger
sources into 200,000-byte chunks before synthesis; no source text is truncated.
Summary reduction remains bounded to four rounds.

Recovery is off by default because summaries cannot retain hidden reasoning or
guarantee every detail of exact replay. Original volumes remain on disk.
With this option enabled, completed rollovers are checkpointed before the
answer attempt, including their usage and any primary-model upgrade, even if
the answer later fails. No further service-error recovery is started after a
fresh volume has been produced. First turns, submission-level HTTP failures,
rate limits alone, refusals, and polling failures do not trigger this recovery.

Retryable terminal response diagnostics include the response ID, deployment,
target ordinal, output budget, canonical request byte count and SHA-256, and
the service's error message (including any support correlation ID).
Capture stderr when investigating failures; full prompts, encrypted context,
and credentials are not included in these added request diagnostics.
A `server_error` is not proof of context exhaustion. This recovery is a
mitigation, not a diagnosis or guarantee that Azure will accept the next request.

## Validate configuration

Confirm that the required setting exists without printing its value:

```powershell
if (-not $env:AZURE_OPENAI_GPT6_ENDPOINT -or -not $env:AZURE_OPENAI_GPT6_BACKUP_ENDPOINT) {
  throw "Both GPT-6 endpoints must be configured for regional failover."
}
az account show --output none
python "scripts\deep_think.py" --help
```

When installed as a skill, prefix paths with the skill folder, for example
`~/.agents/skills/deep-think/`.

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
- **Project is already locked:** run `deep_think.py status --project SLUG`. A
  lock held by a running process means wait. Locks left by dead processes are
  recovered automatically; a lock from another host needs `reconcile
  --release-lock` after you confirm that process exited. Never delete it.
- **Submission outcome is unknown:** Azure may have accepted the request without
  returning its ID. Search Azure telemetry using the attempt time, deployment,
  resource, and request SHA-256 shown by `status`, then run `reconcile --attempt
  ATTEMPT --response-id ID`, or `reconcile --confirm-no-remote-job --reason
  TEXT` once you have verified that no job remains active.
- **Unfinished turn blocks a new prompt:** run `resume` to finish it, `cancel`
  to stop running jobs, or `reconcile --abandon-turn --reason TEXT`.
- **Recorded resource is not a configured endpoint:** the journal names an
  endpoint that is not configured for the selected provider, so the runner will
  not send credentials to it. If the job used the other provider, rerun with
  that `--provider`. If the endpoint is legitimate for this provider (for
  example, you changed configuration while a job ran), pass it explicitly with
  `--endpoint`.
- **Job was submitted through the other provider:** `status` lists each job's
  `provider`, and its next steps include `--provider openai` for OpenAI jobs.
  Rerun `resume`, `cancel`, or `reconcile` with the provider named in the
  message.
- **Refusing to send credentials to another provider's host:** an OpenAI key
  was about to reach an Azure host, or Azure credentials an `openai.com` host.
  Correct `--provider`, the endpoint, or `OPENAI_BASE_URL`. For an Azure
  resource with a key, use `--provider azure --azure-auth key` and
  `AZURE_OPENAI_API_KEY`.
- **Request journal has an invalid artifact name:** the journal names a file
  outside its `requests` directory and may have been tampered with. Inspect it
  before continuing; the runner will not read or delete such paths.
- **Old model still selected:** clear an old `AZURE_OPENAI_DEPLOYMENT`
  override or set it to `gpt-6-astra`, then restart the terminal/agent.
- **Credential chain selects the wrong account:** inspect `az account show`,
  choose the intended subscription, and log in again.
- **An API key is required:** key authentication was selected (`--azure-auth
  key`, `DEEP_THINK_AZURE_AUTH=key`, or `--provider openai`) but the matching
  key variable is empty. Export it in the agent's environment and restart the
  agent; never paste keys into prompts or command arguments.
- **Wrong provider selected:** an Azure endpoint variable or endpoint option
  takes precedence over `OPENAI_API_KEY`. Set `DEEP_THINK_PROVIDER=openai` or
  pass `--provider openai`.
- **`AzureCliCredential: Failed to invoke the Azure CLI`:** check how long
  `az account get-access-token --scope https://ai.azure.com/.default --output
  none` takes. The runner allows developer-credential subprocesses 60 seconds
  (the Azure SDK default is 10). Authentication failures are recorded as not
  sent and are never retried against another target.
