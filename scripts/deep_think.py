#!/usr/bin/env python3

import argparse
import getpass
import hashlib
import json
import math
import os
import random
import re
import socket
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

DEEP_MATH_INSTRUCTIONS = """You are a deep mathematical research partner.
Work on open problems, difficult proofs, and complex theoretical constructions.
State assumptions precisely, separate established facts from conjectures, test
candidate arguments for hidden gaps, and return self-contained Markdown that
another agent can continue from. Never claim an open problem is solved without
a complete verifiable argument.

Use this structure:
# Deep Think Result
## Executive finding
## Formal setup
## Strategy
## Detailed analysis
## Verification and failure modes
## Open gaps
## Suggested continuation
## References
"""

ROLLOVER_SUMMARY_PROMPT = """Create the continuation summary for the next context volume.
Preserve every definition, hypothesis, established lemma, construction, useful
calculation, failed approach, counterexample, dependency, citation, unresolved
gap, and proposed next step needed to continue without the earlier transcript.
Distinguish proved statements from conjectures and tentative ideas. Use this
exact Markdown structure:
# Continuation Summary
## Problem and target
## Definitions and notation
## Established results
## Candidate constructions and arguments
## Rejected approaches and failure reasons
## Open gaps and risks
## Next steps
## References
"""

VISIBLE_TRANSCRIPT_SUMMARY_PROMPT = """The original full-context request could
not be used. Create the continuation summary from the complete
human-readable transcript below. Preserve every visible definition, hypothesis,
result, construction, failed approach, citation, unresolved gap, and next step.
State explicitly that hidden reasoning items were unavailable to this recovery
request. Use the same Continuation Summary Markdown structure.
"""

VISIBLE_TRANSCRIPT_CHUNK_PROMPT = """Summarize this numbered portion of a
deep-mathematics transcript for later synthesis. Preserve every visible
definition, hypothesis, proved or conjectural result, construction, failed
approach, citation, unresolved gap, and next step. Do not infer missing material.
Return concise structured Markdown headed `# Transcript Chunk Summary`.
"""

CARRIED_CONTEXT_PREFIX = """Continue the mathematical investigation using the
following summary of the preceding transcript volume. Treat it as working
context, not as unquestionable truth: preserve its distinctions between proved,
conjectural, and unresolved claims, and re-verify critical steps when needed.
"""

CONTEXT_WINDOW_TOKENS = 1_050_000
MAX_INPUT_TOKENS = 922_000
MAX_OUTPUT_TOKENS = 128_000
MIN_RESPONSE_TOKENS = 25_000
DEFAULT_ROLLOVER_TOKENS = 900_000
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_MAX_DELAY = 30.0
DEFAULT_REQUEST_TIMEOUT = 3600.0
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_POLL_TIMEOUT = 3600.0
# azure-identity's 10-second default is too short for some Azure CLI installs.
DEFAULT_CREDENTIAL_PROCESS_TIMEOUT = 60
DEFAULT_FALLBACK_DEPLOYMENTS = ("gpt-5.6-sol", "gpt-5.4-pro")
FALLBACK_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VISIBLE_SUMMARY_CHUNK_BYTES = 400_000
VISIBLE_CHUNK_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS
VISIBLE_SUMMARY_MAX_REDUCTION_ROUNDS = 4
DEFAULT_DEPLOYMENT = "gpt-6-astra"
ENTRA_SCOPE = "https://ai.azure.com/.default"
STATE_SCHEMA = "deep-think-state"
LEGACY_STATE_VERSION = 1
STATE_VERSION = 2
PROJECT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
LOCK_FILENAME = ".deep-think.lock"
LOCK_SCHEMA = "deep-think-lock/v2"
EMPTY_LOCK_GRACE_SECONDS = 60.0
PROCESS_START_TOLERANCE_SECONDS = 2.0
JOURNAL_DIRECTORY = "requests"
JOURNAL_FILENAME = "journal.jsonl"
JOURNAL_SCHEMA = "deep-think-request-journal/v1"
ACTIVE_RESPONSE_STATUSES = frozenset({"queued", "in_progress"})
TERMINAL_RESPONSE_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "incomplete"}
)
# Gateway errors can be returned after the service has accepted the request.
AMBIGUOUS_HTTP_STATUS_CODES = frozenset({502, 504})
OUTSTANDING_ATTEMPT_STATES = frozenset({"active", "unknown", "in_flight"})
RESPONSE_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
# Every artifact the runner writes under requests/ has one of these bare names.
ARTIFACT_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9_-]{1,200}(?:\.response\.json|\.json|\.prompt\.txt)$"
)
CONFIGURED_ENDPOINT_VARIABLES = (
    "AZURE_OPENAI_GPT6_ENDPOINT",
    "AZURE_OPENAI_GPT6_BACKUP_ENDPOINT",
    "AZURE_OPENAI_FALLBACK_ENDPOINT",
    "AZURE_OPENAI_ENDPOINT",
)
# Resource-specific Azure keys; AZURE_OPENAI_API_KEY covers any other resource.
AZURE_KEY_VARIABLES = {
    "AZURE_OPENAI_GPT6_ENDPOINT": "AZURE_OPENAI_GPT6_API_KEY",
    "AZURE_OPENAI_GPT6_BACKUP_ENDPOINT": "AZURE_OPENAI_GPT6_BACKUP_API_KEY",
    "AZURE_OPENAI_FALLBACK_ENDPOINT": "AZURE_OPENAI_FALLBACK_API_KEY",
}
AZURE_KEY_NAMES = ("AZURE_OPENAI_API_KEY", *AZURE_KEY_VARIABLES.values())
# Sign-in methods for --auth; listing several makes the later ones backups.
METHOD_PROVIDERS = {"entra": "azure", "azure-key": "azure", "openai-key": "openai"}
METHOD_LABELS = {
    "entra": "Microsoft Entra ID",
    "azure-key": "the Azure API key",
    "openai-key": "the OpenAI API key",
}
PROVIDER_LABELS = {"azure": "Azure OpenAI", "openai": "the OpenAI API"}
PROVIDER_AUTH_HINTS = {"azure": "entra (or azure-key)", "openai": "openai-key"}
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1/"
DEFAULT_OPENAI_FALLBACK_MODELS = DEFAULT_FALLBACK_DEPLOYMENTS
# Hosts that identify a provider; one provider's credentials never go to the other.
AZURE_HOST_SUFFIXES = (".azure.com", ".azure.us", ".azure.cn")
OPENAI_HOST = "openai.com"
ENDPOINT_OPTIONS = ("endpoint", "backup_endpoint", "fallback_endpoint")
ENV_FILE_VARIABLE = "DEEP_THINK_ENV_FILE"
# The local env file may configure only deep-think and its credentials.
ENV_FILE_PREFIXES = ("AZURE_", "OPENAI_", "DEEP_THINK_")
ENV_FILE_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
# Services such as OpenAI echo masked key fragments in error messages.
SECRET_FRAGMENT_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_*-]+")
# Env-file names are echoed in errors only when they look like variable names.
ENV_NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# A missing deployment or model moves to the next model instead of stopping.
MISSING_MODEL_CODES = frozenset({"DeploymentNotFound", "model_not_found"})
SERVICE_LABELS = {"azure": "Azure", "openai": "OpenAI"}
# Journal fields used as identifiers or destinations must be strings or null.
JOURNAL_TEXT_FIELDS = (
    "event",
    "attempt_id",
    "turn_id",
    "lock_id",
    "response_id",
    "resource",
    "provider",
    "deployment",
)


class DeepThinkError(RuntimeError):
    pass


class MissingCredentialError(DeepThinkError):
    """A sign-in method has no credential for the requested resource."""


class ContextLimitError(DeepThinkError):
    pass


class OutputLimitError(DeepThinkError):
    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


class MalformedResponseError(DeepThinkError):
    pass


class OpaqueReplayError(DeepThinkError):
    pass


class TerminalServiceError(DeepThinkError):
    pass


class RemoteStateError(DeepThinkError):
    pass


class SubmissionUnknownError(RemoteStateError):
    pass


class ProjectLockedError(DeepThinkError):
    pass


# Failures the turn pipeline recovers from by taking another path. They are
# journaled so a resumed turn can follow the same path without resubmitting.
REPLAYABLE_FAILURES = (
    OutputLimitError,
    ContextLimitError,
    OpaqueReplayError,
    TerminalServiceError,
)
# These describe the request itself, so the same request would fail again.
DETERMINISTIC_FAILURES = frozenset(
    {"OutputLimitError", "ContextLimitError", "OpaqueReplayError"}
)


class TurnResult:
    def __init__(self, text, volume, rolled_over, transcript_path):
        self.text = text
        self.volume = volume
        self.rolled_over = rolled_over
        self.transcript_path = transcript_path


class RetryEvent:
    def __init__(self, purpose, attempt, max_attempts, reason, delay):
        self.purpose = purpose
        self.attempt = attempt
        self.max_attempts = max_attempts
        self.reason = reason
        self.delay = delay


class RequestOutcome:
    def __init__(
        self, response, retry_count, deployment=None, *, intermediate_tokens=0
    ):
        self.response = response
        self.retry_count = retry_count
        self.deployment = deployment
        self.intermediate_tokens = intermediate_tokens


RETRYABLE_RESPONSE_CODES = {
    "server_error",
    "too_many_requests",
    "rate_limit_exceeded",
    "no_capacity",
    "timeout",
    "temporarily_unavailable",
}
CONTEXT_LIMIT_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "input_too_long",
}
MALFORMED_RESPONSE_EXCEPTIONS = (
    AttributeError,
    KeyError,
    TypeError,
    ValueError,
)
OPAQUE_REPLAY_CODE = "invalid_encrypted_content"


def _field(value, name):
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _response_member_container(value, label):
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple, str, bytes, bytearray, int, float, bool)):
        raise MalformedResponseError(f"{label} must be an object.")
    return value


def _response_field(value, name, label):
    container = _response_member_container(value, label)
    if container is None:
        return None
    return _optional_member(container, name, label)


def _optional_response_text(value, name, label):
    field = _response_field(value, name, label)
    if field is None:
        return None
    if not isinstance(field, str):
        raise MalformedResponseError(f"{label}.{name} must be a string.")
    return field


def _required_member(value, name, label):
    try:
        result = value[name] if isinstance(value, dict) else getattr(value, name)
    except MALFORMED_RESPONSE_EXCEPTIONS as error:
        raise MalformedResponseError(f"{label}.{name} is unavailable.") from error
    if result is None:
        raise MalformedResponseError(f"{label}.{name} is missing.")
    return result


def _optional_member(value, name, label):
    if value is None:
        return None
    try:
        return (
            value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        )
    except MALFORMED_RESPONSE_EXCEPTIONS as error:
        raise MalformedResponseError(f"{label}.{name} is unavailable.") from error


def _response_status(response):
    status = _required_member(response, "status", "response")
    if not isinstance(status, str) or not status:
        raise MalformedResponseError("response.status must be a non-empty string.")
    return status


def _response_text(response):
    text = _optional_member(response, "output_text", "response")
    if text is None:
        return ""
    if not isinstance(text, str):
        raise MalformedResponseError("response.output_text must be a string.")
    return text


def _response_id(response):
    response_id = _required_member(response, "id", "response")
    if not isinstance(response_id, str) or not response_id:
        raise MalformedResponseError("response.id must be a non-empty string.")
    return response_id


def _dump_output_item(item):
    if isinstance(item, dict):
        payload = item
    else:
        try:
            payload = item.model_dump(mode="json", exclude_none=True)
        except MALFORMED_RESPONSE_EXCEPTIONS as error:
            raise MalformedResponseError(
                "response.output item could not be serialized."
            ) from error
    if not isinstance(payload, dict):
        raise MalformedResponseError(
            "response.output items must serialize to JSON objects."
        )
    _response_content_items(payload)
    return payload


def _response_output_items(response):
    output = _required_member(response, "output", "response")
    if not isinstance(output, (list, tuple)):
        raise MalformedResponseError("response.output must be a list of items.")
    return output


def _response_error_details(response):
    error = _optional_member(response, "error", "response")
    code = _optional_response_text(error, "code", "response.error")
    error_type = _optional_response_text(error, "type", "response.error")
    message = _optional_response_text(error, "message", "response.error")
    details = _optional_member(response, "incomplete_details", "response")
    incomplete_reason = _optional_response_text(
        details,
        "reason",
        "response.incomplete_details",
    )
    codes = {
        value for value in (code, error_type, incomplete_reason) if value is not None
    }
    return codes, message or incomplete_reason or "unknown response error"


def _response_content_items(payload):
    content = payload.get("content")
    if content is None:
        return []
    if not isinstance(content, (list, tuple)):
        raise MalformedResponseError("response.output item.content must be a list.")
    for item in content:
        if not isinstance(item, dict):
            raise MalformedResponseError(
                "response.output item.content entries must be JSON objects."
            )
    return content


def _response_refusal_text(response):
    for item in _response_output_items(response):
        payload = _dump_output_item(item)
        for content in _response_content_items(payload):
            if content.get("type") == "refusal":
                return content.get("refusal") or content.get("text") or "refused"
    return None


def _status_error_details(error):
    status_code = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    details = body.get("error", body) if isinstance(body, dict) else {}
    code = details.get("code") or details.get("type")
    message = _redact_secrets(details.get("message") or str(error))
    return status_code, code, message


def _request_contains_encrypted_content(request):
    for item in request.get("input", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"reasoning", "compaction"} and item.get(
            "encrypted_content"
        ):
            return True
    return False


def _status_error_is_opaque_replay(error, request_has_encrypted_content):
    if not request_has_encrypted_content:
        return False
    status_code = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    details = body.get("error", body) if isinstance(body, dict) else {}
    return status_code == 400 and details.get("code") == OPAQUE_REPLAY_CODE


def _response_is_opaque_replay(response, request_has_encrypted_content):
    if not request_has_encrypted_content:
        return False
    error = _optional_member(response, "error", "response")
    return (
        _optional_response_text(error, "code", "response.error") == OPAQUE_REPLAY_CODE
    )


def _status_error_is_retryable(error):
    status_code, _, _ = _status_error_details(error)
    if status_code == 429:
        return True
    if (
        status_code is not None
        and 400 <= status_code < 500
        and status_code not in {408, 409}
    ):
        return False
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    directive = headers.get("x-should-retry")
    if directive == "true":
        return True
    if directive == "false":
        return False
    return status_code in {408, 409} or (status_code is not None and status_code >= 500)


def _status_error_is_context_limit(error):
    status_code, code, message = _status_error_details(error)
    if code in CONTEXT_LIMIT_CODES:
        return True
    if status_code != 400:
        return False
    normalized_message = message.lower()
    return any(
        marker in normalized_message
        for marker in (
            "context length",
            "context window",
            "maximum input token",
            "input is too long",
        )
    )


def _retry_after_seconds(error):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    milliseconds = headers.get("retry-after-ms")
    if milliseconds is not None:
        try:
            return max(0.0, float(milliseconds) / 1000)
        except ValueError:
            pass
    retry_after = headers.get("retry-after")
    if retry_after is None:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (retry_at - datetime.now(timezone.utc)).total_seconds(),
        )


def _retry_delay(error, attempt, base_delay, max_delay, random_value):
    requested_delay = _retry_after_seconds(error) if error is not None else None
    if requested_delay is not None:
        return min(max_delay, requested_delay)
    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return min(max_delay, delay + delay * 0.25 * random_value())


def _retry_event(purpose, attempt, max_attempts, reason, delay, on_retry):
    event = RetryEvent(
        purpose,
        attempt,
        max_attempts,
        reason,
        delay,
    )
    if on_retry is not None:
        on_retry(event)


def _deployment_chain(primary_deployment, fallbacks=DEFAULT_FALLBACK_DEPLOYMENTS):
    if primary_deployment in fallbacks:
        return tuple(fallbacks[fallbacks.index(primary_deployment) :])
    return (primary_deployment, *fallbacks)


def _uses_gpt54_profile(deployment):
    # Names such as gpt-5.4-pro-eu are GPT-5.4 deployments too.
    return isinstance(deployment, str) and deployment.startswith("gpt-5.4")


def _request_for_deployment(request, deployment):
    routed_request = {**request, "model": deployment}
    if not _uses_gpt54_profile(deployment):
        return routed_request

    reasoning = request.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning = dict(reasoning)
        reasoning.pop("mode", None)
        reasoning.pop("context", None)
        reasoning["effort"] = "xhigh"
        routed_request["reasoning"] = reasoning
    return routed_request


def _validate_openai_endpoint(endpoint):
    endpoint = (endpoint or OPENAI_DEFAULT_BASE_URL).strip()
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DeepThinkError(
            "OpenAI base URL must use HTTPS, include a host, and contain no "
            "credentials, query parameters, or fragments."
        )
    return endpoint.rstrip("/") + "/"


def _host_provider(endpoint):
    """Return the provider that owns a well-known endpoint host, or None."""
    try:
        host = (urlsplit(endpoint.strip()).hostname or "").rstrip(".")
    except (AttributeError, ValueError):
        raise DeepThinkError(
            "Endpoint host could not be parsed, so credentials will not be sent."
        ) from None
    if host.endswith(AZURE_HOST_SUFFIXES):
        return "azure"
    if host == OPENAI_HOST or host.endswith("." + OPENAI_HOST):
        return "openai"
    return None


def _refuse_cross_provider_host(provider, endpoint):
    """Never send one provider's credentials to the other provider's host."""
    owner = _host_provider(endpoint)
    host = urlsplit(endpoint.strip()).hostname
    if provider == "openai" and owner == "azure":
        raise DeepThinkError(
            f"Refusing to send OpenAI credentials to Azure host {host}. "
            "Use --auth entra or --auth azure-key for Azure OpenAI endpoints."
        )
    if provider == "azure" and owner == "openai":
        raise DeepThinkError(
            f"Refusing to send Azure credentials to OpenAI host {host}. "
            "Use --auth openai-key for the OpenAI API."
        )


def _import_openai():
    try:
        from openai import OpenAI
    except ImportError as error:
        raise DeepThinkError(
            "Install dependencies with: python -m pip install --upgrade openai"
        ) from error
    return OpenAI


def create_client(
    endpoint,
    *,
    auth="entra",
    api_key=None,
    credential_factory=None,
    token_provider_factory=None,
    openai_factory=None,
):
    """Create a Responses client.

    auth: "entra" (Azure, Microsoft Entra ID), "azure-key" (Azure ``api-key``
    header), or "openai-key" (OpenAI bearer key). Keys come only from callers;
    they are never logged or persisted.
    """
    if auth not in {"entra", "azure-key", "openai-key"}:
        raise DeepThinkError(f"Unknown auth mode {auth!r}.")
    if auth != "entra" and (not isinstance(api_key, str) or not api_key.strip()):
        variable = "AZURE_OPENAI_API_KEY" if auth == "azure-key" else "OPENAI_API_KEY"
        raise MissingCredentialError(
            f"An API key is required for {auth} authentication. Set {variable} "
            "(or a resource-specific key variable) in the environment."
        )
    if auth == "openai-key":
        base_url = _validate_openai_endpoint(endpoint)
        _refuse_cross_provider_host("openai", base_url)
        return (openai_factory or _import_openai())(
            base_url=base_url,
            api_key=api_key.strip(),
            max_retries=0,
            timeout=DEFAULT_REQUEST_TIMEOUT,
            **_sdk_options(openai_factory),
        )
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise DeepThinkError(
            "Azure OpenAI endpoint is required. Set AZURE_OPENAI_ENDPOINT "
            "or pass --endpoint."
        )
    endpoint = endpoint.strip()
    parsed_endpoint = urlsplit(endpoint)
    if parsed_endpoint.scheme != "https" or not parsed_endpoint.netloc:
        raise DeepThinkError("Azure OpenAI endpoint must use HTTPS and include a host.")
    _refuse_cross_provider_host("azure", endpoint)
    query = parse_qsl(parsed_endpoint.query, keep_blank_values=True)
    preview_endpoint = (
        parsed_endpoint.path.rstrip("/") == "/openai/responses"
        and len(query) == 1
        and query[0][0] == "api-version"
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-preview)?", query[0][1])
    )
    if (
        parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or (parsed_endpoint.query and not preview_endpoint)
        or parsed_endpoint.fragment
    ):
        raise DeepThinkError(
            "Azure OpenAI endpoint must not contain credentials, query parameters, "
            "or fragments, except api-version on a preview Responses URL."
        )
    client_options = {}
    if preview_endpoint:
        endpoint = urlunsplit(
            (parsed_endpoint.scheme, parsed_endpoint.netloc, "/openai/", "", "")
        )
        client_options["default_query"] = {"api-version": query[0][1]}

    normalized_endpoint = endpoint.rstrip("/") + "/"
    client_options.update(_sdk_options(openai_factory))
    if auth == "azure-key":
        # An empty SDK key suppresses the Authorization header, so Azure sees
        # only its documented api-key header.
        return _without_openai_account(
            (openai_factory or _import_openai())(
                base_url=normalized_endpoint,
                api_key="",
                default_headers={"api-key": api_key.strip()},
                max_retries=0,
                timeout=DEFAULT_REQUEST_TIMEOUT,
                **client_options,
            )
        )

    if credential_factory is None or token_provider_factory is None:
        try:
            from azure.identity import (
                DefaultAzureCredential,
                get_bearer_token_provider,
            )
        except ImportError as error:
            raise DeepThinkError(
                "Install dependencies with: "
                "python -m pip install --upgrade openai azure-identity"
            ) from error
        credential_factory = credential_factory or (
            lambda: DefaultAzureCredential(
                process_timeout=DEFAULT_CREDENTIAL_PROCESS_TIMEOUT
            )
        )
        token_provider_factory = token_provider_factory or get_bearer_token_provider
    openai_factory = openai_factory or _import_openai()

    token_provider = token_provider_factory(
        credential_factory(),
        ENTRA_SCOPE,
    )
    return _without_openai_account(
        openai_factory(
            base_url=normalized_endpoint,
            api_key=token_provider,
            max_retries=0,
            timeout=DEFAULT_REQUEST_TIMEOUT,
            **client_options,
        )
    )


def _sdk_options(openai_factory):
    """Production clients refuse redirects, so headers never reach another host."""
    if openai_factory is not None:
        return {}
    try:
        from openai import DefaultHttpxClient
    except ImportError as error:
        raise DeepThinkError(
            "Install dependencies with: python -m pip install --upgrade openai"
        ) from error
    # The Responses API never redirects; httpx would forward api-key headers.
    return {"http_client": DefaultHttpxClient(follow_redirects=False)}


def _without_openai_account(client):
    """Stop the SDK sending OPENAI_ORG_ID/OPENAI_PROJECT_ID headers to Azure."""
    for attribute in ("organization", "project"):
        if hasattr(client, attribute):
            setattr(client, attribute, None)
    return client


class RouteTarget:
    def __init__(self, deployment, client, resource=None, role=None, provider="azure"):
        self.deployment = deployment
        self.client = client
        self.resource = resource
        self.role = role
        self.provider = provider


class DeploymentRouter:
    def __init__(
        self,
        targets,
        *,
        client_factory=None,
        clients=None,
        provider="azure",
        client_factories=None,
    ):
        self.provider = provider
        self.targets = [
            target
            if isinstance(target, RouteTarget)
            else RouteTarget(*target, provider=provider)
            for target in targets
        ]
        self._client_factories = dict(client_factories or {})
        if client_factory is not None:
            self._client_factories.setdefault(provider, client_factory)
        self.providers = tuple(
            dict.fromkeys(
                [
                    provider,
                    *(target.provider for target in self.targets),
                    *self._client_factories,
                ]
            )
        )
        # Clients are keyed by (provider, resource) so credentials never mix.
        self._clients = dict(clients or {})

    def client_for(self, resource, provider=None):
        provider = provider or self.provider
        for target in self.targets:
            if target.provider == provider and target.resource == resource:
                return target.client
        key = (provider, resource)
        if key not in self._clients:
            factory = self._client_factories.get(provider)
            if factory is None:
                raise DeepThinkError(
                    f"No client is configured for the recorded resource {resource!r}."
                )
            self._clients[key] = factory(resource)
        return self._clients[key]


def create_routed_client(
    endpoint,
    deployment,
    *,
    backup_endpoint=None,
    backup_deployment=None,
    fallback_endpoint=None,
    client_factory=create_client,
    fallback_deployments=DEFAULT_FALLBACK_DEPLOYMENTS,
    provider="azure",
):
    clients = {}

    def client_for(resource):
        if (provider, resource) not in clients:
            clients[(provider, resource)] = client_factory(resource)
        return clients[(provider, resource)]

    targets = [
        RouteTarget(deployment, client_for(endpoint), endpoint, "primary", provider)
    ]
    if backup_endpoint:
        targets.append(
            RouteTarget(
                backup_deployment or deployment,
                client_for(backup_endpoint),
                backup_endpoint,
                "backup",
                provider,
            )
        )
    fallback_resource = fallback_endpoint or endpoint
    for fallback in _deployment_chain(deployment, fallback_deployments)[1:]:
        targets.append(
            RouteTarget(
                fallback,
                client_for(fallback_resource),
                fallback_resource,
                "fallback",
                provider,
            )
        )
    return DeploymentRouter(
        targets, client_factory=client_factory, clients=clients, provider=provider
    )


def _is_sign_in_failure(error):
    """True when the service refused a call because sign-in failed."""
    if _is_credential_error(error):
        return True
    status_code, code, _message = _status_error_details(error)
    # OpenAI reports a model the project cannot use as 403 model_not_found.
    return status_code in {401, 403} and code not in MISSING_MODEL_CODES


class AuthFallbackClient:
    """Azure client that moves to the next sign-in method when one fails.

    A failed token request or an HTTP 401/403 means the call was not accepted,
    so repeating it with the next method cannot duplicate remote work. Every
    method reaches the same Azure resource.
    """

    def __init__(self, clients, *, on_switch=None):
        self._clients = list(clients)
        self._active = 0
        self._on_switch = on_switch
        self.responses = _FallbackResponses(self)

    def _call(self, operation, *args, **kwargs):
        while True:
            method, client = self._clients[self._active]
            try:
                return getattr(client.responses, operation)(*args, **kwargs)
            except Exception as error:
                if self._active + 1 >= len(self._clients) or not _is_sign_in_failure(
                    error
                ):
                    raise
                self._active += 1
                if self._on_switch is not None:
                    self._on_switch(method, self._clients[self._active][0], error)


class _FallbackResponses:
    def __init__(self, owner):
        self._owner = owner

    def __getattr__(self, operation):
        return lambda *args, **kwargs: self._owner._call(operation, *args, **kwargs)


def _attempt_provider(attempt):
    # Journals written before provider support contain only Azure jobs; a
    # tampered value is reported as invalid instead of being trusted.
    provider = attempt.get("provider")
    if provider is None:
        return "azure"
    return (
        provider
        if isinstance(provider, str) and provider in PROVIDER_LABELS
        else "invalid"
    )


def _provider_mismatch(attempt, providers):
    """Explain why an attempt's provider is not among providers, or None."""
    recorded = _attempt_provider(attempt)
    if recorded in providers:
        return None
    hint = PROVIDER_AUTH_HINTS.get(recorded)
    return (
        f"{attempt.get('response_id') or attempt.get('attempt_id')} was submitted "
        f"through {PROVIDER_LABELS.get(recorded, repr(recorded))}, which this "
        "command is not set up to use, so no credentials will be sent for it; "
        + (f"rerun with --auth {hint}" if hint else "inspect the request journal")
    )


def _target_for_attempt(router, attempt):
    mismatch = _provider_mismatch(attempt, router.providers)
    if mismatch is not None:
        raise DeepThinkError(f"Recorded response {mismatch}.")
    recorded = _attempt_provider(attempt)
    for index, target in enumerate(router.targets):
        if (
            target.provider == recorded
            and target.deployment == attempt["deployment"]
            and target.resource == attempt["resource"]
        ):
            return index, target
    if attempt["resource"] is None:
        raise DeepThinkError(
            f"Recorded response {attempt['response_id']} has no resource and no "
            "matching configured target."
        )
    return None, RouteTarget(
        attempt["deployment"],
        router.client_for(attempt["resource"], recorded),
        attempt["resource"],
        attempt.get("role"),
        recorded,
    )


class SubmissionFailure:
    def __init__(
        self,
        kind,
        reason,
        *,
        http_status=None,
        code=None,
        message=None,
        response_id=None,
    ):
        self.kind = kind
        self.reason = reason
        self.http_status = http_status
        self.code = code
        self.message = message
        self.response_id = response_id


def _is_credential_error(error):
    try:
        from azure.core.exceptions import ClientAuthenticationError
    except ImportError:
        return False
    return isinstance(error, ClientAuthenticationError)


def _classify_submission_error(
    error, connection_error, validation_error, status_error, service="Azure"
):
    if isinstance(error, status_error):
        status_code, code, message = _status_error_details(error)
        if status_code in AMBIGUOUS_HTTP_STATUS_CODES:
            return SubmissionFailure(
                "unknown",
                f"gateway HTTP {status_code}; {service} may have accepted the request",
                http_status=status_code,
                code=code,
            )
        return SubmissionFailure(
            "rejected",
            f"{service} HTTP {status_code}",
            http_status=status_code,
            code=code,
            message=message,
        )
    if isinstance(error, validation_error):
        body = getattr(error, "body", None)
        response_id = body.get("id") if isinstance(body, dict) else None
        if isinstance(response_id, str) and response_id:
            return SubmissionFailure(
                "accepted",
                "malformed creation response with a response ID",
                response_id=response_id,
            )
        return SubmissionFailure(
            "unknown", f"{service} returned a success response without a readable ID"
        )
    if isinstance(error, connection_error):
        cause = error.__cause__
        try:
            import httpx

            never_sent = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
        except ImportError:
            never_sent = ()
        if never_sent and isinstance(cause, never_sent):
            return SubmissionFailure(
                "not_sent", f"{type(cause).__name__} before the request was sent"
            )
        detail = type(cause).__name__ if cause is not None else "unknown cause"
        return SubmissionFailure(
            "unknown",
            f"{type(error).__name__} ({detail}) after the request may have been sent",
        )
    if isinstance(error, Exception) and _is_credential_error(error):
        return SubmissionFailure("auth", "Azure authentication failed before sending")
    return SubmissionFailure("unknown", f"interrupted by {type(error).__name__}")


def _safe_response_status(response):
    try:
        return _response_status(response)
    except MalformedResponseError:
        return None


def _record_accepted(journal, attempt_id, response_id, status):
    try:
        journal.append(
            "accepted",
            attempt_id=attempt_id,
            response_id=response_id,
            status=status,
        )
    except DeepThinkError as error:
        raise DeepThinkError(
            f"Azure accepted response {response_id} (attempt {attempt_id}), but "
            f"the request journal could not record it: {error} The job may still "
            "be running. Once the journal is writable, run `reconcile --attempt "
            f"{attempt_id} --response-id {response_id}`."
        ) from error


def _submission_unknown_message(purpose, target, attempt_id, reason):
    if getattr(target, "provider", "azure") == "openai":
        where = (
            "OpenAI may already be running this request. Find it in the OpenAI "
            "dashboard logs"
        )
    else:
        where = "Azure may already be running this request. Find it in Azure telemetry"
    return (
        f"{purpose.capitalize()} submission outcome is unknown (attempt "
        f"{attempt_id}, deployment {target.deployment}, resource "
        f"{target.resource or 'configured client'}): {reason}. No replacement "
        "request was sent: the Responses API has no idempotency key, so "
        f"{where} and run `reconcile --attempt ATTEMPT --response-id ID`, or, "
        "after verifying that no job remains active, run `reconcile "
        "--confirm-no-remote-job --reason TEXT`."
    )


def _poll_background_response(
    client,
    response,
    *,
    purpose,
    max_attempts,
    base_delay,
    max_delay,
    poll_interval,
    poll_timeout,
    target_label,
    sleep,
    random_value,
    on_retry,
    on_status=None,
    initial_wait=True,
    service="Azure",
):
    try:
        from openai import (
            APIConnectionError,
            APIResponseValidationError,
            APIStatusError,
        )
    except ImportError as error:
        raise DeepThinkError(
            "Install dependencies with: "
            "python -m pip install --upgrade openai azure-identity"
        ) from error

    if _response_status(response) not in ACTIVE_RESPONSE_STATUSES:
        return response, 0
    response_id = _response_id(response)
    job = f"response {response_id}, {target_label}"
    deadline = time.monotonic() + poll_timeout
    last_status = _response_status(response)

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise DeepThinkError(
                f"{purpose.capitalize()} poll deadline exceeded after "
                f"{poll_timeout:g}s for {job}. The job may still be running; "
                "retrieve this ID on its original resource before resubmitting."
            )
        return seconds

    def wait(delay):
        sleep(min(delay, remaining()))
        remaining()

    retry_count = 0
    first_poll = True
    while _response_status(response) in ACTIVE_RESPONSE_STATUSES:
        if initial_wait or not first_poll:
            wait(poll_interval)
        first_poll = False
        for attempt in range(1, max_attempts + 1):
            try:
                retrieved = client.responses.retrieve(
                    response_id,
                    timeout=min(DEFAULT_REQUEST_TIMEOUT, remaining()),
                )
            except APIResponseValidationError as error:
                reason = f"malformed {service} response"
                retry_error = error
            except APIConnectionError as error:
                reason = type(error).__name__
                retry_error = error
            except APIStatusError as error:
                status_code, code, message = _status_error_details(error)
                reason = f"{service} HTTP {status_code}"
                if code:
                    reason += f" ({code})"
                if not _status_error_is_retryable(error):
                    raise DeepThinkError(
                        f"{purpose.capitalize()} poll failed without retry for {job}: "
                        f"{reason}: {message}"
                    ) from error
                retry_error = error
            else:
                try:
                    retrieved_id = _response_id(retrieved)
                    _response_status(retrieved)
                    if retrieved_id != response_id:
                        raise MalformedResponseError(
                            "Background response ID changed while polling."
                        )
                except MalformedResponseError as error:
                    reason = f"malformed {service} response"
                    retry_error = error
                else:
                    response = retrieved
                    retry_count += attempt - 1
                    status = _response_status(response)
                    if on_status is not None and status != last_status:
                        on_status(status)
                    last_status = status
                    break

            remaining()
            if attempt >= max_attempts:
                raise DeepThinkError(
                    f"{purpose.capitalize()} poll failed after "
                    f"{attempt} attempts for {job}: {reason}: {retry_error}. "
                    "The job may still be running; retrieve this ID on its "
                    "original resource before resubmitting."
                ) from retry_error
            delay = _retry_delay(
                retry_error,
                attempt,
                base_delay,
                max_delay,
                random_value,
            )
            delay = min(delay, remaining())
            _retry_event(
                f"{purpose} poll",
                attempt,
                max_attempts,
                f"{reason}; {job}",
                delay,
                on_retry,
            )
            wait(delay)

    return response, retry_count


def request_response(client, request, *, journal=None, **options):
    journal = journal if journal is not None else _NULL_JOURNAL
    try:
        return _request_response(client, request, journal=journal, **options)
    except REPLAYABLE_FAILURES as error:
        if not getattr(error, "replayed", False):
            journal.append_best_effort(
                "logical_failed",
                logical_sha256=_sha256_text(_canonical_json_text(request)),
                error_type=type(error).__name__,
                message=_redact_secrets(error),
            )
        raise


def _redact_secrets(text):
    """Remove API-key fragments that services echo in error messages."""
    return SECRET_FRAGMENT_PATTERN.sub("sk-[redacted]", str(text))


def _request_response(
    client,
    request,
    *,
    purpose,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    base_delay=1.0,
    max_delay=30.0,
    sleep=time.sleep,
    random_value=random.random,
    on_retry=None,
    poll_interval=DEFAULT_POLL_INTERVAL,
    poll_timeout=DEFAULT_POLL_TIMEOUT,
    journal=None,
):
    _validate_retry_settings(max_attempts, base_delay, max_delay)
    _validate_poll_timeout(poll_timeout)
    if (
        not isinstance(poll_interval, (int, float))
        or not math.isfinite(poll_interval)
        or poll_interval < 0
    ):
        raise DeepThinkError("Background poll interval must be a finite number.")
    try:
        from openai import (
            APIConnectionError,
            APIResponseValidationError,
            APIStatusError,
        )
    except ImportError as error:
        raise DeepThinkError(
            "Install dependencies with: "
            "python -m pip install --upgrade openai azure-identity"
        ) from error

    journal = journal if journal is not None else _NULL_JOURNAL
    request_has_encrypted_content = _request_contains_encrypted_content(request)
    router = (
        client
        if isinstance(client, DeploymentRouter)
        else DeploymentRouter(
            [(name, client) for name in _deployment_chain(request["model"])]
        )
    )
    targets = router.targets
    if max_attempts is None:
        max_attempts = min(10, max(DEFAULT_MAX_ATTEMPTS, len(targets)))
    logical_sha256 = _sha256_text(_canonical_json_text(request))
    reuse = journal.reusable_attempt(logical_sha256)
    if reuse is not None and reuse.response is not None:
        return RequestOutcome(
            reuse.response,
            reuse.retry_count,
            reuse.attempt["deployment"],
        )
    if reuse is None:
        replayed = journal.replayable_failure(logical_sha256)
        if replayed is not None:
            raise replayed
    deployment_index = 0
    poll_retry_count = 0
    # Providers whose sign-in failed during this request are not retried.
    signed_out = set()

    def retry_next(reason, delay):
        nonlocal deployment_index
        for step in range(1, len(targets) + 1):
            index = (deployment_index + step) % len(targets)
            if targets[index].provider not in signed_out:
                break
        deployment_index = index
        reason += (
            f"; switching deployment to {targets[deployment_index].deployment}"
            f" (target {deployment_index + 1}/{len(targets)})"
        )
        _retry_event(purpose, attempt, max_attempts, reason, delay, on_retry)
        sleep(delay)

    def switch_provider(reason):
        """After a sign-in failure, move to another listed provider's chain."""
        nonlocal deployment_index
        failed = targets[deployment_index].provider
        for step in range(1, len(targets)):
            index = (deployment_index + step) % len(targets)
            if (
                targets[index].provider != failed
                and targets[index].provider not in signed_out
            ):
                signed_out.add(failed)
                deployment_index = index
                reason += (
                    f"; switching to backup provider {targets[index].provider}, "
                    f"deployment {targets[index].deployment} "
                    f"(target {index + 1}/{len(targets)})"
                )
                _retry_event(purpose, attempt, max_attempts, reason, 0.0, on_retry)
                return True
        return False

    for attempt in range(1, max_attempts + 1):
        resumed = reuse is not None
        if resumed:
            known = reuse.attempt
            reuse = None
            target_index, active = _target_for_attempt(router, known)
            if target_index is not None:
                deployment_index = target_index
            attempt_id = known["attempt_id"]
            response_id = known["response_id"]
            attempt_request = _request_for_deployment(request, active.deployment)
            target_label = (
                f"deployment {active.deployment}, target "
                f"{known.get('target_index')}/{known.get('target_count')}"
            )
            journal.append(
                "polling_resumed",
                attempt_id=attempt_id,
                response_id=response_id,
            )
            response = {"id": response_id, "status": "queued"}
        else:
            active = targets[deployment_index]
            target_label = (
                f"deployment {active.deployment}, "
                f"target {deployment_index + 1}/{len(targets)}"
            )
            attempt_request = _request_for_deployment(request, active.deployment)
            journal.ensure_no_unreached_active(logical_sha256)
            attempt_id = journal.begin_attempt(
                purpose=purpose,
                attempt=attempt,
                logical_sha256=logical_sha256,
                request=attempt_request,
                target=active,
                target_index=deployment_index + 1,
                target_count=len(targets),
            )
            try:
                response = active.client.responses.create(**attempt_request)
            except BaseException as error:
                failure = _classify_submission_error(
                    error,
                    APIConnectionError,
                    APIResponseValidationError,
                    APIStatusError,
                    SERVICE_LABELS.get(active.provider, "Azure"),
                )
                if failure.kind == "unknown":
                    if not isinstance(error, Exception):
                        journal.append_best_effort(
                            "submission_unknown",
                            attempt_id=attempt_id,
                            reason=failure.reason,
                        )
                        raise
                    journal.append(
                        "submission_unknown",
                        attempt_id=attempt_id,
                        reason=failure.reason,
                    )
                    raise SubmissionUnknownError(
                        _submission_unknown_message(
                            purpose, active, attempt_id, failure.reason
                        )
                    ) from error
                if failure.kind == "accepted":
                    response_id = failure.response_id
                    _record_accepted(journal, attempt_id, response_id, None)
                    response = {"id": response_id, "status": "queued"}
                else:
                    journal.append(
                        "rejected" if failure.kind == "rejected" else "not_sent",
                        attempt_id=attempt_id,
                        reason=failure.reason,
                        http_status=failure.http_status,
                        code=failure.code,
                    )
                    if failure.kind == "auth":
                        if attempt < max_attempts and switch_provider(
                            "Azure sign-in failed"
                        ):
                            continue
                        raise DeepThinkError(
                            f"{purpose.capitalize()} request was not sent because "
                            f"Azure authentication failed: {error}"
                        ) from error
                    if failure.kind == "not_sent":
                        if attempt >= max_attempts:
                            raise DeepThinkError(
                                f"{purpose.capitalize()} request failed after "
                                f"{attempt} attempts: {error}"
                            ) from error
                        delay = _retry_delay(
                            error,
                            attempt,
                            base_delay,
                            max_delay,
                            random_value,
                        )
                        retry_next(type(error).__name__, delay)
                        continue
                    status_code, code, message = (
                        failure.http_status,
                        failure.code,
                        failure.message,
                    )
                    reason = (
                        f"{SERVICE_LABELS.get(active.provider, 'Azure')} HTTP "
                        f"{status_code}"
                    )
                    if code:
                        reason += f" ({code})"
                    if _status_error_is_opaque_replay(
                        error, request_has_encrypted_content
                    ):
                        raise OpaqueReplayError(
                            f"{purpose.capitalize()} request could not replay "
                            "encrypted reasoning context."
                        ) from error
                    if _status_error_is_context_limit(error):
                        raise ContextLimitError(
                            f"{purpose.capitalize()} request exceeded the context "
                            f"limit: {reason}: {message}"
                        ) from error
                    deployment_missing = (
                        status_code in {403, 404} and code in MISSING_MODEL_CODES
                    )
                    if not (_status_error_is_retryable(error) or deployment_missing):
                        if (
                            status_code in {401, 403}
                            and attempt < max_attempts
                            and switch_provider(reason)
                        ):
                            continue
                        raise DeepThinkError(
                            f"{purpose.capitalize()} request failed without retry: "
                            f"{reason}: {message}"
                        ) from error
                    if attempt >= max_attempts:
                        raise DeepThinkError(
                            f"{purpose.capitalize()} request failed after "
                            f"{attempt} attempts: {reason}: {message}"
                        ) from error
                    delay = _retry_delay(
                        error,
                        attempt,
                        base_delay,
                        max_delay,
                        random_value,
                    )
                    retry_next(reason, delay)
                    continue
            else:
                try:
                    response_id = _response_id(response)
                except MalformedResponseError as error:
                    reason = "the creation response had no valid response ID"
                    journal.append(
                        "submission_unknown", attempt_id=attempt_id, reason=reason
                    )
                    raise SubmissionUnknownError(
                        _submission_unknown_message(purpose, active, attempt_id, reason)
                    ) from error
                status = _safe_response_status(response)
                _record_accepted(journal, attempt_id, response_id, status)
                if status is None:
                    response = {"id": response_id, "status": "queued"}
        try:
            response, retries = _poll_background_response(
                active.client,
                response,
                purpose=purpose,
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                poll_interval=poll_interval,
                poll_timeout=poll_timeout,
                target_label=target_label,
                service=SERVICE_LABELS.get(active.provider, "Azure"),
                sleep=sleep,
                random_value=random_value,
                on_retry=on_retry,
                on_status=lambda status, current=attempt_id: journal.append(
                    "poll_status", attempt_id=current, status=status
                ),
                initial_wait=not resumed,
            )
        except BaseException as error:
            journal.append_best_effort(
                "poll_stopped",
                attempt_id=attempt_id,
                response_id=response_id,
                reason=_redact_secrets(f"{type(error).__name__}: {error}"),
            )
            if isinstance(error, Exception) and _is_credential_error(error):
                raise DeepThinkError(
                    f"{purpose.capitalize()} polling of response {response_id} "
                    "stopped because Azure authentication failed. The job may "
                    "still be running; fix authentication, then run `resume`."
                ) from error
            raise
        poll_retry_count += retries
        journal.record_terminal(attempt_id, response)
        try:
            refusal = _response_refusal_text(response)
            if refusal is not None:
                raise DeepThinkError(
                    f"{purpose.capitalize()} request was refused without retry: "
                    f"{refusal}"
                )
            status = _response_status(response)
            output_text = _response_text(response)
            if status == "completed" and output_text.strip():
                _response_id(response)
                _response_usage(response)
                _response_output(response)
                journal.save_completion(attempt_id, response)
                return RequestOutcome(
                    response,
                    attempt - 1 + poll_retry_count,
                    active.deployment,
                )
            if status == "completed" and attempt < max_attempts:
                journal.append("discarded", attempt_id=attempt_id, reason="empty")
                delay = _retry_delay(
                    None,
                    attempt,
                    base_delay,
                    max_delay,
                    random_value,
                )
                retry_next("empty response", delay)
                continue
            if status == "completed":
                journal.append("discarded", attempt_id=attempt_id, reason="empty")
                raise DeepThinkError(
                    f"{purpose.capitalize()} request failed after "
                    f"{attempt} attempts: empty response"
                )
            if _response_is_opaque_replay(response, request_has_encrypted_content):
                raise OpaqueReplayError(
                    f"{purpose.capitalize()} request could not replay encrypted "
                    "reasoning context."
                )
            response_codes, response_message = _response_error_details(response)
            if response_codes & CONTEXT_LIMIT_CODES:
                raise ContextLimitError(
                    f"{purpose.capitalize()} request exceeded the context limit: "
                    f"{response_message}"
                )
            retryable_codes = response_codes & RETRYABLE_RESPONSE_CODES
            if retryable_codes:
                request_bytes = _canonical_json_text(attempt_request).encode("utf-8")
                reason = (
                    f"Azure response {status} "
                    f"({', '.join(sorted(retryable_codes))}); "
                    f"response_id={_response_id(response)}; {target_label}; "
                    f"max_output_tokens={attempt_request.get('max_output_tokens')}; "
                    f"request_bytes={len(request_bytes)}; "
                    f"request_sha256={hashlib.sha256(request_bytes).hexdigest()}; "
                    f"{response_message}"
                )
            if retryable_codes and attempt < max_attempts:
                delay = _retry_delay(
                    None,
                    attempt,
                    base_delay,
                    max_delay,
                    random_value,
                )
                retry_next(reason, delay)
                continue
            if retryable_codes:
                error_type = (
                    TerminalServiceError
                    if status == "failed" and "server_error" in retryable_codes
                    else DeepThinkError
                )
                raise error_type(
                    f"{purpose.capitalize()} request failed after "
                    f"{attempt} attempts: {reason}"
                )
            if "max_output_tokens" in response_codes:
                raise OutputLimitError(
                    f"{purpose.capitalize()} request exhausted its output budget.",
                    response,
                )
            code_text = (
                " (" + ", ".join(sorted(response_codes)) + ")" if response_codes else ""
            )
            raise DeepThinkError(
                f"{purpose.capitalize()} request failed without retry: Azure "
                f"response {status}{code_text}: {response_message}"
            )
        except MalformedResponseError as error:
            journal.append("discarded", attempt_id=attempt_id, reason="malformed")
            if attempt >= max_attempts:
                raise DeepThinkError(
                    f"{purpose.capitalize()} request failed after "
                    f"{attempt} attempts: malformed Azure response"
                ) from error
            delay = _retry_delay(
                None,
                attempt,
                base_delay,
                max_delay,
                random_value,
            )
            retry_next("malformed Azure response", delay)
            continue

    raise DeepThinkError(f"{purpose.capitalize()} request exhausted retries.")


def build_response_request(
    history,
    deployment,
    max_output_tokens=MAX_OUTPUT_TOKENS,
):
    return {
        "model": deployment,
        "input": history,
        "store": False,
        "instructions": DEEP_MATH_INSTRUCTIONS,
        "reasoning": {
            "mode": "pro",
            "effort": "max",
            "context": "all_turns",
            "summary": "auto",
        },
        "text": {"verbosity": "high"},
        "max_output_tokens": max_output_tokens,
        "truncation": "disabled",
        "background": True,
    }


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _cleanup_temporary_file(path):
    if path is None:
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError as error:
        return error
    return None


def _atomic_write_error(path, operation, error, cleanup_error=None):
    message = f"Could not atomically write {path} while {operation}: {error}"
    if cleanup_error is not None:
        message += f" (temporary-file cleanup also failed: {cleanup_error})"
    return DeepThinkError(message)


def _atomic_write_text(path, content):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DeepThinkError(
            f"Could not create directory for {path}: {error}"
        ) from error

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
    except OSError as error:
        cleanup_error = _cleanup_temporary_file(temporary_path)
        raise _atomic_write_error(
            path,
            "creating or writing the temporary file",
            error,
            cleanup_error,
        ) from error

    try:
        os.replace(temporary_path, path)
    except OSError as error:
        cleanup_error = _cleanup_temporary_file(temporary_path)
        raise _atomic_write_error(
            path,
            "replacing the destination file",
            error,
            cleanup_error,
        ) from error


def _write_json(path, value):
    _atomic_write_text(path, _json_text(value))


def _json_text(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _canonical_json_text(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state_digest(state):
    return _sha256_text(
        _canonical_json_text(
            {key: value for key, value in state.items() if key != "state_sha256"}
        )
    )


def _verify_file_checksum(path, expected, label):
    if expected is None:
        return
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise DeepThinkError(
            f"Could not checksum {label} file {path}: {error}"
        ) from error
    if actual != expected:
        raise DeepThinkError(
            f"Current {label} checksum does not match state; a prior write "
            "may have been interrupted or the file was edited manually."
        )


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DeepThinkError(f"Could not read JSON file {path}: {error}") from error


def _read_text(path, *, label):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DeepThinkError(f"Could not read {label} file {path}: {error}") from error


def _validate_project(project):
    if not PROJECT_PATTERN.fullmatch(project):
        raise DeepThinkError(
            "Project must be a lowercase hyphenated slug of 1-64 characters."
        )


def _validate_retry_settings(max_attempts, base_delay, max_delay):
    # None means one attempt per routed target (at least DEFAULT_MAX_ATTEMPTS).
    if max_attempts is not None and (
        not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10
    ):
        raise DeepThinkError("Maximum attempts must be between 1 and 10.")
    if not all(
        isinstance(delay, (int, float)) and math.isfinite(delay)
        for delay in (base_delay, max_delay)
    ):
        raise DeepThinkError("Retry delays must be finite numbers.")
    if base_delay < 0 or max_delay < 0:
        raise DeepThinkError("Retry delays must not be negative.")


def _validate_checksum(checksum, *, label):
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise DeepThinkError(f"State file contains an invalid {label}.")


def _validate_poll_timeout(poll_timeout):
    if (
        isinstance(poll_timeout, bool)
        or not isinstance(poll_timeout, (int, float))
        or not math.isfinite(poll_timeout)
        or poll_timeout <= 0
    ):
        raise DeepThinkError("Background poll timeout must be finite and positive.")


def _validate_state(state, project):
    if not isinstance(state, dict):
        raise DeepThinkError("State file is invalid or missing required fields.")
    required_types = {
        "version": int,
        "project": str,
        "title": str,
        "deployment": str,
        "volume": int,
        "turn": int,
        "context_tokens": int,
        "cumulative_tokens": int,
        "rollover_tokens": int,
        "created_at": str,
        "updated_at": str,
    }
    if any(
        key not in state or not isinstance(state[key], expected_type)
        for key, expected_type in required_types.items()
    ):
        raise DeepThinkError("State file is invalid or missing required fields.")
    version = state["version"]
    schema = state.get("schema")
    if version == LEGACY_STATE_VERSION:
        current_state = False
        if schema not in (None, STATE_SCHEMA):
            raise DeepThinkError("State file is invalid or inconsistent.")
    elif version == STATE_VERSION and schema == STATE_SCHEMA:
        current_state = True
    else:
        raise DeepThinkError("State file is invalid or inconsistent.")
    if state["project"] != project or not state["title"] or not state["deployment"]:
        raise DeepThinkError("State file is invalid or inconsistent.")
    if (
        state["volume"] < 1
        or state["turn"] < 0
        or state["context_tokens"] < 0
        or state["cumulative_tokens"] < 0
        or not 1 <= state["rollover_tokens"] <= DEFAULT_ROLLOVER_TOKENS
    ):
        raise DeepThinkError("State file is invalid or inconsistent.")
    last_response_id = state.get("last_response_id")
    if last_response_id is not None and not isinstance(last_response_id, str):
        raise DeepThinkError("State file is invalid or inconsistent.")
    for checksum_name in ("context_sha256", "transcript_sha256"):
        checksum = state.get(checksum_name)
        if checksum is not None:
            _validate_checksum(checksum, label="checksum")
    if not current_state:
        return
    for checksum_name in ("context_sha256", "transcript_sha256", "state_sha256"):
        checksum = state.get(checksum_name)
        if checksum is None:
            raise DeepThinkError(
                "Current state schema requires state, context, and transcript "
                "checksums."
            )
        _validate_checksum(checksum, label=checksum_name)
    if state["state_sha256"] != _state_digest(state):
        raise DeepThinkError(
            "Current state digest does not match state.json; it may have been "
            "edited manually."
        )


def _estimate_tokens(text):
    return max(1, len(text.encode("utf-8")))


def _estimate_message_tokens(role, content):
    return _estimate_tokens(
        _canonical_json_text(
            {
                "type": "message",
                "role": role,
                "content": content,
            }
        )
    )


def _estimate_carried_context_tokens(summary_text, summary_usage):
    normalized_summary = summary_text.rstrip()
    carried_context = f"{CARRIED_CONTEXT_PREFIX.rstrip()}\n\n{normalized_summary}"
    deterministic_total = _estimate_message_tokens("developer", carried_context)
    prefix_overhead = _estimate_message_tokens("developer", "") + _estimate_tokens(
        f"{CARRIED_CONTEXT_PREFIX.rstrip()}\n\n"
    )
    summary_content_tokens = max(
        summary_usage["output_tokens"],
        _estimate_tokens(normalized_summary),
    )
    return max(deterministic_total, summary_content_tokens + prefix_overhead)


def _minimum_response_tokens(rollover_tokens):
    return min(
        MIN_RESPONSE_TOKENS,
        max(1, rollover_tokens // 4),
    )


def _normal_output_budget(base_tokens, prompt, rollover_tokens):
    input_upper_bound = base_tokens + _estimate_tokens(prompt)
    if input_upper_bound > MAX_INPUT_TOKENS:
        raise DeepThinkError(
            "Request exceeds the 922,000-token maximum input estimate."
        )
    return min(
        MAX_OUTPUT_TOKENS,
        rollover_tokens - input_upper_bound,
    )


def _response_output(response):
    return [_dump_output_item(item) for item in _response_output_items(response)]


def _response_usage(response):
    usage = _required_member(response, "usage", "response")
    input_tokens = _required_member(usage, "input_tokens", "response.usage")
    output_tokens = _required_member(usage, "output_tokens", "response.usage")
    total_tokens = _required_member(usage, "total_tokens", "response.usage")
    for label, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("total_tokens", total_tokens),
    ):
        if not isinstance(value, int) or value < 0:
            raise MalformedResponseError(
                f"response.usage.{label} must be a non-negative integer."
            )
    details = _optional_member(usage, "output_tokens_details", "response.usage")
    reasoning_tokens = (
        _optional_member(
            details, "reasoning_tokens", "response.usage.output_tokens_details"
        )
        if details is not None
        else 0
    )
    if reasoning_tokens is None:
        reasoning_tokens = 0
    if not isinstance(reasoning_tokens, int) or reasoning_tokens < 0:
        raise MalformedResponseError(
            "response.usage.output_tokens_details.reasoning_tokens must be a "
            "non-negative integer."
        )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _require_complete(response):
    if _response_status(response) != "completed":
        details = _optional_member(response, "incomplete_details", "response")
        reason = (
            _optional_response_text(
                details,
                "reason",
                "response.incomplete_details",
            )
            or "unknown"
        )
        raise DeepThinkError(f"Azure response was incomplete: {reason}")
    if not _response_text(response):
        raise DeepThinkError("Azure response completed without visible output.")


def _volume_paths(project_dir, volume):
    prefix = f"{volume:04d}"
    return (
        project_dir / f"{prefix}-context.json",
        project_dir / f"{prefix}-transcript.md",
    )


def _transcript_header(state):
    primary_mode, primary_effort = _reasoning_profile(state["deployment"])
    return (
        "---\n"
        f"schema: deep-think-transcript/v{STATE_VERSION}\n"
        f"project: {json.dumps(state['project'])}\n"
        f"volume: {state['volume']}\n"
        f"primary-deployment: {json.dumps(state['deployment'])}\n"
        f"primary-reasoning-mode: {primary_mode}\n"
        f"primary-reasoning-effort: {primary_effort}\n"
        f"context-window-tokens: {CONTEXT_WINDOW_TOKENS}\n"
        f"rollover-tokens: {state['rollover_tokens']}\n"
        f"created-at: {json.dumps(state['created_at'])}\n"
        "---\n\n"
        f"# {state['title']} - Volume {state['volume']:04d}\n"
    )


def _reasoning_profile(deployment):
    if _uses_gpt54_profile(deployment):
        return "not configurable", "xhigh"
    return "pro", "max"


def _turn_markdown(
    turn,
    prompt,
    response,
    usage,
    retry_count=0,
    deployment=None,
):
    reasoning_mode, reasoning_effort = _reasoning_profile(deployment)
    return (
        f"\n## Conversation {turn}\n\n"
        "### User\n\n"
        f"{prompt.rstrip()}\n\n"
        "### Assistant\n\n"
        f"{response.output_text.rstrip()}\n\n"
        "### Usage\n\n"
        f"- Response ID: `{response.id}`\n"
        f"- Deployment: `{deployment}`\n"
        f"- Reasoning mode: `{reasoning_mode}`\n"
        f"- Reasoning effort: `{reasoning_effort}`\n"
        f"- Input tokens: {usage['input_tokens']:,}\n"
        f"- Output tokens: {usage['output_tokens']:,}\n"
        f"- Reasoning tokens: {usage['reasoning_tokens']:,}\n"
        f"- Application retries: {retry_count}\n"
        f"- Context tokens after turn: {usage['total_tokens']:,} / "
        f"{CONTEXT_WINDOW_TOKENS:,}\n"
    )


def _rollover_markdown(
    response,
    usage,
    retry_count=0,
    recovery_note=None,
    deployment=None,
    intermediate_tokens=0,
):
    reasoning_mode, reasoning_effort = _reasoning_profile(deployment)
    recovery_markdown = (
        f"\n> Recovery mode: {recovery_note}.\n" if recovery_note is not None else ""
    )
    return (
        "\n## Volume rollover summary\n\n"
        f"{recovery_markdown}"
        f"{response.output_text.rstrip()}\n\n"
        "### Summary usage\n\n"
        f"- Response ID: `{response.id}`\n"
        f"- Deployment: `{deployment}`\n"
        f"- Reasoning mode: `{reasoning_mode}`\n"
        f"- Reasoning effort: `{reasoning_effort}`\n"
        f"- Input tokens: {usage['input_tokens']:,}\n"
        f"- Output tokens: {usage['output_tokens']:,}\n"
        f"- Reasoning tokens: {usage['reasoning_tokens']:,}\n"
        f"- Application retries: {retry_count}\n"
        f"- Final context tokens: {usage['total_tokens']:,} / "
        f"{CONTEXT_WINDOW_TOKENS:,}\n"
        f"- Intermediate summary tokens: {intermediate_tokens:,}\n"
    )


def _new_state(project, title, deployment, rollover_tokens):
    now = _utc_now()
    return {
        "schema": STATE_SCHEMA,
        "version": STATE_VERSION,
        "project": project,
        "title": title,
        "deployment": deployment,
        "volume": 1,
        "turn": 0,
        "context_tokens": 0,
        "cumulative_tokens": 0,
        "rollover_tokens": rollover_tokens,
        "last_response_id": None,
        "created_at": now,
        "updated_at": now,
    }


def _write_pending_state(
    state_path,
    state,
    *,
    context_path,
    transcript_path,
    pending_json,
    pending_text,
):
    if context_path not in pending_json or transcript_path not in pending_text:
        raise DeepThinkError("Pending state is missing the current volume files.")
    state["schema"] = STATE_SCHEMA
    state["version"] = STATE_VERSION
    state["context_sha256"] = _sha256_text(_json_text(pending_json[context_path]))
    state["transcript_sha256"] = _sha256_text(pending_text[transcript_path])
    state["state_sha256"] = _state_digest(state)
    for path, value in pending_json.items():
        _write_json(path, value)
    for path, value in pending_text.items():
        _atomic_write_text(path, value)
    _write_json(state_path, state)


def _file_sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DeepThinkError(f"Could not checksum {path}: {error}") from error


def _parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _operator_name():
    try:
        return getpass.getuser()
    except (ImportError, KeyError, OSError):
        return None


class CachedResponse:
    """A completed response restored from the local request journal."""

    def __init__(self, payload):
        self.id = payload["id"]
        self.status = payload["status"]
        self.output_text = payload["output_text"]
        self.output = payload["output"]
        self.usage = payload["usage"]
        self.error = None
        self.incomplete_details = None


def _serialize_completed_response(response):
    usage = _response_usage(response)
    return {
        "id": _response_id(response),
        "status": _response_status(response),
        "output_text": _response_text(response),
        "output": _response_output(response),
        "usage": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "output_tokens_details": {"reasoning_tokens": usage["reasoning_tokens"]},
        },
    }


class ReusableAttempt:
    def __init__(self, attempt, response=None, retry_count=0):
        self.attempt = attempt
        self.response = response
        self.retry_count = retry_count


def _read_journal(path):
    try:
        data = Path(path).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return [], False
    except OSError as error:
        raise DeepThinkError(
            f"Could not read request journal {path}: {error}"
        ) from error
    torn_tail = bool(data) and not data.endswith(b"\n")
    records = []
    for number, line in enumerate(data.split(b"\n")[:-1], start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DeepThinkError(
                f"Request journal {path} line {number} is corrupt: {error}. "
                "Inspect it before submitting further requests."
            ) from error
        if not isinstance(record, dict) or record.get("schema") != JOURNAL_SCHEMA:
            raise DeepThinkError(
                f"Request journal {path} line {number} is corrupt: unexpected "
                "record format."
            )
        for field in ("payload", "prompt_file"):
            name = record.get(field)
            if name is not None and not _is_artifact_name(name):
                raise DeepThinkError(
                    f"Request journal {path} line {number} has an invalid "
                    f"artifact name in {field!r}: {name!r}. The journal may have "
                    "been tampered with; inspect it before continuing."
                )
        for field in JOURNAL_TEXT_FIELDS:
            value = record.get(field)
            if value is not None and not isinstance(value, str):
                raise DeepThinkError(
                    f"Request journal {path} line {number} has an invalid "
                    f"{field!r} value. The journal may have been tampered with; "
                    "inspect it before continuing."
                )
        records.append(record)
    return records, torn_tail


def _is_artifact_name(name):
    return isinstance(name, str) and ARTIFACT_NAME_PATTERN.fullmatch(name) is not None


ATTEMPT_RECORD_FIELDS = (
    "attempt_id",
    "turn_id",
    "lock_id",
    "purpose",
    "attempt",
    "logical_sha256",
    "request_sha256",
    "request_bytes",
    "max_output_tokens",
    "deployment",
    "resource",
    "role",
    "provider",
    "target_index",
    "target_count",
)


class JournalView:
    """Derived local and remote request state replayed from the journal."""

    def __init__(self, records, *, torn_tail=False):
        self.records = records
        self.torn_tail = torn_tail
        self.turns = {}
        self.attempts = {}
        self.legacy_items = {}
        self.logical_failures = {}
        self._sequence = 0
        for record in records:
            self._sequence += 1
            self._apply(record)

    def _apply(self, record):
        event = record.get("event")
        turn = self.turns.get(record.get("turn_id"))
        if event == "turn_started":
            self.turns[record.get("turn_id")] = {
                "turn_id": record.get("turn_id"),
                "started_at": record.get("recorded_at"),
                "prompt_sha256": record.get("prompt_sha256"),
                "prompt_file": record.get("prompt_file"),
                "base_state_sha256": record.get("base_state_sha256"),
                "deployment": record.get("deployment"),
                "title": record.get("title"),
                "rollover_tokens": record.get("rollover_tokens"),
                "recover_service_errors": record.get("recover_service_errors"),
                "checkpoints": [],
                "checkpoint_kinds": {},
                "state": "open",
                "last_error": None,
            }
        elif event == "turn_checkpoint" and turn is not None:
            turn["checkpoints"].append(record.get("state_sha256"))
            turn["checkpoint_kinds"][record.get("state_sha256")] = (
                record.get("kind") or "proactive"
            )
            for attempt in self.turn_attempts(turn["turn_id"]):
                if attempt["terminal_status"] == "completed" and attempt["payload"]:
                    attempt["persisted"] = True
        elif event == "turn_error" and turn is not None:
            turn["last_error"] = record.get("error")
        elif event == "turn_committed" and turn is not None:
            turn["state"] = "committed"
        elif event in {"turn_superseded", "turn_abandoned"} and turn is not None:
            turn["state"] = "abandoned"
        elif event == "submitting":
            attempt = {key: record.get(key) for key in ATTEMPT_RECORD_FIELDS}
            attempt.update(
                submitted_at=record.get("recorded_at"),
                sequence=self._sequence,
                outcome=None,
                response_id=None,
                last_status=None,
                terminal_status=None,
                payload=None,
                payload_sha256=None,
                poll_stopped=None,
                resolution=None,
                reason=None,
                discarded=False,
                persisted=False,
                attached_unverified=False,
            )
            self.attempts[attempt["attempt_id"]] = attempt
        elif event == "logical_failed":
            self.logical_failures[
                (record.get("turn_id"), record.get("logical_sha256"))
            ] = {
                "error_type": record.get("error_type"),
                "message": record.get("message"),
                "sequence": self._sequence,
            }
        elif event == "legacy_writer_unknown":
            self.legacy_items[record.get("item_id")] = {
                "item_id": record.get("item_id"),
                "recorded_at": record.get("recorded_at"),
                "previous_owner": record.get("previous_owner"),
                "resolved": False,
            }
        elif event in {"operator_confirmed_no_remote_job", "legacy_resolved"}:
            for attempt_id in record.get("attempt_ids") or []:
                if attempt_id in self.attempts:
                    self.attempts[attempt_id]["resolution"] = "confirmed_no_remote_job"
            for item_id in record.get("item_ids") or []:
                if item_id in self.legacy_items:
                    self.legacy_items[item_id]["resolved"] = True
        else:
            self._apply_attempt_event(event, record)

    def _apply_attempt_event(self, event, record):
        attempt = self.attempts.get(record.get("attempt_id"))
        if attempt is None:
            return
        status = record.get("status")
        if event in {"poll_status", "terminal", "cancel_requested", "completed"}:
            attempt["attached_unverified"] = False
        if event == "accepted":
            attempt.update(
                outcome="accepted",
                response_id=record.get("response_id"),
                last_status=status,
            )
        elif event in {"rejected", "not_sent"}:
            attempt.update(outcome=event, reason=record.get("reason"))
        elif event == "submission_unknown" and attempt["outcome"] in {None, "unknown"}:
            attempt.update(outcome="unknown", reason=record.get("reason"))
        elif event == "operator_attached":
            attempt.update(
                outcome="accepted",
                response_id=record.get("response_id"),
                resource=record.get("resource", attempt["resource"]),
                last_status=None,
                attached_unverified=True,
            )
        elif event == "attachment_rejected":
            attempt.update(
                outcome="unknown",
                response_id=None,
                attached_unverified=False,
                reason=record.get("reason"),
            )
        elif event == "remote_not_visible":
            # The job may still run where this credential cannot see it.
            attempt.update(
                outcome="unknown",
                attached_unverified=False,
                reason=record.get("reason"),
            )
        elif event in {"poll_status", "terminal", "cancel_requested"}:
            if status is not None:
                attempt["last_status"] = status
            if status in TERMINAL_RESPONSE_STATUSES:
                attempt["terminal_status"] = status
        elif event == "completed":
            attempt.update(
                terminal_status="completed",
                last_status="completed",
                payload=record.get("payload"),
                payload_sha256=record.get("payload_sha256"),
            )
        elif event == "poll_stopped":
            attempt["poll_stopped"] = record.get("reason")
        elif event == "discarded":
            attempt["discarded"] = True
        elif event == "remote_unavailable":
            attempt["resolution"] = "unavailable"

    @staticmethod
    def attempt_state(attempt, live_lock_id=None):
        if attempt["resolution"] is not None:
            return "resolved"
        if attempt["outcome"] in {"rejected", "not_sent"}:
            return "not_created"
        if attempt["terminal_status"] is not None:
            return "terminal"
        if attempt["outcome"] == "accepted":
            return "active"
        if attempt["outcome"] is None and attempt["lock_id"] == live_lock_id:
            return "in_flight"
        return "unknown"

    def open_turn(self):
        for turn in reversed(list(self.turns.values())):
            if turn["state"] == "open":
                return turn
        return None

    def turn_attempts(self, turn_id):
        return [a for a in self.attempts.values() if a["turn_id"] == turn_id]

    def outstanding(self, live_lock_id=None):
        return [
            attempt
            for attempt in self.attempts.values()
            if self.attempt_state(attempt, live_lock_id) in OUTSTANDING_ATTEMPT_STATES
        ]

    def unresolved_legacy(self):
        return [item for item in self.legacy_items.values() if not item["resolved"]]

    def recoverable(self, turn_id):
        turn = self.turns.get(turn_id)
        if turn is None or turn["state"] != "open":
            return []
        return [
            attempt
            for attempt in self.turn_attempts(turn_id)
            if attempt["terminal_status"] == "completed"
            and attempt["payload"]
            and not attempt["discarded"]
            and not attempt["persisted"]
            and attempt["resolution"] is None
        ]

    def attempt_for_response(self, response_id):
        matches = [a for a in self.attempts.values() if a["response_id"] == response_id]
        return matches[-1] if matches else None

    def replayable_failure(self, turn_id, logical_sha256):
        failure = self.logical_failures.get((turn_id, logical_sha256))
        if failure is None:
            return None
        later = [
            attempt
            for attempt in self.attempts.values()
            if attempt["turn_id"] == turn_id
            and attempt["sequence"] > failure["sequence"]
        ]
        # A later retry of the same request supersedes the recorded failure.
        if any(attempt["logical_sha256"] == logical_sha256 for attempt in later):
            return None
        if failure["error_type"] in DETERMINISTIC_FAILURES:
            return failure
        # A transient failure is replayed only if the turn then moved on.
        return failure if later else None


class RequestJournal:
    """Append-only, fsynced record of every Azure submission for one project."""

    def __init__(self, project_dir, *, lock_id=None):
        self.directory = Path(project_dir) / JOURNAL_DIRECTORY
        self.path = self.directory / JOURNAL_FILENAME
        self.lock_id = lock_id
        self.turn_id = None
        self._tail_checked = False

    def _failure(self, error):
        return DeepThinkError(
            f"Could not durably write request journal {self.path}: {error}"
        )

    def _repair_torn_tail(self):
        self._tail_checked = True
        try:
            with open(self.path, "r+b") as stream:
                data = stream.read()
                if not data or data.endswith(b"\n"):
                    return
                cut = data.rfind(b"\n") + 1
                discarded = data[cut:]
                stream.seek(cut)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        except FileNotFoundError:
            return
        except OSError as error:
            raise self._failure(error) from error
        self.append(
            "torn_record_discarded",
            bytes=len(discarded),
            sha256=hashlib.sha256(discarded).hexdigest(),
        )

    def append(self, event, **fields):
        record = {
            "schema": JOURNAL_SCHEMA,
            "event": event,
            "recorded_at": _utc_now(),
            "lock_id": self.lock_id,
            "turn_id": self.turn_id,
        }
        record.update(fields)
        data = (_canonical_json_text(record) + "\n").encode("utf-8")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise self._failure(error) from error
        if not self._tail_checked:
            self._repair_torn_tail()
        try:
            with open(self.path, "ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise self._failure(error) from error
        return record

    def append_best_effort(self, event, **fields):
        try:
            return self.append(event, **fields)
        except DeepThinkError:
            return None

    def view(self):
        records, torn_tail = _read_journal(self.path)
        return JournalView(records, torn_tail=torn_tail)

    def begin_attempt(
        self,
        *,
        purpose,
        attempt,
        logical_sha256,
        request,
        target,
        target_index,
        target_count,
    ):
        attempt_id = uuid.uuid4().hex
        body = _canonical_json_text(request).encode("utf-8")
        self.append(
            "submitting",
            attempt_id=attempt_id,
            purpose=purpose,
            attempt=attempt,
            logical_sha256=logical_sha256,
            request_sha256=hashlib.sha256(body).hexdigest(),
            request_bytes=len(body),
            max_output_tokens=request.get("max_output_tokens"),
            deployment=target.deployment,
            resource=target.resource,
            role=target.role,
            provider=target.provider,
            target_index=target_index,
            target_count=target_count,
            pid=os.getpid(),
        )
        return attempt_id

    def reusable_attempt(self, logical_sha256):
        if self.turn_id is None:
            return None
        view = self.view()
        candidates = [
            attempt
            for attempt in view.turn_attempts(self.turn_id)
            if attempt["logical_sha256"] == logical_sha256
        ]
        for attempt in candidates:
            if view.attempt_state(attempt) == "unknown":
                raise SubmissionUnknownError(
                    _submission_unknown_message(
                        attempt["purpose"] or "request",
                        RouteTarget(attempt["deployment"], None, attempt["resource"]),
                        attempt["attempt_id"],
                        attempt["reason"]
                        or "the writer stopped before recording the outcome",
                    )
                )
        reusable = [a for a in candidates if not a["discarded"]]
        for index, attempt in reversed(list(enumerate(reusable))):
            if attempt["resolution"] is None and attempt["payload"]:
                response = self.load_completion(attempt)
                if response is not None:
                    return ReusableAttempt(attempt, response, index)
        for state, status in (("active", None), ("terminal", "completed")):
            for attempt in reversed(reusable):
                if view.attempt_state(attempt) == state and (
                    status is None or attempt["terminal_status"] == status
                ):
                    return ReusableAttempt(attempt)
        return None

    def replayable_failure(self, logical_sha256):
        if self.turn_id is None:
            return None
        failure = self.view().replayable_failure(self.turn_id, logical_sha256)
        if failure is None:
            return None
        error_type = {cls.__name__: cls for cls in REPLAYABLE_FAILURES}.get(
            failure["error_type"]
        )
        if error_type is None:
            return None
        message = f"{failure['message']} (replayed from the request journal)"
        error = (
            OutputLimitError(message, None)
            if error_type is OutputLimitError
            else error_type(message)
        )
        error.replayed = True
        return error

    def ensure_no_unreached_active(self, logical_sha256):
        if self.turn_id is None:
            return
        view = self.view()
        running = [
            attempt["response_id"]
            for attempt in view.turn_attempts(self.turn_id)
            if view.attempt_state(attempt) == "active"
            and attempt["logical_sha256"] != logical_sha256
        ]
        if running:
            raise RemoteStateError(
                "The unfinished turn still has running request(s) "
                f"{', '.join(running)} that this run did not reach; another "
                "submission could duplicate paid work. Run `resume`, which reuses "
                "the turn's recorded options, or use `cancel` or `reconcile`."
            )

    def record_terminal(self, attempt_id, response):
        try:
            response_id = _response_id(response)
        except MalformedResponseError:
            response_id = None
        try:
            codes, message = _response_error_details(response)
        except MalformedResponseError:
            codes, message = set(), None
        try:
            usage = _response_usage(response)
        except MalformedResponseError:
            usage = None
        self.append(
            "terminal",
            attempt_id=attempt_id,
            response_id=response_id,
            status=_safe_response_status(response),
            error_codes=sorted(codes),
            error_message=message if codes else None,
            usage=usage,
        )

    def save_completion(self, attempt_id, response):
        payload = _serialize_completed_response(response)
        response_id = payload["id"]
        name = (
            f"{response_id}.json"
            if RESPONSE_FILENAME_PATTERN.fullmatch(response_id)
            else f"{attempt_id}.response.json"
        )
        text = _json_text(payload)
        try:
            _atomic_write_text(self._artifact_path(name), text)
        except DeepThinkError as error:
            raise DeepThinkError(
                f"Response {response_id} completed, but the request journal could "
                f"not cache it: {error}"
            ) from error
        self.append(
            "completed",
            attempt_id=attempt_id,
            response_id=response_id,
            payload=name,
            payload_sha256=_sha256_text(text),
        )

    def _artifact_path(self, name):
        # Names come from the journal, which may be shared; never escape it.
        if not _is_artifact_name(name):
            raise DeepThinkError(
                f"Request journal contains an invalid artifact name: {name!r}"
            )
        path = self.directory / name
        if path.resolve().parent != self.directory.resolve():
            raise DeepThinkError(
                f"Request journal artifact name escapes {self.directory}: {name!r}"
            )
        return path

    def load_completion(self, attempt):
        path = self._artifact_path(attempt["payload"])
        try:
            text = path.read_bytes().decode("utf-8")
            if _sha256_text(text) != attempt["payload_sha256"]:
                return None
            response = CachedResponse(json.loads(text))
            _response_id(response)
            _response_usage(response)
            _response_output(response)
        except (OSError, UnicodeError, KeyError, TypeError, ValueError):
            return None
        except MalformedResponseError:
            return None
        return response if response.output_text.strip() else None

    def save_prompt(self, turn_id, prompt):
        name = f"{turn_id}.prompt.txt"
        try:
            _atomic_write_text(self._artifact_path(name), prompt)
        except DeepThinkError as error:
            raise DeepThinkError(
                f"Could not record the turn prompt in the request journal: {error}"
            ) from error
        return name

    def read_prompt(self, turn):
        path = self._artifact_path(turn["prompt_file"])
        try:
            prompt = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise DeepThinkError(
                f"The unfinished turn's journaled prompt is unavailable: {error}"
            ) from error
        if _sha256_text(prompt) != turn["prompt_sha256"]:
            raise DeepThinkError(
                "The unfinished turn's journaled prompt does not match its checksum."
            )
        return prompt

    def release_payloads(self, turn_id):
        for attempt in self.view().turn_attempts(turn_id):
            if attempt["payload"]:
                path = self._artifact_path(attempt["payload"])
                try:
                    path.unlink()
                except OSError:
                    pass

    def release_turn_artifacts(self, turn_id):
        self.release_payloads(turn_id)
        turn = self.view().turns.get(turn_id)
        if turn is not None and turn["prompt_file"]:
            path = self._artifact_path(turn["prompt_file"])
            try:
                path.unlink()
            except OSError:
                pass


class _NullJournal:
    """Used only by direct library calls that are not tied to a project."""

    turn_id = None

    def append(self, event, **fields):
        return None

    def append_best_effort(self, event, **fields):
        return None

    def begin_attempt(self, **fields):
        return uuid.uuid4().hex

    def reusable_attempt(self, logical_sha256):
        return None

    def replayable_failure(self, logical_sha256):
        return None

    def ensure_no_unreached_active(self, logical_sha256):
        return None

    def record_terminal(self, attempt_id, response):
        return None

    def save_completion(self, attempt_id, response):
        return None


_NULL_JOURNAL = _NullJournal()


class WriterLock:
    def __init__(self, path, owner, recovered=None):
        self.path = path
        self.owner = owner
        self.lock_id = owner["lock_id"]
        self.recovered = recovered


def _windows_process_liveness(pid):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such process
            return False, None
        if error == 5:  # ERROR_ACCESS_DENIED: the process exists
            return True, None
        return None, None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None, None
        if exit_code.value != 259:  # STILL_ACTIVE
            return False, None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(
            handle, *(ctypes.byref(item) for item in times)
        ):
            return True, None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        started = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=ticks // 10
        )
        return True, started
    finally:
        kernel32.CloseHandle(handle)


def _process_liveness(pid):
    """Return (alive, start time); alive is None when it cannot be determined."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None, None
    if os.name == "nt":
        try:
            return _windows_process_liveness(pid)
        except (AttributeError, OSError, ValueError):
            return None, None
    # Never use os.kill for probing on Windows: it terminates the process there.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, None
    except OSError:
        return None, None
    return True, None


def _current_process_started_at():
    alive, started = _process_liveness(os.getpid())
    return started.isoformat() if alive and started is not None else None


def _inspect_lock(path):
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return {"state": "none", "owner": None, "legacy_format": False}
    except (OSError, UnicodeError) as error:
        return {
            "state": "unverifiable",
            "reason": f"the lock file is unreadable: {error}",
            "owner": None,
            "raw": None,
            "legacy_format": False,
        }
    try:
        owner = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        owner = None
    if not isinstance(owner, dict):
        status = {"raw": raw, "owner": None, "legacy_format": False}
        if time.time() - modified < EMPTY_LOCK_GRACE_SECONDS:
            return {
                **status,
                "state": "busy",
                "reason": "the lock is being created or has unrecognized contents",
            }
        return {
            **status,
            "state": "stale",
            "ownerless": True,
            "reason": "the lock has no recorded owner and is older than the "
            "creation grace period",
        }
    status = {"raw": raw, "owner": owner, "legacy_format": "lock_id" not in owner}
    host = owner.get("host")
    pid = owner.get("pid")
    if host is not None and host != socket.gethostname():
        return {
            **status,
            "state": "unverifiable",
            "reason": f"the lock belongs to host {host!r}",
        }
    if pid == os.getpid():
        return {**status, "state": "live", "reason": "this process holds the lock"}
    alive, started = _process_liveness(pid)
    if alive is None:
        return {
            **status,
            "state": "unverifiable",
            "reason": f"could not determine whether process {pid!r} is running",
        }
    if not alive:
        return {**status, "state": "stale", "reason": f"process {pid} is not running"}
    if started is not None:
        tolerance = timedelta(seconds=PROCESS_START_TOLERANCE_SECONDS)
        recorded = _parse_timestamp(owner.get("process_started_at"))
        created = _parse_timestamp(owner.get("created_at"))
        if (recorded is not None and abs(started - recorded) > tolerance) or (
            recorded is None and created is not None and started > created + tolerance
        ):
            return {
                **status,
                "state": "stale",
                "reason": f"process ID {pid} now belongs to a different process",
            }
    return {**status, "state": "live", "reason": f"process {pid} is running"}


def _lock_message(project, status, *, live):
    owner = status.get("owner") or {}
    details = ", ".join(
        f"{key} {owner[key]}"
        for key in ("pid", "host", "command", "created_at")
        if owner.get(key) is not None
    )
    details = f"{details or 'owner unknown'}; {status.get('reason')}"
    if live:
        return (
            f"Project {project!r} is already locked by a running writer ({details}). "
            "Do not delete the lock; inspect it with "
            f"`deep_think.py status --project {project}`."
        )
    return (
        f"Project {project!r} is already locked by a writer that cannot be "
        f"verified ({details}). After confirming that process has exited, run "
        f"`deep_think.py reconcile --project {project} --release-lock`."
    )


def _retire_lock(lock_path, status):
    retired = lock_path.with_name(f"{LOCK_FILENAME}.stale-{uuid.uuid4().hex}")
    try:
        os.rename(lock_path, retired)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProjectLockedError(
            f"Could not retire the stale project lock {lock_path}: {error}"
        ) from error
    try:
        raw = retired.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raw = None
    if raw != status.get("raw"):
        try:
            os.rename(retired, lock_path)
        except OSError:
            pass
        raise ProjectLockedError(
            "The project lock changed while a stale lock was being recovered; "
            "retry the command."
        )
    # Keep the retired lock until its evidence has been journaled.
    return retired


@contextmanager
def _project_lock(root, project, *, command="ask", release_unverifiable=False):
    project_dir = Path(root) / project
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DeepThinkError(
            f"Could not create project directory {project_dir}: {error}"
        ) from error
    lock_path = project_dir / LOCK_FILENAME
    owner = {
        "schema": LOCK_SCHEMA,
        "lock_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "process_started_at": _current_process_started_at(),
        "created_at": _utc_now(),
        "command": command,
    }
    recovered = None
    for _ in range(5):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            status = _inspect_lock(lock_path)
            if status["state"] == "none":
                continue
            if status["state"] in {"live", "busy"}:
                raise ProjectLockedError(_lock_message(project, status, live=True))
            if status["state"] == "unverifiable" and not release_unverifiable:
                raise ProjectLockedError(_lock_message(project, status, live=False))
            retired = _retire_lock(lock_path, status)
            if retired is not None:
                recovered = {**status, "retired_path": str(retired)}
            continue
        try:
            os.write(descriptor, json.dumps(owner).encode("utf-8"))
            os.fsync(descriptor)
        except OSError as error:
            os.close(descriptor)
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise DeepThinkError(
                f"Could not record the project lock owner: {error}"
            ) from error
        os.close(descriptor)
        break
    else:
        raise ProjectLockedError(
            f"Project {project!r} is already locked: the lock changed repeatedly "
            "while it was being acquired."
        )
    try:
        yield WriterLock(lock_path, owner, recovered)
    finally:
        current = _inspect_lock(lock_path).get("owner") or {}
        if current.get("lock_id") == owner["lock_id"]:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def _parse_lock_owner(raw):
    try:
        owner = json.loads(raw) if raw and raw.strip() else None
    except json.JSONDecodeError:
        return None
    return owner if isinstance(owner, dict) else None


def _retired_locks(project_dir):
    """Yield (path, raw contents or None if unreadable) for retired locks."""
    for path in sorted(Path(project_dir).glob(f"{LOCK_FILENAME}.stale-*")):
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            raw = None
        yield path, raw


def _record_retired_lock(journal, raw, reason):
    owner = _parse_lock_owner(raw)
    legacy = owner is not None and "lock_id" not in owner
    journal.append(
        "stale_lock_recovered",
        previous_owner=owner
        if owner is not None
        else {"raw": (raw or "")[:200], "unreadable": raw is None},
        reason=reason,
        legacy_format=legacy,
    )
    if owner is not None and owner.get("lock_id"):
        for attempt in journal.view().attempts.values():
            if attempt["lock_id"] == owner["lock_id"] and attempt["outcome"] is None:
                journal.append(
                    "submission_unknown",
                    attempt_id=attempt["attempt_id"],
                    turn_id=attempt["turn_id"],
                    reason="the writer stopped after recording the submission "
                    "intent and before recording its outcome",
                )
    elif legacy or raw is None:
        journal.append(
            "legacy_writer_unknown",
            item_id=uuid.uuid4().hex,
            previous_owner=owner,
            reason="a writer without a request journal stopped; whether it left "
            "an Azure request running is unknown",
        )


def _open_project_journal(project_dir, lock):
    journal = RequestJournal(project_dir, lock_id=lock.lock_id)
    recovered = lock.recovered or {}
    for path, raw in _retired_locks(project_dir):
        reason = (
            recovered.get("reason")
            if recovered.get("retired_path") == str(path)
            else "left behind by an interrupted stale-lock recovery"
        )
        _record_retired_lock(journal, raw, reason)
        try:
            path.unlink()
        except OSError:
            pass
    return journal


def _split_text_by_utf8_bytes(text, max_bytes):
    chunks = []
    current = []
    current_bytes = 0
    for character in text:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > max_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        chunks.append("".join(current))
    return chunks


def _request_visible_transcript_summary(
    client,
    *,
    transcript,
    deployment,
    minimum_response_tokens,
    max_attempts,
    retry_base_delay,
    retry_max_delay,
    sleep,
    random_value,
    on_retry,
    poll_timeout=DEFAULT_POLL_TIMEOUT,
    input_limit=MAX_INPUT_TOKENS,
    journal=None,
):
    total_retry_count = 0
    intermediate_tokens = 0
    source_text = transcript
    source_label = "TRANSCRIPT"

    for reduction_round in range(VISIBLE_SUMMARY_MAX_REDUCTION_ROUNDS + 1):
        visible_prompt = (
            VISIBLE_TRANSCRIPT_SUMMARY_PROMPT.rstrip()
            + f"\n\n--- BEGIN {source_label} ---\n"
            + source_text
            + f"\n--- END {source_label} ---"
        )
        visible_input_upper_bound = _estimate_tokens(visible_prompt)
        if visible_input_upper_bound <= input_limit:
            break
        if reduction_round >= VISIBLE_SUMMARY_MAX_REDUCTION_ROUNDS:
            raise DeepThinkError(
                "Visible transcript summary reduction exceeded its bounded rounds."
            )

        chunks = _split_text_by_utf8_bytes(
            source_text,
            min(VISIBLE_SUMMARY_CHUNK_BYTES, input_limit // 2),
        )
        summaries = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_prompt = (
                VISIBLE_TRANSCRIPT_CHUNK_PROMPT.rstrip()
                + f"\n\nChunk {index} of {len(chunks)}"
                + "\n\n--- BEGIN CHUNK ---\n"
                + chunk
                + "\n--- END CHUNK ---"
            )
            chunk_outcome = request_response(
                client,
                build_response_request(
                    [
                        {
                            "type": "message",
                            "role": "user",
                            "content": chunk_prompt,
                        }
                    ],
                    deployment,
                    VISIBLE_CHUNK_OUTPUT_TOKENS,
                ),
                purpose=f"visible transcript chunk {index}",
                max_attempts=max_attempts,
                base_delay=retry_base_delay,
                max_delay=retry_max_delay,
                sleep=sleep,
                random_value=random_value,
                on_retry=on_retry,
                poll_timeout=poll_timeout,
                journal=journal,
            )
            total_retry_count += chunk_outcome.retry_count
            intermediate_tokens += _response_usage(chunk_outcome.response)[
                "total_tokens"
            ]
            summaries.append(
                f"## Chunk {index} of {len(chunks)}\n\n"
                f"{chunk_outcome.response.output_text.rstrip()}"
            )
        reduced_text = "\n\n".join(summaries)
        if _estimate_tokens(reduced_text) >= _estimate_tokens(source_text):
            raise DeepThinkError(
                "Chunk summaries did not reduce the visible transcript enough "
                "to fit a final rollover-summary request."
            )
        source_text = reduced_text
        source_label = f"CHUNK SUMMARIES ROUND {reduction_round + 1}"

    visible_output_budget = min(
        MAX_OUTPUT_TOKENS,
        CONTEXT_WINDOW_TOKENS - visible_input_upper_bound,
    )
    if visible_output_budget < minimum_response_tokens:
        raise DeepThinkError(
            "The visible transcript leaves insufficient room for a rollover summary."
        )
    final_outcome = request_response(
        client,
        build_response_request(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": visible_prompt,
                }
            ],
            deployment,
            visible_output_budget,
        ),
        purpose="visible transcript rollover summary",
        max_attempts=max_attempts,
        base_delay=retry_base_delay,
        max_delay=retry_max_delay,
        sleep=sleep,
        random_value=random_value,
        on_retry=on_retry,
        poll_timeout=poll_timeout,
        journal=journal,
    )
    return RequestOutcome(
        final_outcome.response,
        total_retry_count + final_outcome.retry_count,
        final_outcome.deployment,
        intermediate_tokens=intermediate_tokens,
    )


def _rollover_volume(
    client,
    *,
    state,
    project_dir,
    context_path,
    transcript_path,
    history,
    transcript,
    pending_json,
    pending_text,
    minimum_response_tokens,
    max_attempts,
    retry_base_delay,
    retry_max_delay,
    sleep,
    random_value,
    on_retry,
    force_visible_transcript=False,
    force_visible_reason=None,
    poll_timeout=DEFAULT_POLL_TIMEOUT,
    recover_service_errors=False,
    service_recovery=False,
    journal=None,
):
    summary_input_upper_bound = state["context_tokens"] + _estimate_tokens(
        ROLLOVER_SUMMARY_PROMPT
    )
    summary_item = {
        "type": "message",
        "role": "user",
        "content": ROLLOVER_SUMMARY_PROMPT,
    }
    recovery_note = None
    if force_visible_transcript:
        _retry_event(
            "rollover summary",
            1,
            1,
            force_visible_reason or "using visible transcript recovery",
            0,
            on_retry,
        )
        summary_outcome = _request_visible_transcript_summary(
            client,
            transcript=transcript,
            deployment=state["deployment"],
            minimum_response_tokens=minimum_response_tokens,
            max_attempts=max_attempts,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            sleep=sleep,
            random_value=random_value,
            on_retry=on_retry,
            poll_timeout=poll_timeout,
            input_limit=(
                VISIBLE_SUMMARY_CHUNK_BYTES if service_recovery else MAX_INPUT_TOKENS
            ),
            journal=journal,
        )
        recovery_note = "visible transcript recovery"
    elif summary_input_upper_bound > MAX_INPUT_TOKENS:
        _retry_event(
            "rollover summary",
            1,
            1,
            "opaque context is unreplayable; using visible transcript",
            0,
            on_retry,
        )
        summary_outcome = _request_visible_transcript_summary(
            client,
            transcript=transcript,
            deployment=state["deployment"],
            minimum_response_tokens=minimum_response_tokens,
            max_attempts=max_attempts,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            sleep=sleep,
            random_value=random_value,
            on_retry=on_retry,
            poll_timeout=poll_timeout,
            journal=journal,
        )
        recovery_note = "visible transcript recovery"
    else:
        summary_output_budget = min(
            MAX_OUTPUT_TOKENS,
            CONTEXT_WINDOW_TOKENS - summary_input_upper_bound,
        )
        if summary_output_budget < minimum_response_tokens:
            raise DeepThinkError(
                "Insufficient context space to generate a rollover summary."
            )
        try:
            summary_outcome = request_response(
                client,
                build_response_request(
                    [*history, summary_item],
                    state["deployment"],
                    summary_output_budget,
                ),
                purpose="rollover summary",
                max_attempts=max_attempts,
                base_delay=retry_base_delay,
                max_delay=retry_max_delay,
                sleep=sleep,
                random_value=random_value,
                on_retry=on_retry,
                poll_timeout=poll_timeout,
                journal=journal,
            )
        except (ContextLimitError, OpaqueReplayError, TerminalServiceError) as error:
            service_recovery = isinstance(error, TerminalServiceError)
            if service_recovery and not recover_service_errors:
                raise
            _retry_event(
                "rollover summary",
                1,
                1,
                (
                    f"terminal server_error; retrying from visible transcript: {error}"
                    if service_recovery
                    else "retrying from visible transcript"
                ),
                0,
                on_retry,
            )
            try:
                summary_outcome = _request_visible_transcript_summary(
                    client,
                    transcript=transcript,
                    deployment=state["deployment"],
                    minimum_response_tokens=minimum_response_tokens,
                    max_attempts=max_attempts,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                    sleep=sleep,
                    random_value=random_value,
                    on_retry=on_retry,
                    poll_timeout=poll_timeout,
                    input_limit=(
                        VISIBLE_SUMMARY_CHUNK_BYTES
                        if service_recovery
                        else MAX_INPUT_TOKENS
                    ),
                    journal=journal,
                )
            except ContextLimitError as recovery_error:
                raise DeepThinkError(
                    "Azure rejected both normal and visible-transcript "
                    "rollover summaries for context length."
                ) from recovery_error
            recovery_note = "visible transcript recovery"
    if service_recovery:
        recovery_note = (
            "visible transcript recovery after terminal server_error; "
            "service-side cause unknown"
        )
    summary_response = summary_outcome.response
    summary_usage = _response_usage(summary_response)
    carried_summary = summary_response.output_text.rstrip()
    pending_json[context_path] = [
        *history,
        summary_item,
        *_response_output(summary_response),
    ]
    pending_text[transcript_path] = transcript + _rollover_markdown(
        summary_response,
        summary_usage,
        summary_outcome.retry_count,
        recovery_note,
        summary_outcome.deployment,
        summary_outcome.intermediate_tokens,
    )
    state["cumulative_tokens"] += (
        summary_usage["total_tokens"] + summary_outcome.intermediate_tokens
    )
    state["volume"] += 1
    state["turn"] = 0
    state["context_tokens"] = _estimate_carried_context_tokens(
        carried_summary,
        summary_usage,
    )
    state["last_response_id"] = None
    state["updated_at"] = _utc_now()

    context_path, transcript_path = _volume_paths(project_dir, state["volume"])
    carried_context = f"{CARRIED_CONTEXT_PREFIX.rstrip()}\n\n{carried_summary}"
    history = [
        {
            "type": "message",
            "role": "developer",
            "content": carried_context,
        }
    ]
    transcript = (
        _transcript_header(state)
        + "\n## Carried context\n\n"
        + summary_response.output_text.rstrip()
        + "\n"
    )
    pending_json[context_path] = history
    pending_text[transcript_path] = transcript
    return (
        context_path,
        transcript_path,
        history,
        transcript,
        carried_context,
    )


def _begin_journaled_turn(
    journal,
    *,
    project,
    prompt,
    deployment,
    title,
    rollover_tokens,
    recover_service_errors,
    state_sha256,
    committed_response_id,
    resume_turn_id,
):
    view = journal.view()
    unknown = [a for a in view.attempts.values() if view.attempt_state(a) == "unknown"]
    if unknown:
        attempt = unknown[-1]
        raise SubmissionUnknownError(
            _submission_unknown_message(
                attempt["purpose"] or "request",
                RouteTarget(attempt["deployment"], None, attempt["resource"]),
                attempt["attempt_id"],
                attempt["reason"] or "the writer stopped before recording the outcome",
            )
        )
    if view.unresolved_legacy():
        raise RemoteStateError(
            f"A pre-journal writer for project {project!r} stopped without "
            "recording whether it left an Azure request running. Verify with "
            "Azure telemetry, then run `reconcile --response-id ID --endpoint URL`, "
            "or `reconcile --confirm-no-remote-job --reason TEXT` after confirming "
            "that no job remains active."
        )
    prompt_sha256 = _sha256_text(prompt)
    turn = view.open_turn()
    if turn is not None:
        attempts = view.turn_attempts(turn["turn_id"])
        if committed_response_id is not None and any(
            attempt["response_id"] == committed_response_id
            and attempt["terminal_status"] == "completed"
            for attempt in attempts
        ):
            journal.append(
                "turn_committed",
                turn_id=turn["turn_id"],
                reason="the committed state already contains this turn's answer",
            )
            journal.release_turn_artifacts(turn["turn_id"])
            turn = None
    if turn is not None:
        matches = (
            turn["prompt_sha256"] == prompt_sha256
            and turn["deployment"] == deployment
            and state_sha256 in [turn["base_state_sha256"], *turn["checkpoints"]]
        )
        if matches and resume_turn_id in {None, turn["turn_id"]}:
            journal.turn_id = turn["turn_id"]
            journal.append("turn_resumed", state_sha256=state_sha256)
            # The kind of checkpoint resumed from, or None at the turn's start.
            return turn["checkpoint_kinds"].get(state_sha256)
        outstanding = [
            attempt
            for attempt in attempts
            if view.attempt_state(attempt) in OUTSTANDING_ATTEMPT_STATES
        ]
        recoverable = view.recoverable(turn["turn_id"])
        if outstanding or recoverable or resume_turn_id is not None:
            kind = "running" if outstanding else "completed but uncommitted"
            ids = ", ".join(
                attempt["response_id"] or attempt["attempt_id"]
                for attempt in outstanding + recoverable
            )
            prefix = (
                "The unfinished turn cannot be resumed because the committed "
                "project state changed after it started. "
                if resume_turn_id is not None
                else ""
            )
            detail = f" with {kind} request(s) {ids}" if ids else ""
            raise RemoteStateError(
                f"{prefix}Project {project!r} has an unfinished turn{detail}. "
                "Run `resume` to finish it, `cancel` to stop running jobs, or "
                "`reconcile --abandon-turn --reason TEXT` to discard it."
            )
        journal.append(
            "turn_superseded",
            turn_id=turn["turn_id"],
            reason="a different turn started; no running or uncommitted requests "
            "remained",
        )
    elif resume_turn_id is not None:
        raise DeepThinkError(
            f"Project {project!r} has no unfinished turn; nothing to resume."
        )
    journal.turn_id = uuid.uuid4().hex
    prompt_file = journal.save_prompt(journal.turn_id, prompt)
    journal.append(
        "turn_started",
        project=project,
        prompt_sha256=prompt_sha256,
        prompt_file=prompt_file,
        base_state_sha256=state_sha256,
        deployment=deployment,
        title=title,
        rollover_tokens=rollover_tokens,
        recover_service_errors=recover_service_errors,
    )
    return None


def _run_turn_locked(
    client,
    *,
    root,
    project,
    prompt,
    deployment,
    journal,
    title=None,
    rollover_tokens=DEFAULT_ROLLOVER_TOKENS,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    retry_base_delay=DEFAULT_RETRY_BASE_DELAY,
    retry_max_delay=DEFAULT_RETRY_MAX_DELAY,
    sleep=time.sleep,
    random_value=random.random,
    on_retry=None,
    poll_timeout=DEFAULT_POLL_TIMEOUT,
    recover_service_errors=False,
    resume_turn_id=None,
    upgradable_deployments=DEFAULT_FALLBACK_DEPLOYMENTS,
):
    _validate_project(project)
    if not prompt.strip():
        raise DeepThinkError("Prompt must not be empty.")
    if not 1 <= rollover_tokens <= DEFAULT_ROLLOVER_TOKENS:
        raise DeepThinkError(
            f"Rollover tokens must be between 1 and {DEFAULT_ROLLOVER_TOKENS:,}."
        )
    _validate_retry_settings(
        max_attempts,
        retry_base_delay,
        retry_max_delay,
    )
    _validate_poll_timeout(poll_timeout)

    project_dir = Path(root) / project
    state_path = project_dir / "state.json"
    existing_project = state_path.exists()
    state_sha256 = _file_sha256(state_path) if existing_project else None
    if existing_project:
        state = _read_json(state_path)
        _validate_state(state, project)
        if title and title != state["title"]:
            raise DeepThinkError(
                f"Project title is already {state['title']!r}; omit --title."
            )
        upgrading = (
            deployment == DEFAULT_DEPLOYMENT
            and state["deployment"] in upgradable_deployments
        )
        if deployment != state["deployment"] and not upgrading:
            raise DeepThinkError(
                f"Project deployment is already {state['deployment']!r}."
            )
    else:
        state = _new_state(
            project,
            title or project.replace("-", " ").title(),
            deployment,
            rollover_tokens,
        )

    context_path, transcript_path = _volume_paths(project_dir, state["volume"])
    if existing_project and not context_path.is_file():
        raise DeepThinkError(f"Current context file is missing: {context_path}")
    if existing_project and not transcript_path.is_file():
        raise DeepThinkError(f"Current transcript file is missing: {transcript_path}")
    history = _read_json(context_path) if existing_project else []
    if not isinstance(history, list) or not all(
        isinstance(item, dict) for item in history
    ):
        raise DeepThinkError(
            f"Context file must contain a JSON array of objects: {context_path}"
        )
    transcript = (
        _read_text(transcript_path, label="transcript")
        if existing_project
        else _transcript_header(state)
    )
    if existing_project:
        _verify_file_checksum(
            context_path,
            state.get("context_sha256"),
            "context",
        )
        _verify_file_checksum(
            transcript_path,
            state.get("transcript_sha256"),
            "transcript",
        )
    resumed_checkpoint = _begin_journaled_turn(
        journal,
        project=project,
        prompt=prompt,
        deployment=deployment,
        title=title,
        rollover_tokens=rollover_tokens,
        recover_service_errors=recover_service_errors,
        state_sha256=state_sha256,
        committed_response_id=state.get("last_response_id"),
        resume_turn_id=resume_turn_id,
    )
    if existing_project and deployment != state["deployment"]:
        transcript += (
            f"\nPrimary deployment upgraded from `{state['deployment']}` "
            f"to `{deployment}`; preceding context retained.\n"
        )
        state["deployment"] = deployment
    pending_json = {}
    pending_text = {}
    # Resuming from a checkpoint means this turn already produced the volume.
    rolled_over = resumed_checkpoint is not None
    # The single reactive recovery allowance persists through checkpoints.
    reactive_recovery_used = resumed_checkpoint == "reactive"
    checkpointed = False

    def checkpoint(kind):
        nonlocal checkpointed
        _write_pending_state(
            state_path,
            state,
            context_path=context_path,
            transcript_path=transcript_path,
            pending_json=pending_json,
            pending_text=pending_text,
        )
        journal.append(
            "turn_checkpoint", state_sha256=_file_sha256(state_path), kind=kind
        )
        journal.release_payloads(journal.turn_id)
        checkpointed = True

    def budget_after_rollover(kind):
        # A completed rollover is paid work; keep it even if the prompt fails.
        try:
            return _normal_output_budget(
                state["context_tokens"],
                prompt,
                state["rollover_tokens"],
            )
        except DeepThinkError:
            if not checkpointed:
                checkpoint(kind)
            raise

    minimum_response_tokens = _minimum_response_tokens(state["rollover_tokens"])
    input_upper_bound = state["context_tokens"] + _estimate_tokens(prompt)
    output_budget = (
        -1
        if history and input_upper_bound > MAX_INPUT_TOKENS
        else _normal_output_budget(
            state["context_tokens"],
            prompt,
            state["rollover_tokens"],
        )
    )
    if history and output_budget < minimum_response_tokens:
        minimum_carried_tokens = (
            _estimate_message_tokens("developer", "")
            + _estimate_tokens(f"{CARRIED_CONTEXT_PREFIX.rstrip()}\n\n")
            + 1
        )
        try:
            best_case_budget = _normal_output_budget(
                minimum_carried_tokens, prompt, state["rollover_tokens"]
            )
        except DeepThinkError:
            best_case_budget = -1
        if best_case_budget < minimum_response_tokens or resumed_checkpoint:
            # A rollover cannot make room, or this turn already rolled over:
            # do not pay for another summary.
            raise DeepThinkError(
                "Prompt leaves too little room for reasoning and output even "
                "after a rollover; split it into smaller turns."
            )
        (
            context_path,
            transcript_path,
            history,
            transcript,
            _carried_context,
        ) = _rollover_volume(
            client,
            state=state,
            project_dir=project_dir,
            context_path=context_path,
            transcript_path=transcript_path,
            history=history,
            transcript=transcript,
            pending_json=pending_json,
            pending_text=pending_text,
            minimum_response_tokens=minimum_response_tokens,
            max_attempts=max_attempts,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            sleep=sleep,
            random_value=random_value,
            on_retry=on_retry,
            poll_timeout=poll_timeout,
            recover_service_errors=recover_service_errors,
            journal=journal,
        )
        rolled_over = True
        if recover_service_errors:
            checkpoint("proactive")
        output_budget = budget_after_rollover("proactive")

    if output_budget < minimum_response_tokens:
        if pending_json and not checkpointed:
            checkpoint("proactive")
        raise DeepThinkError(
            "Prompt leaves too little room for reasoning and output; split it "
            "into a smaller first turn."
        )

    user_item = {
        "type": "message",
        "role": "user",
        "content": prompt,
    }
    recovery_count = 0
    try:
        outcome = request_response(
            client,
            build_response_request(
                [*history, user_item],
                deployment,
                output_budget,
            ),
            purpose="answer",
            max_attempts=max_attempts,
            base_delay=retry_base_delay,
            max_delay=retry_max_delay,
            sleep=sleep,
            random_value=random_value,
            on_retry=on_retry,
            poll_timeout=poll_timeout,
            journal=journal,
        )
    except (
        ContextLimitError,
        OutputLimitError,
        OpaqueReplayError,
        TerminalServiceError,
    ) as error:
        service_recovery = isinstance(error, TerminalServiceError)
        if service_recovery and (
            not recover_service_errors or not history or rolled_over
        ):
            raise
        exhausted_output_budget = output_budget
        if isinstance(error, OutputLimitError) and output_budget >= MAX_OUTPUT_TOKENS:
            raise DeepThinkError(
                "Azure exhausted the model's 128,000-token output limit; "
                "split the request into smaller turns."
            ) from error
        if reactive_recovery_used:
            raise DeepThinkError(
                "Azure rejected the answer context after rollover recovery; "
                "split the prompt into a smaller turn."
            ) from error
        if not history:
            raise DeepThinkError(
                "Azure rejected the first-turn context; split the prompt into "
                "smaller source batches."
            ) from error
        recovery_reason = (
            f"terminal server_error; retrying from visible transcript: {error}"
            if service_recovery
            else (
                "opaque replay failure; retrying from visible transcript"
                if isinstance(error, OpaqueReplayError)
                else (
                    "output limit; forcing rollover"
                    if isinstance(error, OutputLimitError)
                    else "context limit; forcing rollover"
                )
            )
        )
        _retry_event(
            "answer",
            1,
            1,
            recovery_reason,
            0,
            on_retry,
        )
        (
            context_path,
            transcript_path,
            history,
            transcript,
            _carried_context,
        ) = _rollover_volume(
            client,
            state=state,
            project_dir=project_dir,
            context_path=context_path,
            transcript_path=transcript_path,
            history=history,
            transcript=transcript,
            pending_json=pending_json,
            pending_text=pending_text,
            minimum_response_tokens=minimum_response_tokens,
            max_attempts=max_attempts,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            sleep=sleep,
            random_value=random_value,
            on_retry=on_retry,
            poll_timeout=poll_timeout,
            recover_service_errors=recover_service_errors and not rolled_over,
            service_recovery=service_recovery,
            force_visible_transcript=(
                service_recovery or isinstance(error, OpaqueReplayError)
            ),
            force_visible_reason=(
                recovery_reason
                if service_recovery
                else (
                    "invalid_encrypted_content; retrying from visible transcript"
                    if isinstance(error, OpaqueReplayError)
                    else None
                )
            ),
            journal=journal,
        )
        rolled_over = True
        reactive_recovery_used = True
        recovery_count = 1
        if recover_service_errors:
            checkpoint("reactive")
        output_budget = budget_after_rollover("reactive")
        if (
            isinstance(error, OutputLimitError)
            and output_budget <= exhausted_output_budget
        ):
            if not checkpointed:
                checkpoint("reactive")
            raise DeepThinkError(
                "Fresh rollover output budget is not larger than the exhausted "
                "answer budget; split or narrow the request and continue from "
                "the new volume."
            ) from error
        if output_budget < minimum_response_tokens:
            if not checkpointed:
                checkpoint("reactive")
            raise DeepThinkError(
                "Prompt remains too large after context rollover."
            ) from error
        try:
            outcome = request_response(
                client,
                build_response_request(
                    [*history, user_item],
                    deployment,
                    output_budget,
                ),
                purpose="answer after rollover",
                max_attempts=max_attempts,
                base_delay=retry_base_delay,
                max_delay=retry_max_delay,
                sleep=sleep,
                random_value=random_value,
                on_retry=on_retry,
                poll_timeout=poll_timeout,
                journal=journal,
            )
        except (OutputLimitError, ContextLimitError, OpaqueReplayError):
            # Retrying cannot change this outcome, so keep the paid rollover
            # and let the caller narrow the request from the new volume.
            if not checkpointed:
                checkpoint("reactive")
            raise
    response = outcome.response
    _require_complete(response)

    usage = _response_usage(response)
    updated_history = [*history, user_item, *_response_output(response)]
    state["turn"] += 1
    state["context_tokens"] = usage["total_tokens"]
    state["cumulative_tokens"] += usage["total_tokens"]
    state["last_response_id"] = response.id
    state["updated_at"] = _utc_now()

    transcript += _turn_markdown(
        state["turn"],
        prompt,
        response,
        usage,
        outcome.retry_count + recovery_count,
        outcome.deployment,
    )

    pending_json[context_path] = updated_history
    pending_text[transcript_path] = transcript
    _write_pending_state(
        state_path,
        state,
        context_path=context_path,
        transcript_path=transcript_path,
        pending_json=pending_json,
        pending_text=pending_text,
    )
    journal.append_best_effort(
        "turn_committed",
        state_sha256=_file_sha256(state_path),
        volume=state["volume"],
        turn=state["turn"],
        response_id=response.id,
    )
    journal.release_turn_artifacts(journal.turn_id)

    return TurnResult(
        response.output_text,
        state["volume"],
        rolled_over,
        transcript_path,
    )


def _run_journaled_turn(client, journal, **options):
    try:
        return _run_turn_locked(client, journal=journal, **options)
    except BaseException as error:
        if journal.turn_id is not None:
            journal.append_best_effort(
                "turn_error",
                error=_redact_secrets(f"{type(error).__name__}: {error}"),
            )
        raise


def run_turn(
    client,
    *,
    root,
    project,
    prompt,
    deployment,
    title=None,
    rollover_tokens=DEFAULT_ROLLOVER_TOKENS,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    retry_base_delay=DEFAULT_RETRY_BASE_DELAY,
    retry_max_delay=DEFAULT_RETRY_MAX_DELAY,
    sleep=time.sleep,
    random_value=random.random,
    on_retry=None,
    poll_timeout=DEFAULT_POLL_TIMEOUT,
    recover_service_errors=False,
    upgradable_deployments=DEFAULT_FALLBACK_DEPLOYMENTS,
):
    _validate_project(project)
    with _project_lock(root, project, command="ask") as lock:
        journal = _open_project_journal(Path(root) / project, lock)
        return _run_journaled_turn(
            client,
            journal,
            root=root,
            project=project,
            prompt=prompt,
            deployment=deployment,
            title=title,
            rollover_tokens=rollover_tokens,
            max_attempts=max_attempts,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            sleep=sleep,
            random_value=random_value,
            on_retry=on_retry,
            poll_timeout=poll_timeout,
            recover_service_errors=recover_service_errors,
            upgradable_deployments=upgradable_deployments,
        )


def _existing_project_dir(root, project):
    _validate_project(project)
    project_dir = Path(root) / project
    if not project_dir.is_dir():
        raise DeepThinkError(f"Project directory not found: {project_dir}")
    return project_dir


def resume_turn(
    client_builder,
    *,
    root,
    project,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    retry_base_delay=DEFAULT_RETRY_BASE_DELAY,
    retry_max_delay=DEFAULT_RETRY_MAX_DELAY,
    sleep=time.sleep,
    random_value=random.random,
    on_retry=None,
    poll_timeout=DEFAULT_POLL_TIMEOUT,
    upgradable_deployments=DEFAULT_FALLBACK_DEPLOYMENTS,
):
    """Finish the journaled unfinished turn without duplicating known requests."""
    project_dir = _existing_project_dir(root, project)
    with _project_lock(root, project, command="resume") as lock:
        journal = _open_project_journal(project_dir, lock)
        turn = journal.view().open_turn()
        if turn is None:
            raise DeepThinkError(
                f"Project {project!r} has no unfinished turn; nothing to resume."
            )
        prompt = journal.read_prompt(turn)
        return _run_journaled_turn(
            client_builder(turn["deployment"]),
            journal,
            root=root,
            project=project,
            prompt=prompt,
            deployment=turn["deployment"],
            title=turn["title"],
            rollover_tokens=turn["rollover_tokens"],
            max_attempts=max_attempts,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            sleep=sleep,
            random_value=random_value,
            on_retry=on_retry,
            poll_timeout=poll_timeout,
            recover_service_errors=bool(turn["recover_service_errors"]),
            resume_turn_id=turn["turn_id"],
            upgradable_deployments=upgradable_deployments,
        )


def _attempt_summary(attempt, state):
    return {
        "attempt_id": attempt["attempt_id"],
        "turn_id": attempt["turn_id"],
        "state": state,
        "purpose": attempt["purpose"],
        "response_id": attempt["response_id"],
        "deployment": attempt["deployment"],
        "resource": attempt["resource"],
        "provider": _attempt_provider(attempt),
        "role": attempt["role"],
        "target": f"{attempt['target_index']}/{attempt['target_count']}",
        "submitted_at": attempt["submitted_at"],
        "last_status": attempt["last_status"],
        "poll_stopped": attempt["poll_stopped"],
        "reason": attempt["reason"],
        "request_sha256": attempt["request_sha256"],
        "request_bytes": attempt["request_bytes"],
        "max_output_tokens": attempt["max_output_tokens"],
    }


def _committed_summary(project_dir):
    state_path = project_dir / "state.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {"error": str(error)}
    if not isinstance(state, dict):
        return {"error": "state.json is not a JSON object"}
    return {
        "volume": state.get("volume"),
        "turn": state.get("turn"),
        "deployment": state.get("deployment"),
        "last_response_id": state.get("last_response_id"),
        "state_file_sha256": _file_sha256(state_path),
    }


def _next_steps(project, lock, outstanding, unresolved, recoverable):
    # Recovery must use the provider that accepted the jobs; Azure is the default.
    providers = {item["provider"] for item in (*outstanding, *recoverable)}
    flag = " --auth openai-key" if providers == {"openai"} else ""
    command = "deep_think.py"
    steps = []
    if {"azure", "openai"} <= providers:
        steps.append(
            "Jobs span Azure OpenAI and the OpenAI API; list both in --auth "
            "(for example --auth entra,openai-key) for the commands below."
        )
    if lock["state"] in {"live", "busy"}:
        steps.append(
            f"A writer holds the project ({lock.get('reason')}). Wait for it to "
            "finish; do not delete the lock."
        )
    if lock["state"] == "unverifiable":
        steps.append(
            "After confirming the lock owner has exited, run "
            f"`{command} reconcile --project {project}{flag} --release-lock`."
        )
    active = [item["response_id"] for item in outstanding if item["state"] == "active"]
    if active:
        steps.append(
            f"Run `{command} resume --project {project}{flag}` to keep polling "
            f"{', '.join(active)} on the original resource without resubmitting, "
            f"or `{command} cancel --project {project}{flag}` to stop it. "
            f"`{command} reconcile --project {project}{flag}` records current "
            "remote status."
        )
    if recoverable:
        steps.append(
            f"Run `{command} resume --project {project}{flag}` to commit the cached "
            "completed response without a new request."
        )
    if unresolved or any(item["state"] == "unknown" for item in outstanding):
        steps.append(
            "The service may have accepted a request whose response ID was never "
            "recorded. Search Azure telemetry or the OpenAI dashboard logs using "
            "the recorded time, deployment, resource, and request SHA-256."
        )
        steps.append(
            f"If found, run `{command} reconcile --project {project}{flag} --attempt "
            "ATTEMPT --response-id ID` (for a pre-journal writer: `--response-id ID "
            "--endpoint URL`). If you verify that no job remains active, run "
            f"`{command} reconcile --project {project}{flag} --confirm-no-remote-job "
            "--reason TEXT`."
        )
    return steps or ["No recovery action is required."]


def project_status(root, project):
    """Return read-only local and journaled remote request state."""
    project_dir = _existing_project_dir(root, project)
    lock = _inspect_lock(project_dir / LOCK_FILENAME)
    journal_path = project_dir / JOURNAL_DIRECTORY / JOURNAL_FILENAME
    records, torn_tail = _read_journal(journal_path)
    view = JournalView(records, torn_tail=torn_tail)
    owner = lock.get("owner") or {}
    live_lock_id = owner.get("lock_id") if lock["state"] in {"live", "busy"} else None
    outstanding = [
        _attempt_summary(attempt, view.attempt_state(attempt, live_lock_id))
        for attempt in view.outstanding(live_lock_id)
    ]
    unresolved = [
        {
            "item_id": item["item_id"],
            "recorded_at": item["recorded_at"],
            "previous_owner": item["previous_owner"],
        }
        for item in view.unresolved_legacy()
    ]
    if (
        lock["state"] in {"stale", "unverifiable"}
        and lock.get("legacy_format")
        and lock.get("owner") is not None
    ):
        unresolved.append(
            {
                "item_id": None,
                "recorded_at": None,
                "previous_owner": lock["owner"],
                "note": "pre-journal lock; it is recorded as unresolved when a "
                "writer recovers it",
            }
        )
    for _path, raw in _retired_locks(project_dir):
        owner = _parse_lock_owner(raw)
        if raw is None or (owner is not None and "lock_id" not in owner):
            unresolved.append(
                {
                    "item_id": None,
                    "recorded_at": None,
                    "previous_owner": owner,
                    "note": "retired pre-journal lock awaiting its journal record",
                }
            )
    turn = view.open_turn()
    recoverable = [
        _attempt_summary(attempt, view.attempt_state(attempt, live_lock_id))
        for attempt in (view.recoverable(turn["turn_id"]) if turn else [])
    ]
    blocking = []
    if lock["state"] in {"live", "busy", "unverifiable"}:
        blocking.append(f"writer lock is {lock['state']}: {lock.get('reason')}")
    blocking.extend(
        f"{item['state']} request {item['response_id'] or item['attempt_id']}"
        for item in outstanding
    )
    if unresolved:
        blocking.append("a pre-journal writer may have left an Azure request running")
    if recoverable:
        blocking.append("the unfinished turn has completed but uncommitted responses")
    return {
        "project": project,
        "project_dir": str(project_dir),
        "writer_lock": {
            "state": lock["state"],
            "reason": lock.get("reason"),
            "owner": lock.get("owner"),
            "legacy_format": bool(lock.get("legacy_format")),
        },
        "committed": _committed_summary(project_dir),
        "journal": {
            "path": str(journal_path),
            "records": len(records),
            "torn_tail": torn_tail,
        },
        "open_turn": (
            {
                key: turn[key]
                for key in (
                    "turn_id",
                    "started_at",
                    "deployment",
                    "prompt_sha256",
                    "base_state_sha256",
                    "last_error",
                )
            }
            if turn
            else None
        ),
        "outstanding": outstanding,
        "unresolved_unknown": unresolved,
        "recoverable": recoverable,
        "can_submit_new_request": not blocking,
        "blocking": blocking,
        "next_steps": _next_steps(project, lock, outstanding, unresolved, recoverable),
    }


def _api_errors():
    try:
        from openai import APIConnectionError, APIStatusError
    except ImportError as error:
        raise DeepThinkError(
            "Install dependencies with: "
            "python -m pip install --upgrade openai azure-identity"
        ) from error
    return APIConnectionError, APIStatusError


def _remote_call(
    client_factory, clients, resource, operation, response_id, service="Azure"
):
    """Run one retrieve/cancel call; return (kind, response or message).

    Errors are returned rather than raised, so one job's failure (a refused
    endpoint, a missing credential, or a failed sign-in) never stops recovery
    of the others. Nothing is sent when the client cannot be built.
    """
    connection_error, status_error = _api_errors()
    try:
        if resource not in clients:
            clients[resource] = client_factory(resource)
        response = getattr(clients[resource].responses, operation)(response_id)
    except DeepThinkError as error:
        return "error", _redact_secrets(error)
    except status_error as error:
        status_code, _code, message = _status_error_details(error)
        if status_code == 404:
            return "not_found", f"{service} HTTP 404: {message}"
        return "error", f"{service} HTTP {status_code}: {message}"
    except connection_error as error:
        return "error", f"{type(error).__name__}: {error}"
    except Exception as error:
        # The SDK fetches Entra tokens before its transport error handling.
        if _is_credential_error(error):
            return "error", (
                f"{service} authentication failed while calling {operation} for "
                f"{response_id}: {_redact_secrets(error)} Fix authentication (for "
                "example, `az login`) and retry; no remote state was changed by "
                "this call."
            )
        raise
    return "response", response


def _provider_factories(client_factory, provider):
    """Map providers to client factories; a bare factory serves one provider."""
    if isinstance(client_factory, Mapping):
        return dict(client_factory)
    return {provider: client_factory}


def cancel_requests(
    client_factory, *, root, project, response_ids=(), endpoint=None, provider="azure"
):
    """Cancel journaled (or explicitly identified) background responses.

    client_factory is one factory for provider, or a mapping of provider to
    factory; each job is contacted only through the provider that accepted it.
    """
    factories = _provider_factories(client_factory, provider)
    first_provider = next(iter(factories))
    project_dir = _existing_project_dir(root, project)
    results = []
    with _project_lock(root, project, command="cancel") as lock:
        journal = _open_project_journal(project_dir, lock)
        view = journal.view()
        if response_ids:
            plan = []
            for response_id in response_ids:
                attempt = view.attempt_for_response(response_id)
                if attempt is None and not endpoint:
                    raise DeepThinkError(
                        f"Response {response_id} is not in the request journal; "
                        "pass --endpoint with its original resource."
                    )
                resource = attempt["resource"] if attempt else endpoint
                plan.append((response_id, resource, attempt))
        else:
            plan = [
                (attempt["response_id"], attempt["resource"], attempt)
                for attempt in view.outstanding()
                if view.attempt_state(attempt) == "active"
            ]
        clients = {}
        for response_id, resource, attempt in plan:
            mismatch = _provider_mismatch(attempt, factories) if attempt else None
            if mismatch is not None:
                results.append(
                    {"response_id": response_id, "status": None, "error": mismatch}
                )
                continue
            owner = _attempt_provider(attempt) if attempt else first_provider
            factory = factories[owner]
            owner_clients = clients.setdefault(owner, {})
            attempt_fields = (
                {"attempt_id": attempt["attempt_id"], "turn_id": attempt["turn_id"]}
                if attempt
                else {}
            )
            kind, value = _remote_call(
                factory,
                owner_clients,
                resource,
                "cancel",
                response_id,
                SERVICE_LABELS.get(owner, "Azure"),
            )
            if kind == "error" and attempt is not None:
                # Azure rejects cancelling finished jobs (HTTP 400), so record
                # the job's actual state instead of leaving it marked active.
                observed_kind, observed = _remote_call(
                    factory,
                    owner_clients,
                    resource,
                    "retrieve",
                    response_id,
                    SERVICE_LABELS.get(owner, "Azure"),
                )
                if observed_kind == "response":
                    status, note = _record_observed_response(journal, attempt, observed)
                    results.append(
                        {
                            "response_id": response_id,
                            "status": status,
                            "resource": resource,
                            "note": f"cancel failed ({value}); {note}",
                        }
                    )
                    continue
                if observed_kind == "not_found":
                    kind, value = observed_kind, observed
            if kind != "response":
                if kind == "not_found" and attempt is not None:
                    value = _record_not_found(journal, attempt, response_id)
                results.append(
                    {"response_id": response_id, "status": None, "error": value}
                )
                continue
            status = _safe_response_status(value)
            journal.append(
                "cancel_requested",
                response_id=response_id,
                resource=resource,
                status=status,
                **attempt_fields,
            )
            results.append(
                {"response_id": response_id, "status": status, "resource": resource}
            )
    if not results:
        results_message = "No active journaled responses to cancel."
    else:
        results_message = f"Processed {len(results)} cancellation request(s)."
    return {
        "message": results_message,
        "results": results,
        "status": project_status(root, project),
    }


def _record_not_found(journal, attempt, response_id):
    fields = {
        "attempt_id": attempt["attempt_id"],
        "turn_id": attempt["turn_id"],
        "response_id": response_id,
    }
    if attempt["attached_unverified"]:
        journal.append(
            "attachment_rejected",
            reason="the operator-attached response ID was not found on the "
            "attempt's recorded resource",
            **fields,
        )
        return (
            f"{response_id}: not found on the attempt's recorded resource; the "
            "submission remains unknown"
        )
    if _attempt_provider(attempt) == "openai":
        # OpenAI scopes responses to the project that created them, so a 404
        # under the current key does not prove that the job stopped.
        journal.append(
            "remote_not_visible",
            reason="OpenAI returned HTTP 404 for the recorded response ID",
            **fields,
        )
        return (
            f"{response_id}: not found with the current OpenAI key (HTTP 404). "
            "OpenAI deletes background responses about 10 minutes after they "
            "finish and hides them from other projects, so the job may still be "
            "running. It is now an unknown submission: check the key and project, "
            f"then run `reconcile --attempt {attempt['attempt_id']} --response-id "
            f"{response_id}`, or `reconcile --confirm-no-remote-job --reason TEXT` "
            "after verifying that nothing is running."
        )
    journal.append("remote_unavailable", http_status=404, **fields)
    return (
        f"{response_id}: no longer retained by Azure (HTTP 404) on its original "
        "resource; treated as not running"
    )


def _record_observed_response(journal, attempt, response):
    """Record a retrieved response for a journaled attempt; return (status, note)."""
    status = _safe_response_status(response)
    fields = {"attempt_id": attempt["attempt_id"], "turn_id": attempt["turn_id"]}
    if status not in TERMINAL_RESPONSE_STATUSES:
        journal.append("poll_status", status=status, **fields)
        return status, f"still {status}"
    journal.record_terminal(attempt["attempt_id"], response)
    if status == "completed":
        try:
            if _response_text(response).strip():
                journal.save_completion(attempt["attempt_id"], response)
                return status, "completed; cached for `resume`"
        except (DeepThinkError, MalformedResponseError) as error:
            return status, f"completed but not cached ({error})"
    return status, status


def _refresh_active_attempts(journal, factories, clients, actions):
    view = journal.view()
    for attempt in view.attempts.values():
        if view.attempt_state(attempt) != "active":
            continue
        response_id = attempt["response_id"]
        mismatch = _provider_mismatch(attempt, factories)
        if mismatch is not None:
            actions.append(f"{response_id}: not observed; {mismatch}")
            continue
        owner = _attempt_provider(attempt)
        kind, value = _remote_call(
            factories[owner],
            clients.setdefault(owner, {}),
            attempt["resource"],
            "retrieve",
            response_id,
            SERVICE_LABELS.get(owner, "Azure"),
        )
        if kind == "not_found":
            actions.append(_record_not_found(journal, attempt, response_id))
        elif kind == "error":
            actions.append(f"{response_id}: could not observe status ({value})")
        else:
            _status, note = _record_observed_response(journal, attempt, value)
            actions.append(f"{response_id}: {note}")


def reconcile_project(
    client_factory,
    *,
    root,
    project,
    response_id=None,
    endpoint=None,
    attempt_id=None,
    confirm_no_remote_job=False,
    abandon_turn=False,
    reason=None,
    release_lock=False,
    provider="azure",
):
    """Record observed remote state and explicit operator resolutions.

    client_factory is one factory for provider, or a mapping of provider to
    factory; each job is contacted only through the provider that accepted it.
    """
    factories = _provider_factories(client_factory, provider)
    first_provider = next(iter(factories))
    reason = reason.strip() if isinstance(reason, str) else None
    if (confirm_no_remote_job or abandon_turn) and not reason:
        raise DeepThinkError(
            "--confirm-no-remote-job and --abandon-turn require --reason "
            "describing the evidence."
        )
    if attempt_id and not (response_id or confirm_no_remote_job):
        raise DeepThinkError(
            "--attempt requires --response-id or --confirm-no-remote-job."
        )
    project_dir = _existing_project_dir(root, project)
    actions = []
    clients = {}
    with _project_lock(
        root, project, command="reconcile", release_unverifiable=release_lock
    ) as lock:
        journal = _open_project_journal(project_dir, lock)
        if lock.recovered is not None:
            actions.append(f"recovered writer lock ({lock.recovered.get('reason')})")
        if response_id:
            view = journal.view()
            if attempt_id:
                attempt = view.attempts.get(attempt_id)
                if attempt is None or view.attempt_state(attempt) != "unknown":
                    raise DeepThinkError(
                        f"Attempt {attempt_id} is not an unresolved unknown "
                        "submission in the request journal."
                    )
                if (
                    endpoint
                    and attempt["resource"] is not None
                    and endpoint != attempt["resource"]
                ):
                    raise DeepThinkError(
                        f"Attempt {attempt_id} was submitted to its recorded resource "
                        f"{attempt['resource']!r}; --endpoint cannot override it."
                    )
                journal.append(
                    "operator_attached",
                    attempt_id=attempt_id,
                    turn_id=attempt["turn_id"],
                    response_id=response_id,
                    resource=endpoint or attempt["resource"],
                    reason=reason,
                    operator=_operator_name(),
                )
                actions.append(f"attached {response_id} to attempt {attempt_id}")
            elif view.attempt_for_response(response_id) is None:
                if not endpoint:
                    raise DeepThinkError(
                        "--endpoint is required for a response ID that is not in "
                        "the request journal."
                    )
                kind, value = _remote_call(
                    factories[first_provider],
                    clients.setdefault(first_provider, {}),
                    endpoint,
                    "retrieve",
                    response_id,
                    SERVICE_LABELS.get(first_provider, "Azure"),
                )
                status = _safe_response_status(value) if kind == "response" else None
                journal.append(
                    "external_response_observed",
                    response_id=response_id,
                    resource=endpoint,
                    status=status,
                    result=kind,
                )
                legacy = journal.view().unresolved_legacy()
                if status in TERMINAL_RESPONSE_STATUSES and legacy:
                    journal.append(
                        "legacy_resolved",
                        item_ids=[item["item_id"] for item in legacy],
                        response_id=response_id,
                        observed_status=status,
                        reason=reason,
                        operator=_operator_name(),
                    )
                    actions.append(
                        f"{response_id} is {status}; resolved the pre-journal writer"
                    )
                elif status in ACTIVE_RESPONSE_STATUSES:
                    actions.append(
                        f"{response_id} is still {status}; wait, or stop it with "
                        f"`cancel --response-id {response_id} --endpoint URL`"
                    )
                elif kind == "not_found":
                    actions.append(
                        f"{response_id} was not found on that resource; this does "
                        "not prove that no request is running"
                    )
                else:
                    actions.append(f"{response_id}: {status or value}")
        _refresh_active_attempts(journal, factories, clients, actions)
        view = journal.view()
        if confirm_no_remote_job:
            attempts = [
                attempt["attempt_id"]
                for attempt in view.attempts.values()
                if view.attempt_state(attempt) == "unknown"
                and attempt_id in {None, attempt["attempt_id"]}
            ]
            items = [item["item_id"] for item in view.unresolved_legacy()]
            if attempts or items:
                journal.append(
                    "operator_confirmed_no_remote_job",
                    attempt_ids=attempts,
                    item_ids=items,
                    reason=reason,
                    operator=_operator_name(),
                )
                actions.append(
                    f"confirmed no remote job for {len(attempts) + len(items)} "
                    "unknown submission(s)"
                )
            else:
                actions.append("no unresolved unknown submissions to confirm")
            view = journal.view()
        if abandon_turn:
            turn = view.open_turn()
            if turn is None:
                actions.append("no unfinished turn to abandon")
            else:
                running = [
                    attempt["response_id"] or attempt["attempt_id"]
                    for attempt in view.turn_attempts(turn["turn_id"])
                    if view.attempt_state(attempt) in OUTSTANDING_ATTEMPT_STATES
                ]
                if running:
                    raise RemoteStateError(
                        "Cannot abandon the unfinished turn while request(s) "
                        f"{', '.join(running)} may still be running; cancel them "
                        "or resolve unknown submissions first."
                    )
                journal.append(
                    "turn_abandoned",
                    turn_id=turn["turn_id"],
                    reason=reason,
                    operator=_operator_name(),
                )
                journal.release_turn_artifacts(turn["turn_id"])
                actions.append(f"abandoned unfinished turn {turn['turn_id']}")
    return {"actions": actions, "status": project_status(root, project)}


def _add_project_arguments(parser):
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            os.getenv(
                "DEEP_THINK_TRANSCRIPTS_ROOT",
                "deep-think-transcripts",
            )
        ),
    )


def _add_connection_arguments(parser):
    parser.add_argument(
        "--auth",
        help=(
            "Comma-separated sign-in methods in priority order: entra, azure-key, "
            "openai-key. Later methods are backups. Defaults to DEEP_THINK_AUTH, "
            "then one method detected from the environment."
        ),
    )


def _add_routing_arguments(parser):
    _add_connection_arguments(parser)
    parser.add_argument(
        "--endpoint",
        help=(
            "Primary v1 endpoint or preview Responses URL for the first --auth "
            "method. Defaults to AZURE_OPENAI_GPT6_ENDPOINT, then "
            "AZURE_OPENAI_ENDPOINT; for OpenAI (requires --auth openai-key), "
            "OPENAI_BASE_URL or https://api.openai.com/v1/."
        ),
    )
    parser.add_argument(
        "--backup-endpoint",
        help="Backup resource for the primary model; defaults to AZURE_OPENAI_GPT6_BACKUP_ENDPOINT for GPT-6.",
    )
    parser.add_argument(
        "--backup-deployment",
        help="Backup deployment name if different from the primary deployment.",
    )
    parser.add_argument(
        "--fallback-endpoint",
        default=os.getenv("AZURE_OPENAI_FALLBACK_ENDPOINT")
        or os.getenv("AZURE_OPENAI_ENDPOINT"),
        help="Resource hosting the existing GPT-5.6 and GPT-5.4 deployments.",
    )


def _add_retry_arguments(parser):
    parser.add_argument(
        "--max-attempts",
        type=int,
        help=(
            "Submission attempts per request (1-10). Defaults to one per model "
            "target, and at least 5."
        ),
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=DEFAULT_POLL_TIMEOUT,
        help="Polling budget in seconds per accepted job (default: 3600); never resubmit on expiry.",
    )
    parser.add_argument(
        "--retry-base-delay",
        type=float,
        default=DEFAULT_RETRY_BASE_DELAY,
    )
    parser.add_argument(
        "--retry-max-delay",
        type=float,
        default=DEFAULT_RETRY_MAX_DELAY,
    )


def _build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run a persistent GPT-6 Astra deep-mathematics conversation "
            "through Azure OpenAI."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    ask = subparsers.add_parser(
        "ask",
        help="Add one turn to a locally persisted research project.",
    )
    _add_project_arguments(ask)
    ask.add_argument("--title")
    prompt_group = ask.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file", type=Path)
    _add_routing_arguments(ask)
    ask.add_argument(
        "--deployment",
        default=os.getenv("AZURE_OPENAI_DEPLOYMENT", DEFAULT_DEPLOYMENT),
    )
    ask.add_argument(
        "--rollover-tokens",
        type=int,
        default=DEFAULT_ROLLOVER_TOKENS,
    )
    _add_retry_arguments(ask)
    ask.add_argument(
        "--recover-service-errors",
        action="store_true",
        help="Allow one visible-transcript rollover after terminal server_error retries exhaust.",
    )

    status = subparsers.add_parser(
        "status",
        help="Show the writer lock and journaled request state (read-only).",
    )
    _add_project_arguments(status)

    resume = subparsers.add_parser(
        "resume",
        help="Finish the unfinished turn, polling known jobs on their original resources.",
    )
    _add_project_arguments(resume)
    _add_routing_arguments(resume)
    _add_retry_arguments(resume)

    cancel = subparsers.add_parser(
        "cancel",
        help="Cancel active background responses on their original resources.",
    )
    _add_project_arguments(cancel)
    _add_connection_arguments(cancel)
    cancel.add_argument("--response-id", action="append", dest="response_ids")
    cancel.add_argument(
        "--endpoint",
        help="Original resource for a response ID that is not in the journal.",
    )

    reconcile = subparsers.add_parser(
        "reconcile",
        help="Record observed remote state and explicit recovery decisions.",
    )
    _add_project_arguments(reconcile)
    _add_connection_arguments(reconcile)
    reconcile.add_argument("--response-id")
    reconcile.add_argument(
        "--endpoint",
        help="Original resource for a response ID found outside the journal.",
    )
    reconcile.add_argument(
        "--attempt",
        help="Unknown submission attempt that --response-id belongs to.",
    )
    reconcile.add_argument(
        "--confirm-no-remote-job",
        action="store_true",
        help="Record that unknown submissions created no running job (needs --reason).",
    )
    reconcile.add_argument(
        "--abandon-turn",
        action="store_true",
        help="Discard the unfinished turn once no remote work is outstanding.",
    )
    reconcile.add_argument("--reason")
    reconcile.add_argument(
        "--release-lock",
        action="store_true",
        help="Break a writer lock whose owner cannot be verified.",
    )
    return parser


def _prompt_from_args(args, stdin):
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file is not None:
        try:
            return args.prompt_file.read_text(encoding="utf-8")
        except OSError as error:
            raise DeepThinkError(
                f"Could not read prompt file {args.prompt_file}: {error}"
            ) from error
    return stdin.read()


def _resolve_methods(args):
    """Return sign-in methods in priority order; later ones are backups."""
    listed = getattr(args, "auth", None) or os.getenv("DEEP_THINK_AUTH")
    if listed and listed.strip():
        methods = tuple(name.strip().lower() for name in listed.split(","))
        if len(set(methods)) != len(methods) or not all(
            name in METHOD_PROVIDERS for name in methods
        ):
            raise DeepThinkError(
                "--auth and DEEP_THINK_AUTH take distinct methods from entra, "
                "azure-key, and openai-key, in priority order."
            )
        return methods
    if any(getattr(args, option, None) for option in ENDPOINT_OPTIONS) or any(
        os.getenv(name) for name in CONFIGURED_ENDPOINT_VARIABLES
    ):
        # Endpoint options have always meant Azure; OpenAI must be requested.
        # A key in the environment comes first, with Entra ID as its backup.
        if any(os.getenv(name) for name in AZURE_KEY_NAMES):
            return ("azure-key", "entra")
        return ("entra",)
    return ("openai-key",) if os.getenv("OPENAI_API_KEY") else ("entra",)


def _method_providers(methods):
    return tuple(dict.fromkeys(METHOD_PROVIDERS[method] for method in methods))


def _method_client_factory(method):
    if method == "entra":
        return lambda resource: create_client(resource)
    if method == "openai-key":
        key = os.getenv("OPENAI_API_KEY")
        return lambda resource: create_client(resource, auth="openai-key", api_key=key)
    keys = {}
    for endpoint_variable, key_variable in AZURE_KEY_VARIABLES.items():
        endpoint, key = os.getenv(endpoint_variable), os.getenv(key_variable)
        if endpoint and key:
            keys[_endpoint_key(endpoint)] = key
    default_key = os.getenv("AZURE_OPENAI_API_KEY")

    def factory(resource):
        key = keys.get(_endpoint_key(resource)) if isinstance(resource, str) else None
        return create_client(resource, auth="azure-key", api_key=key or default_key)

    return factory


def _configured_client_factories(methods, *, on_switch=None, on_unavailable=None):
    """Return one client factory per provider, ordered by first use in methods.

    Several methods for one provider become an AuthFallbackClient that tries
    them in order on the same resource. A method that cannot build a client
    for a resource (such as a missing per-resource key, or azure-identity not
    installed) is skipped there; the request fails only if no method remains.
    """
    factories = {}
    reported = set()
    for provider in _method_providers(methods):
        builders = [
            (method, _method_client_factory(method))
            for method in methods
            if METHOD_PROVIDERS[method] == provider
        ]
        if len(builders) == 1:
            factories[provider] = builders[0][1]
            continue

        def factory(resource, builders=builders):
            clients, failures = [], []
            for method, build in builders:
                try:
                    clients.append((method, build(resource)))
                except DeepThinkError as error:
                    failures.append((method, error))
            if not clients:
                raise failures[0][1]
            for method, error in failures:
                expected = isinstance(error, MissingCredentialError)
                if not expected and method not in reported:
                    reported.add(method)
                    if on_unavailable is not None:
                        on_unavailable(method, error)
            if len(clients) == 1:
                return clients[0][1]
            return AuthFallbackClient(clients, on_switch=on_switch)

        factories[provider] = factory
    return factories


def _upgradable_deployments():
    """Older primaries that a default GPT-6 turn may upgrade, including local ones."""
    return tuple(
        dict.fromkeys(
            [
                *DEFAULT_FALLBACK_DEPLOYMENTS,
                *_fallback_names(
                    "AZURE_OPENAI_FALLBACK_DEPLOYMENTS", DEFAULT_FALLBACK_DEPLOYMENTS
                ),
                *_fallback_names(
                    "OPENAI_FALLBACK_MODELS", DEFAULT_OPENAI_FALLBACK_MODELS
                ),
            ]
        )
    )


def _fallback_names(variable, default):
    """Read an ordered fallback list from the environment; 'none' disables it."""
    raw = (os.getenv(variable) or "").strip()
    if not raw:
        return default
    if raw.lower() == "none":
        return ()
    names = tuple(name.strip() for name in raw.split(","))
    if len(set(names)) != len(names) or not all(
        FALLBACK_NAME_PATTERN.fullmatch(name) for name in names
    ):
        raise DeepThinkError(
            f"{variable} must be 'none' or a comma-separated list of distinct "
            "deployment or model names."
        )
    return names


def _provider_router(args, deployment, provider, client_factory, endpoint):
    """Build one provider's model chain; endpoint is an explicit --endpoint."""
    if provider == "openai":
        return create_routed_client(
            endpoint or os.getenv("OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL,
            deployment,
            client_factory=client_factory,
            fallback_deployments=_fallback_names(
                "OPENAI_FALLBACK_MODELS", DEFAULT_OPENAI_FALLBACK_MODELS
            ),
            provider=provider,
        )
    fallbacks = _fallback_names(
        "AZURE_OPENAI_FALLBACK_DEPLOYMENTS", DEFAULT_FALLBACK_DEPLOYMENTS
    )
    if deployment == DEFAULT_DEPLOYMENT and DEFAULT_DEPLOYMENT in fallbacks:
        # Listing the primary would route GPT-6 away from its own resources.
        raise DeepThinkError(
            "AZURE_OPENAI_FALLBACK_DEPLOYMENTS must not include the primary "
            f"model {DEFAULT_DEPLOYMENT}."
        )
    legacy_deployment = deployment in fallbacks
    primary = (
        endpoint
        or (
            args.fallback_endpoint
            if legacy_deployment
            else os.getenv("AZURE_OPENAI_GPT6_ENDPOINT")
        )
        or os.getenv("AZURE_OPENAI_ENDPOINT")
    )
    backup_endpoint = args.backup_endpoint or (
        None if legacy_deployment else os.getenv("AZURE_OPENAI_GPT6_BACKUP_ENDPOINT")
    )
    # The GPT-6 backup deployment name applies only to the configured GPT-6
    # primary, so an explicit other model is never answered by GPT-6.
    configured_primary = os.getenv("AZURE_OPENAI_DEPLOYMENT") or DEFAULT_DEPLOYMENT
    return create_routed_client(
        primary,
        deployment,
        backup_endpoint=backup_endpoint,
        backup_deployment=args.backup_deployment
        or (
            os.getenv("AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT")
            if deployment == configured_primary and not legacy_deployment
            else None
        ),
        fallback_endpoint=args.fallback_endpoint,
        client_factory=client_factory,
        fallback_deployments=fallbacks,
        provider=provider,
    )


def _router_from_args(args, deployment, client_factories):
    """Chain each provider's models in --auth order; later providers are backups."""
    providers = _method_providers(_resolve_methods(args))
    if not isinstance(client_factories, Mapping):
        client_factories = dict.fromkeys(providers, client_factories)
    targets, clients = [], {}
    for provider in providers:
        router = _provider_router(
            args,
            deployment,
            provider,
            client_factories[provider],
            args.endpoint if provider == providers[0] else None,
        )
        targets.extend(router.targets)
        clients.update(router._clients)
    return DeploymentRouter(
        targets,
        clients=clients,
        provider=providers[0],
        client_factories={
            provider: client_factories[provider] for provider in providers
        },
    )


def _endpoint_key(endpoint):
    return endpoint.strip().rstrip("/")


def _owner_or_none(endpoint):
    try:
        return _host_provider(endpoint)
    except DeepThinkError:
        return None


def _trusted_client_factory(client_factory, args, provider):
    """Send credentials only to endpoints configured for this provider."""
    first = _method_providers(_resolve_methods(args))[0]
    explicit = getattr(args, "endpoint", None) if provider == first else None
    if provider == "openai":
        configured = [explicit, os.getenv("OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL]
        options = "--endpoint"
    else:
        configured = [os.getenv(name) for name in CONFIGURED_ENDPOINT_VARIABLES]
        configured += [
            explicit,
            getattr(args, "backup_endpoint", None),
            getattr(args, "fallback_endpoint", None),
        ]
        options = (
            "--endpoint (for resume: --endpoint, --backup-endpoint, or "
            "--fallback-endpoint)"
        )
    other = "azure" if provider == "openai" else "openai"
    trusted = {_endpoint_key(value) for value in configured if value}
    # Endpoints configured for the other provider are never trusted here, even
    # when passed explicitly, unless their host plainly belongs to this one or
    # this provider's own settings configure them too.
    if other == "openai":
        foreign = [os.getenv("OPENAI_BASE_URL")]
        own = [os.getenv(name) for name in CONFIGURED_ENDPOINT_VARIABLES]
    else:
        foreign = [os.getenv(name) for name in CONFIGURED_ENDPOINT_VARIABLES]
        own = [os.getenv("OPENAI_BASE_URL")]
    own = {_endpoint_key(value) for value in own if value}
    foreign = {
        _endpoint_key(value)
        for value in foreign
        if value and _owner_or_none(value) != provider
    } - own
    single = f"--auth {PROVIDER_AUTH_HINTS[provider]}"

    def factory(resource):
        # Journal resources may be tampered with. For Azure, None carries no
        # destination; an OpenAI client would default to api.openai.com.
        if resource is None and provider == "openai":
            raise DeepThinkError(
                "The recorded OpenAI response has no resource, so credentials "
                "will not be sent; the journal may have been tampered with."
            )
        if resource is not None:
            key = _endpoint_key(resource) if isinstance(resource, str) else None
            if key is not None and key in foreign:
                raise DeepThinkError(
                    f"Resource {resource!r} is configured for "
                    f"{PROVIDER_LABELS[other]}, so {PROVIDER_LABELS[provider]} "
                    "credentials will not be sent to it. If the job used "
                    f"{PROVIDER_LABELS[other]}, rerun with --auth "
                    f"{PROVIDER_AUTH_HINTS[other]}."
                )
            if key is None or key not in trusted:
                raise DeepThinkError(
                    f"Recorded resource {resource!r} is not a configured endpoint "
                    f"for {PROVIDER_LABELS[provider]}, so credentials will not be "
                    f"sent to it. If it belongs to {PROVIDER_LABELS[other]}, rerun "
                    f"with --auth {PROVIDER_AUTH_HINTS[other]}; if it is a "
                    f"legitimate {PROVIDER_LABELS[provider]} endpoint, pass it "
                    f"explicitly with {options}, using {single} so that "
                    "--endpoint refers to that provider."
                )
            _refuse_cross_provider_host(provider, resource)
        return client_factory(resource)

    return factory


def default_env_file(environ=None):
    """Return the per-user env file path, or None when no home is known."""
    environ = os.environ if environ is None else environ
    config_home = environ.get("XDG_CONFIG_HOME")
    # The XDG spec says to ignore relative values.
    if config_home and Path(config_home).is_absolute():
        return Path(config_home) / "deep-think" / ".env"
    try:
        return Path.home() / ".config" / "deep-think" / ".env"
    except RuntimeError:
        return None


def _parse_env_file(text, label):
    settings = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{label}:{number}"
        match = ENV_FILE_LINE.fullmatch(line)
        if match is None:
            raise DeepThinkError(f"{where}: expected NAME=value.")
        name, value = match.groups()
        if not name.startswith(ENV_FILE_PREFIXES):
            shown = name if ENV_NAME_PATTERN.fullmatch(name) else "this line"
            raise DeepThinkError(
                f"{where}: {shown} is not a deep-think setting; the env file may "
                "set only AZURE_*, OPENAI_*, and DEEP_THINK_* variables."
            )
        if value[:1] in {'"', "'"}:
            end = value.find(value[0], 1)
            rest = value[end + 1 :].strip() if end > 0 else ""
            if end < 0 or (rest and not rest.startswith("#")):
                raise DeepThinkError(f"{where}: {name} has a malformed quoted value.")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if "\x00" in value:
            raise DeepThinkError(f"{where}: {name} contains a NUL character.")
        settings[name] = value
    return settings


def load_env_file(path=None, *, environ=None):
    """Load local settings into the environment without overriding it.

    The file is ``path``, DEEP_THINK_ENV_FILE, or the per-user default. Values
    are never echoed, because the file may hold API keys.
    """
    environ = os.environ if environ is None else environ
    if path is None and environ.get(ENV_FILE_VARIABLE):
        path = Path(environ[ENV_FILE_VARIABLE]).expanduser()
    required = path is not None
    path = Path(path) if required else default_env_file(environ)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if required:
            raise DeepThinkError(
                f"Env file {path} does not exist; fix or unset {ENV_FILE_VARIABLE}."
            ) from None
        return None
    except UnicodeError:
        raise DeepThinkError(f"Env file {path} is not valid UTF-8.") from None
    except OSError as error:
        raise DeepThinkError(
            f"Could not read env file {path}: {error.strerror or type(error).__name__}."
        ) from None
    for name, value in _parse_env_file(text, str(path)).items():
        if value and not environ.get(name):
            environ[name] = value
    return path


def cli(argv=None):
    """Script entry point: load the local env file, then run the command."""
    try:
        load_env_file()
    except DeepThinkError as error:
        sys.stderr.write(f"deep-think: {_redact_secrets(error)}\n")
        return 2
    return main(argv)


def main(
    argv=None,
    *,
    client_factory=create_client,
    stdin=None,
    stdout=None,
    stderr=None,
):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if hasattr(stdout, "reconfigure"):
        stdout.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)

    def report_retry(event):
        stderr.write(
            f"Retrying {event.purpose} after {event.reason} "
            f"(attempt {event.attempt}/{event.max_attempts}, "
            f"delay {event.delay:.2f}s).\n"
        )

    def report_switch(previous, current, error):
        stderr.write(
            f"deep-think: {METHOD_LABELS[previous]} sign-in failed "
            f"({type(error).__name__}); using {METHOD_LABELS[current]}.\n"
        )

    def report_unavailable(method, error):
        stderr.write(
            f"deep-think: {METHOD_LABELS[method]} is unavailable "
            f"({_redact_secrets(error)}); using the other listed sign-in methods.\n"
        )

    try:
        if args.command != "status":
            methods = _resolve_methods(args)
            if getattr(args, "endpoint", None) and len(_method_providers(methods)) > 1:
                raise DeepThinkError(
                    "--endpoint names one provider's endpoint, so it needs an "
                    "--auth list for a single provider (for example --auth "
                    "openai-key, or --auth entra,azure-key)."
                )
            if client_factory is create_client:
                factories = _configured_client_factories(
                    methods,
                    on_switch=report_switch,
                    on_unavailable=report_unavailable,
                )
            else:
                factories = dict.fromkeys(_method_providers(methods), client_factory)
            factories = {
                provider: _trusted_client_factory(factory, args, provider)
                for provider, factory in factories.items()
            }
        if args.command == "status":
            report = project_status(args.root, args.project)
        elif args.command == "cancel":
            report = cancel_requests(
                factories,
                root=args.root,
                project=args.project,
                response_ids=args.response_ids or (),
                endpoint=args.endpoint,
            )
        elif args.command == "reconcile":
            report = reconcile_project(
                factories,
                root=args.root,
                project=args.project,
                response_id=args.response_id,
                endpoint=args.endpoint,
                attempt_id=args.attempt,
                confirm_no_remote_job=args.confirm_no_remote_job,
                abandon_turn=args.abandon_turn,
                reason=args.reason,
                release_lock=args.release_lock,
            )
        elif args.command == "resume":
            report = None
            result = resume_turn(
                lambda deployment: _router_from_args(args, deployment, factories),
                root=args.root,
                project=args.project,
                max_attempts=args.max_attempts,
                retry_base_delay=args.retry_base_delay,
                retry_max_delay=args.retry_max_delay,
                on_retry=report_retry,
                poll_timeout=args.poll_timeout,
                upgradable_deployments=_upgradable_deployments(),
            )
        else:
            report = None
            prompt = _prompt_from_args(args, stdin)
            result = run_turn(
                _router_from_args(args, args.deployment, factories),
                root=args.root,
                project=args.project,
                title=args.title,
                prompt=prompt,
                deployment=args.deployment,
                rollover_tokens=args.rollover_tokens,
                max_attempts=args.max_attempts,
                retry_base_delay=args.retry_base_delay,
                retry_max_delay=args.retry_max_delay,
                on_retry=report_retry,
                poll_timeout=args.poll_timeout,
                recover_service_errors=args.recover_service_errors,
                upgradable_deployments=_upgradable_deployments(),
            )
    except DeepThinkError as error:
        stderr.write(f"deep-think: {_redact_secrets(error)}\n")
        if args.command in {"ask", "resume"}:
            stderr.write(
                "deep-think: inspect recovery state with "
                f"`deep_think.py status --project {args.project}`.\n"
            )
        return 2

    if report is not None:
        stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return 0
    stdout.write(result.text.rstrip() + "\n")
    stderr.write(f"Transcript: {result.transcript_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
