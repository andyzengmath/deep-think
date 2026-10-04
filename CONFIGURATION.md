# Configuration

Deep Think reaches a model in one of three ways, called sign-in methods. You
can use one, or list several in priority order so that later methods back up
earlier ones.

| Method | Provider and sign-in | Required settings | Optional settings |
| --- | --- | --- | --- |
| `entra` | Azure OpenAI with Microsoft Entra ID | An Azure endpoint; `az login`, a managed identity, or workload identity | `AZURE_CLIENT_ID` for a user-assigned identity |
| `azure-key` | Azure OpenAI with an API key | An Azure endpoint and `AZURE_OPENAI_API_KEY` | Per-resource keys `AZURE_OPENAI_GPT6_API_KEY`, `AZURE_OPENAI_GPT6_BACKUP_API_KEY`, `AZURE_OPENAI_FALLBACK_API_KEY` |
| `openai-key` | OpenAI API with an API key | `OPENAI_API_KEY` | `OPENAI_BASE_URL` (default `https://api.openai.com/v1/`), `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID` |

Prefer Microsoft Entra ID where it is available: it needs no long-lived secret.

## Where settings come from

The runner reads the process environment, then fills in anything missing from
a private env file:

1. The file named by `DEEP_THINK_ENV_FILE`, which must exist. Set it to
   `NUL` (Windows) or `/dev/null` to skip env files entirely.
2. Otherwise `$XDG_CONFIG_HOME/deep-think/.env` when `XDG_CONFIG_HOME` is set,
   or `~/.config/deep-think/.env` (Windows: `%USERPROFILE%\.config\deep-think\.env`).
   A missing default file is ignored.

Start from [.env.example](.env.example). The file holds `NAME=value` lines;
`#` starts a comment, values may be quoted, and an optional `export ` prefix is
accepted. Only `AZURE_*`, `OPENAI_*`, and `DEEP_THINK_*` variables are allowed,
so the file cannot change `PATH` or other process settings. Variables already
set in the environment always win, which lets CI systems, secret managers, and
one-off shell overrides take precedence. Errors name the file, line, and
variable but never print a value.

Keep the file outside every repository, readable only by you (`chmod 600` on
macOS and Linux), and never commit a filled-in copy. An API key in the file is
only as safe as the file; a secret manager that exports variables into the
agent's environment is safer still.

## Choosing methods and backups

`--auth` or `DEEP_THINK_AUTH` takes a comma-separated list of distinct methods
in priority order, for example `entra,openai-key`. Without either, one method
is detected:

1. If an `--endpoint`, `--backup-endpoint`, or `--fallback-endpoint` option or
   an Azure endpoint variable is set: `azure-key` when an Azure key variable is
   set, otherwise `entra`.
2. Otherwise `openai-key` when `OPENAI_API_KEY` is set.
3. Otherwise `entra`, which then reports the missing endpoint.

With several methods, every request follows one ordered chain:

- Each provider's models are tried in turn, as described below. Retryable
  errors move to the next model; after the last model of one provider, the
  next provider in the list takes over.
- Several Azure methods share the same resources. If one cannot sign in
  (no Entra ID token, or HTTP 401/403), the next is used for that resource
  immediately, and a note is printed to stderr.
- If every method of a provider fails to sign in, the request moves straight
  to the next listed provider. Without one, the failure is final.
- Prompts go only to providers you list. Credentials never cross providers:
  OpenAI keys are refused for Azure hosts (`*.azure.com`, `*.azure.us`,
  `*.azure.cn`), and Azure keys or Entra ID tokens are refused for `openai.com`
  hosts.

Each journaled job records its provider. `resume`, `cancel`, and `reconcile`
contact a job only through a listed method of that provider; otherwise they
report which `--auth` value to use.

## Azure OpenAI

### Prerequisites

1. An Azure OpenAI resource with a `gpt-6-astra` deployment. Fallback
   deployments (`gpt-5.6-sol` and `gpt-5.4-pro` by default) are optional but
   recommended.
2. Its v1 base endpoint, such as `https://<resource>.openai.azure.com/openai/v1/`,
   or a full preview Responses URL such as
   `https://<resource>.openai.azure.com/openai/responses?api-version=2025-04-01-preview`.
   For preview URLs the runner keeps `api-version` for creation and polling.
   No other query parameters, embedded credentials, or fragments are accepted.
3. For Entra ID: an identity with a data-plane role on each resource, normally
   **Cognitive Services OpenAI User** at resource or deployment scope.
4. Python dependencies from `scripts/requirements.txt`.

A single resource needs only `AZURE_OPENAI_ENDPOINT`.

### Model chain

1. The primary deployment (`gpt-6-astra`, or `AZURE_OPENAI_DEPLOYMENT` /
   `--deployment`) on `AZURE_OPENAI_GPT6_ENDPOINT`, or else on
   `AZURE_OPENAI_ENDPOINT`.
2. Optionally the same model on `AZURE_OPENAI_GPT6_BACKUP_ENDPOINT`, for
   example in another region. Set `AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT` or
   `--backup-deployment` if its deployment name differs.
3. Each deployment in `AZURE_OPENAI_FALLBACK_DEPLOYMENTS` (default
   `gpt-5.6-sol,gpt-5.4-pro`; `none` disables fallbacks) on
   `AZURE_OPENAI_FALLBACK_ENDPOINT`, else `AZURE_OPENAI_ENDPOINT`, else the
   primary resource.

An explicit `--deployment` that names a fallback deployment uses the fallback
resource and starts at that point in the chain. Deployment names must exist
on the resource; a submission-level `DeploymentNotFound` moves on to the next
model.

## OpenAI API

Requests go to `OPENAI_BASE_URL` (default `https://api.openai.com/v1/`). The
chain is `gpt-6-astra` (or `--deployment`), then `OPENAI_FALLBACK_MODELS`
(default `gpt-5.6-sol,gpt-5.4-pro`; `none` disables fallbacks). The primary
model name applies to every listed method, so when you combine providers keep
Azure deployment names equal to the model names.

## Retries and attempts

Each logical request gets one submission attempt per model in its chain, and
at least five; `--max-attempts` sets a fixed number from 1 to 10. Retryable
service errors, connection failures before the request is sent, malformed or
empty responses, and submission-level `DeploymentNotFound` errors advance to
the next model. Refusals and ordinary invalid requests stop immediately; sign-in
failures stop unless another method is listed. Every new request starts at the
top of the chain.

Ambiguous submissions (read timeouts or disconnects after sending, gateway HTTP
502/504, interruptions, or a success response without a readable ID) stop as
`submission_unknown` and are never resubmitted automatically. Polling an
accepted job never changes resources or resubmits it.

GPT-6 Astra and GPT-5.6 Sol use `pro` mode and `max` effort. GPT-5.4 Pro uses
`xhigh` and omits reasoning mode and context options it does not support.

## Managed identity

In Azure, enable a system-assigned or user-assigned managed identity and grant
it the data-plane role on every resource. Configure the endpoint variables in
the service environment; `DefaultAzureCredential` selects the managed identity
automatically. For a user-assigned identity, also set `AZURE_CLIENT_ID`. A
client ID is not a secret, but it belongs in environment configuration rather
than in this repository.

## CI and workload identity

Prefer workload identity federation or the CI platform's Azure login action.
Store endpoints and deployment names as protected environment configuration,
and let the platform obtain short-lived Entra ID tokens. Never:

- commit a token, API key, client secret, certificate, or credential file;
- put credentials in an endpoint URL;
- print access tokens in logs;
- commit a filled-in `.env`, `*.local`, `*.key`, `*.pem`, or `*.pfx` file;
- commit generated transcripts without reviewing their prompts and outputs.

The repository `.gitignore` excludes common local configuration and credential
files, but ignore rules are not a substitute for secret scanning.

## Command-line overrides

`--endpoint` replaces the primary endpoint of the first listed provider: the
Azure primary resource, or the OpenAI base URL with `--auth openai-key`.
`--backup-endpoint` and `--fallback-endpoint` replace Azure resources only. An
endpoint option without `--auth` or `DEEP_THINK_AUTH` selects Azure.

```bash
python3 scripts/deep_think.py ask --auth openai-key \
  --endpoint https://api.openai.com/v1/ \
  --project project-slug --prompt "Audit the construction."
```

Prefer settings for routine use so endpoints stay out of shell history.

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

```bash
python3 scripts/deep_think.py ask --project project-slug \
  --prompt "Continue the investigation." \
  --poll-timeout 7200 --recover-service-errors
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
mitigation, not a diagnosis or guarantee that the service will accept the next
request.

## Upgrading older projects

Projects created with GPT-5.6 Sol or GPT-5.4 Pro as their primary model adopt
GPT-6 Astra on their next successful default turn without discarding history.
An explicit `--deployment` keeps an older primary. Remove an old
`AZURE_OPENAI_DEPLOYMENT` override, or set it to `gpt-6-astra`, to use the new
default.

## Validate configuration

Check which settings are present without printing their values:

```bash
python3 - <<'EOF'
import os
names = ["DEEP_THINK_AUTH", "OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
         "AZURE_OPENAI_GPT6_ENDPOINT", "AZURE_OPENAI_API_KEY"]
for name in names:
    print(name, "set" if os.getenv(name) else "-")
EOF
az account show --output none   # Entra ID only
python3 scripts/deep_think.py --help
```

These checks see the shell environment only; the runner also reads the env
file. When installed as a skill, prefix paths with the skill folder, for
example `~/.agents/skills/deep-think/`.

## Troubleshooting

- **Missing endpoint:** set `AZURE_OPENAI_ENDPOINT` (or
  `AZURE_OPENAI_GPT6_ENDPOINT`) in the env file or environment, or pass
  `--endpoint`. To use OpenAI instead, set `OPENAI_API_KEY` or
  `DEEP_THINK_AUTH=openai-key`.
- **A setting seems ignored:** a variable set in the shell overrides the env
  file. Check that `DEEP_THINK_ENV_FILE` is unset or points to the intended
  file, and restart the agent after changing user-level variables.
- **Env file error:** the message names the file, line, and variable. Use
  `NAME=value` lines and only `AZURE_*`, `OPENAI_*`, or `DEEP_THINK_*` names.
- **HTTP 401:** refresh `az login`, check the managed or workload identity, or
  check the API key.
- **HTTP 403:** verify the identity's data-plane role and resource scope.
- **Deployment not found:** set `AZURE_OPENAI_DEPLOYMENT` to the deployment
  name, not the base model name unless they are identical. For the GPT-6
  backup, use `AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT` or `--backup-deployment`;
  for older models, `AZURE_OPENAI_FALLBACK_DEPLOYMENTS`. An available model
  listing does not prove a deployment with that name exists.
- **HTTP 429 or transient errors:** the runner advances through the chain and
  starts the next request at the top.
- **Project is already locked:** run `deep_think.py status --project SLUG`. A
  lock held by a running process means wait. Locks left by dead processes are
  recovered automatically; a lock from another host needs `reconcile
  --release-lock` after you confirm that process exited. Never delete it.
- **Submission outcome is unknown:** the service may have accepted the request
  without returning its ID. Search Azure telemetry or the OpenAI dashboard logs
  using the attempt time, deployment, resource, and request SHA-256 shown by
  `status`, then run `reconcile --attempt ATTEMPT --response-id ID`, or
  `reconcile --confirm-no-remote-job --reason TEXT` once you have verified that
  no job remains active.
- **Unfinished turn blocks a new prompt:** run `resume` to finish it, `cancel`
  to stop running jobs, or `reconcile --abandon-turn --reason TEXT`.
- **Recorded resource is not a configured endpoint:** the journal names an
  endpoint that is not configured for that provider, so the runner will not send
  credentials to it. If the job used the other provider, rerun with the
  `--auth` value the message names. If the endpoint is legitimate (for example,
  you changed configuration while a job ran), pass it explicitly with
  `--endpoint`.
- **Job was submitted through another provider:** `status` lists each job's
  `provider`, and its next steps include `--auth openai-key` for OpenAI jobs.
  Rerun `resume`, `cancel`, or `reconcile` with an `--auth` list that includes
  that provider.
- **Refusing to send credentials to another provider's host:** an OpenAI key
  was about to reach an Azure host, or Azure credentials an `openai.com` host.
  Correct `--auth`, the endpoint, or `OPENAI_BASE_URL`. For an Azure resource
  with a key, use `--auth azure-key` and `AZURE_OPENAI_API_KEY`.
- **Request journal has an invalid artifact name:** the journal names a file
  outside its `requests` directory and may have been tampered with. Inspect it
  before continuing; the runner will not read or delete such paths.
- **Old model still selected:** clear an old `AZURE_OPENAI_DEPLOYMENT`
  override or set it to `gpt-6-astra`, then restart the terminal or agent.
- **Credential chain selects the wrong account:** inspect `az account show`,
  choose the intended subscription, and log in again.
- **An API key is required:** `azure-key` or `openai-key` was selected but the
  matching key variable is empty. Add it to the env file or the agent's
  environment; never paste keys into prompts or command arguments.
- **Wrong method selected:** an Azure endpoint takes precedence over
  `OPENAI_API_KEY`, and an Azure key over Entra ID. Set `DEEP_THINK_AUTH` (for
  example `openai-key` or `entra`) or pass `--auth`.
- **`AzureCliCredential: Failed to invoke the Azure CLI`:** check how long
  `az account get-access-token --scope https://ai.azure.com/.default --output
  none` takes. The runner allows developer-credential subprocesses 60 seconds
  (the Azure SDK default is 10). Sign-in failures are recorded as not sent;
  they move to the next listed method, if any, and otherwise stop.
