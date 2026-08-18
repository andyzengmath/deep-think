# Secure configuration

`deep-think` uses Microsoft Entra ID through `DefaultAzureCredential`. It does
not accept API keys. Resource-specific values and identity credentials must be
supplied at runtime; none belong in this repository.

## Prerequisites

You need:

1. An Azure OpenAI resource with a Responses API deployment.
2. The resource's v1 endpoint and deployment name.
3. An Entra identity allowed to invoke that deployment. Assign the narrowest
   suitable data-plane role, normally **Cognitive Services OpenAI User**, at
   the resource or deployment scope.
4. Python dependencies installed from `scripts/requirements.txt`.

## Local PowerShell setup

Authenticate interactively, then enter the resource settings into the current
process environment:

```powershell
az login
$env:AZURE_OPENAI_ENDPOINT = Read-Host "Azure OpenAI v1 endpoint"
$env:AZURE_OPENAI_DEPLOYMENT = Read-Host "Azure OpenAI deployment name"
```

Copy the v1 endpoint from the Azure portal or your organization's deployment
output. Do not add a token, user information, query string, or fragment. The
runner requires HTTPS and normalizes the trailing slash.

The deployment variable is optional when its name is `gpt-5.6-sol`; setting it
explicitly makes the active deployment unambiguous.

The values above last only for the current PowerShell process. If you automate
them, use an operating-system, CI, or cloud configuration store outside the
repository. Do not create a tracked setup script or `.env` file.

## Managed identity

In Azure, enable a system-assigned or user-assigned managed identity and grant
it the required data-plane role. Configure `AZURE_OPENAI_ENDPOINT` and
`AZURE_OPENAI_DEPLOYMENT` in the service environment. `DefaultAzureCredential`
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

`--endpoint` can override the environment variable for an isolated run:

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
if (-not $env:AZURE_OPENAI_ENDPOINT) {
  throw "AZURE_OPENAI_ENDPOINT is not configured."
}
az account show --output none
python "scripts\deep_think.py" --help
```

When installed as a repository skill, prefix paths with
`.github\skills\deep-think\`.

## Troubleshooting

- **Missing endpoint:** set `AZURE_OPENAI_ENDPOINT` or pass `--endpoint`.
- **HTTP 401:** refresh `az login` or check the managed/workload identity.
- **HTTP 403:** verify the identity's data-plane role and resource scope.
- **Deployment not found:** set `AZURE_OPENAI_DEPLOYMENT` to the deployment
  name, not the base model name unless they are identical.
- **Credential chain selects the wrong account:** inspect `az account show`,
  choose the intended subscription, and log in again. Do not work around the
  issue by adding an API key.
