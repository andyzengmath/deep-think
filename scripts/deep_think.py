#!/usr/bin/env python3

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

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

VISIBLE_TRANSCRIPT_SUMMARY_PROMPT = """The opaque Responses API context was
rejected for length. Create the continuation summary from the complete
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
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_MAX_DELAY = 30.0
DEFAULT_REQUEST_TIMEOUT = 3600.0
DEFAULT_POLL_INTERVAL = 2.0
VISIBLE_SUMMARY_CHUNK_BYTES = 400_000
VISIBLE_CHUNK_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS
VISIBLE_SUMMARY_MAX_REDUCTION_ROUNDS = 4
DEFAULT_DEPLOYMENT = "gpt-5.6-sol"
ENTRA_SCOPE = "https://ai.azure.com/.default"
STATE_SCHEMA = "deep-think-state"
LEGACY_STATE_VERSION = 1
STATE_VERSION = 2
PROJECT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class DeepThinkError(RuntimeError):
    pass


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
    def __init__(self, response, retry_count):
        self.response = response
        self.retry_count = retry_count


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
    message = details.get("message") or str(error)
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
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {})
    directive = headers.get("x-should-retry")
    if directive == "true":
        return True
    if directive == "false":
        return False
    status_code, _, _ = _status_error_details(error)
    return status_code in {408, 409, 429} or (
        status_code is not None and status_code >= 500
    )


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


def create_client(
    endpoint,
    *,
    credential_factory=None,
    token_provider_factory=None,
    openai_factory=None,
):
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise DeepThinkError(
            "Azure OpenAI endpoint is required. Set AZURE_OPENAI_ENDPOINT "
            "or pass --endpoint."
        )
    endpoint = endpoint.strip()
    parsed_endpoint = urlsplit(endpoint)
    if parsed_endpoint.scheme != "https" or not parsed_endpoint.netloc:
        raise DeepThinkError("Azure OpenAI endpoint must use HTTPS and include a host.")
    if (
        parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise DeepThinkError(
            "Azure OpenAI endpoint must not contain credentials, query parameters, "
            "or fragments."
        )

    if (
        credential_factory is None
        or token_provider_factory is None
        or openai_factory is None
    ):
        try:
            from azure.identity import (
                DefaultAzureCredential,
                get_bearer_token_provider,
            )
            from openai import OpenAI
        except ImportError as error:
            raise DeepThinkError(
                "Install dependencies with: "
                "python -m pip install --upgrade openai azure-identity"
            ) from error
        credential_factory = credential_factory or DefaultAzureCredential
        token_provider_factory = token_provider_factory or get_bearer_token_provider
        openai_factory = openai_factory or OpenAI

    normalized_endpoint = endpoint.rstrip("/") + "/"
    token_provider = token_provider_factory(
        credential_factory(),
        ENTRA_SCOPE,
    )
    return openai_factory(
        base_url=normalized_endpoint,
        api_key=token_provider,
        max_retries=0,
        timeout=DEFAULT_REQUEST_TIMEOUT,
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
    sleep,
    random_value,
    on_retry,
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

    retry_count = 0
    while _response_status(response) in {"queued", "in_progress"}:
        response_id = _response_id(response)
        sleep(poll_interval)
        for attempt in range(1, max_attempts + 1):
            try:
                retrieved = client.responses.retrieve(response_id)
            except APIResponseValidationError as error:
                reason = "malformed Azure response"
                retry_error = error
            except APIConnectionError as error:
                reason = type(error).__name__
                retry_error = error
            except APIStatusError as error:
                status_code, code, message = _status_error_details(error)
                reason = f"Azure HTTP {status_code}"
                if code:
                    reason += f" ({code})"
                if not _status_error_is_retryable(error):
                    raise DeepThinkError(
                        f"{purpose.capitalize()} poll failed without retry: "
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
                    reason = "malformed Azure response"
                    retry_error = error
                else:
                    response = retrieved
                    retry_count += attempt - 1
                    break

            if attempt >= max_attempts:
                raise DeepThinkError(
                    f"{purpose.capitalize()} poll failed after "
                    f"{attempt} attempts: {reason}"
                ) from retry_error
            delay = _retry_delay(
                retry_error,
                attempt,
                base_delay,
                max_delay,
                random_value,
            )
            _retry_event(
                f"{purpose} poll",
                attempt,
                max_attempts,
                reason,
                delay,
                on_retry,
            )
            sleep(delay)

    return response, retry_count


def request_response(
    client,
    request,
    *,
    purpose,
    max_attempts=3,
    base_delay=1.0,
    max_delay=30.0,
    sleep=time.sleep,
    random_value=random.random,
    on_retry=None,
    poll_interval=DEFAULT_POLL_INTERVAL,
):
    _validate_retry_settings(max_attempts, base_delay, max_delay)
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

    request_has_encrypted_content = _request_contains_encrypted_content(request)
    poll_retry_count = 0
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.responses.create(**request)
        except APIResponseValidationError as error:
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
            _retry_event(
                purpose,
                attempt,
                max_attempts,
                "malformed Azure response",
                delay,
                on_retry,
            )
            sleep(delay)
            continue
        except APIConnectionError as error:
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
            _retry_event(
                purpose,
                attempt,
                max_attempts,
                type(error).__name__,
                delay,
                on_retry,
            )
            sleep(delay)
            continue
        except APIStatusError as error:
            status_code, code, message = _status_error_details(error)
            reason = f"Azure HTTP {status_code}"
            if code:
                reason += f" ({code})"
            if _status_error_is_opaque_replay(error, request_has_encrypted_content):
                raise OpaqueReplayError(
                    f"{purpose.capitalize()} request could not replay encrypted "
                    "reasoning context."
                ) from error
            if _status_error_is_context_limit(error):
                raise ContextLimitError(
                    f"{purpose.capitalize()} request exceeded the context "
                    f"limit: {reason}: {message}"
                ) from error
            if not _status_error_is_retryable(error):
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
            _retry_event(
                purpose,
                attempt,
                max_attempts,
                reason,
                delay,
                on_retry,
            )
            sleep(delay)
            continue
        try:
            response, retries = _poll_background_response(
                client,
                response,
                purpose=purpose,
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                poll_interval=poll_interval,
                sleep=sleep,
                random_value=random_value,
                on_retry=on_retry,
            )
            poll_retry_count += retries
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
                return RequestOutcome(
                    response,
                    attempt - 1 + poll_retry_count,
                )
            if status == "completed" and attempt < max_attempts:
                delay = _retry_delay(
                    None,
                    attempt,
                    base_delay,
                    max_delay,
                    random_value,
                )
                _retry_event(
                    purpose,
                    attempt,
                    max_attempts,
                    "empty response",
                    delay,
                    on_retry,
                )
                sleep(delay)
                continue
            if status == "completed":
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
            if retryable_codes and attempt < max_attempts:
                reason = (
                    "Azure response error (" + ", ".join(sorted(retryable_codes)) + ")"
                )
                delay = _retry_delay(
                    None,
                    attempt,
                    base_delay,
                    max_delay,
                    random_value,
                )
                _retry_event(
                    purpose,
                    attempt,
                    max_attempts,
                    reason,
                    delay,
                    on_retry,
                )
                sleep(delay)
                continue
            if retryable_codes:
                raise DeepThinkError(
                    f"{purpose.capitalize()} request failed after "
                    f"{attempt} attempts: {response_message}"
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
            _retry_event(
                purpose,
                attempt,
                max_attempts,
                "malformed Azure response",
                delay,
                on_retry,
            )
            sleep(delay)
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
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
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
    return (
        "---\n"
        f"schema: deep-think-transcript/v{STATE_VERSION}\n"
        f"project: {json.dumps(state['project'])}\n"
        f"volume: {state['volume']}\n"
        f"model: {json.dumps(state['deployment'])}\n"
        "reasoning-mode: pro\n"
        "reasoning-effort: max\n"
        f"context-window-tokens: {CONTEXT_WINDOW_TOKENS}\n"
        f"rollover-tokens: {state['rollover_tokens']}\n"
        f"created-at: {json.dumps(state['created_at'])}\n"
        "---\n\n"
        f"# {state['title']} - Volume {state['volume']:04d}\n"
    )


def _turn_markdown(turn, prompt, response, usage, retry_count=0):
    return (
        f"\n## Conversation {turn}\n\n"
        "### User\n\n"
        f"{prompt.rstrip()}\n\n"
        "### Assistant\n\n"
        f"{response.output_text.rstrip()}\n\n"
        "### Usage\n\n"
        f"- Response ID: `{response.id}`\n"
        "- Reasoning mode: `pro`\n"
        "- Reasoning effort: `max`\n"
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
):
    recovery_markdown = (
        f"\n> Recovery mode: {recovery_note}.\n" if recovery_note is not None else ""
    )
    return (
        "\n## Volume rollover summary\n\n"
        f"{recovery_markdown}"
        f"{response.output_text.rstrip()}\n\n"
        "### Summary usage\n\n"
        f"- Response ID: `{response.id}`\n"
        f"- Input tokens: {usage['input_tokens']:,}\n"
        f"- Output tokens: {usage['output_tokens']:,}\n"
        f"- Reasoning tokens: {usage['reasoning_tokens']:,}\n"
        f"- Application retries: {retry_count}\n"
        f"- Final context tokens: {usage['total_tokens']:,} / "
        f"{CONTEXT_WINDOW_TOKENS:,}\n"
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


@contextmanager
def _project_lock(root, project):
    project_dir = Path(root) / project
    project_dir.mkdir(parents=True, exist_ok=True)
    lock_path = project_dir / ".deep-think.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
    except FileExistsError as error:
        try:
            owner = lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            owner = "owner unavailable"
        raise DeepThinkError(
            f"Project is already locked by another process: {owner}. "
            "If that process crashed, remove the lock file after confirming "
            "no deep-think request is still running."
        ) from error

    try:
        os.write(
            descriptor,
            json.dumps(
                {
                    "pid": os.getpid(),
                    "created_at": _utc_now(),
                }
            ).encode("utf-8"),
        )
    finally:
        os.close(descriptor)

    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


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
):
    total_retry_count = 0
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
        if visible_input_upper_bound <= MAX_INPUT_TOKENS:
            break
        if reduction_round >= VISIBLE_SUMMARY_MAX_REDUCTION_ROUNDS:
            raise DeepThinkError(
                "Visible transcript summary reduction exceeded its bounded rounds."
            )

        chunks = _split_text_by_utf8_bytes(
            source_text,
            VISIBLE_SUMMARY_CHUNK_BYTES,
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
            )
            total_retry_count += chunk_outcome.retry_count
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
    )
    return RequestOutcome(
        final_outcome.response,
        total_retry_count + final_outcome.retry_count,
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
            )
        except (ContextLimitError, OpaqueReplayError):
            _retry_event(
                "rollover summary",
                1,
                1,
                "retrying from visible transcript",
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
                )
            except ContextLimitError as recovery_error:
                raise DeepThinkError(
                    "Azure rejected both normal and visible-transcript "
                    "rollover summaries for context length."
                ) from recovery_error
            recovery_note = "visible transcript recovery"
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
    )
    state["cumulative_tokens"] += summary_usage["total_tokens"]
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


def _run_turn_locked(
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

    project_dir = Path(root) / project
    state_path = project_dir / "state.json"
    existing_project = state_path.exists()
    if existing_project:
        state = _read_json(state_path)
        _validate_state(state, project)
        if title and title != state["title"]:
            raise DeepThinkError(
                f"Project title is already {state['title']!r}; omit --title."
            )
        if deployment != state["deployment"]:
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
    pending_json = {}
    pending_text = {}
    rolled_over = False
    reactive_recovery_used = False

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
        )
        rolled_over = True
        output_budget = _normal_output_budget(
            state["context_tokens"],
            prompt,
            state["rollover_tokens"],
        )

    if output_budget < minimum_response_tokens:
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
        )
    except (ContextLimitError, OutputLimitError, OpaqueReplayError) as error:
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
            "opaque replay failure; retrying from visible transcript"
            if isinstance(error, OpaqueReplayError)
            else (
                "output limit; forcing rollover"
                if isinstance(error, OutputLimitError)
                else "context limit; forcing rollover"
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
            force_visible_transcript=isinstance(error, OpaqueReplayError),
            force_visible_reason=(
                "invalid_encrypted_content; retrying from visible transcript"
                if isinstance(error, OpaqueReplayError)
                else None
            ),
        )
        rolled_over = True
        reactive_recovery_used = True
        recovery_count = 1
        output_budget = _normal_output_budget(
            state["context_tokens"],
            prompt,
            state["rollover_tokens"],
        )
        if (
            isinstance(error, OutputLimitError)
            and output_budget <= exhausted_output_budget
        ):
            _write_pending_state(
                state_path,
                state,
                context_path=context_path,
                transcript_path=transcript_path,
                pending_json=pending_json,
                pending_text=pending_text,
            )
            raise DeepThinkError(
                "Fresh rollover output budget is not larger than the exhausted "
                "answer budget; split or narrow the request and continue from "
                "the new volume."
            ) from error
        if output_budget < minimum_response_tokens:
            _write_pending_state(
                state_path,
                state,
                context_path=context_path,
                transcript_path=transcript_path,
                pending_json=pending_json,
                pending_text=pending_text,
            )
            raise DeepThinkError(
                "Prompt remains too large after context rollover."
            ) from error
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
        )
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

    return TurnResult(
        response.output_text,
        state["volume"],
        rolled_over,
        transcript_path,
    )


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
):
    _validate_project(project)
    with _project_lock(root, project):
        return _run_turn_locked(
            client,
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
        )


def _build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run a persistent GPT-5.6 Sol deep-mathematics conversation "
            "through Azure OpenAI."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    ask = subparsers.add_parser(
        "ask",
        help="Add one turn to a locally persisted research project.",
    )
    ask.add_argument("--project", required=True)
    ask.add_argument("--title")
    prompt_group = ask.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file", type=Path)
    ask.add_argument(
        "--root",
        type=Path,
        default=Path(
            os.getenv(
                "DEEP_THINK_TRANSCRIPTS_ROOT",
                "deep-think-transcripts",
            )
        ),
    )
    ask.add_argument(
        "--endpoint",
        default=os.getenv("AZURE_OPENAI_ENDPOINT"),
        help=(
            "Azure OpenAI v1 endpoint. Prefer the AZURE_OPENAI_ENDPOINT "
            "environment variable."
        ),
    )
    ask.add_argument(
        "--deployment",
        default=os.getenv("AZURE_OPENAI_DEPLOYMENT", DEFAULT_DEPLOYMENT),
    )
    ask.add_argument(
        "--rollover-tokens",
        type=int,
        default=DEFAULT_ROLLOVER_TOKENS,
    )
    ask.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
    )
    ask.add_argument(
        "--retry-base-delay",
        type=float,
        default=DEFAULT_RETRY_BASE_DELAY,
    )
    ask.add_argument(
        "--retry-max-delay",
        type=float,
        default=DEFAULT_RETRY_MAX_DELAY,
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
    args = _build_parser().parse_args(argv)

    try:
        prompt = _prompt_from_args(args, stdin)
        client = client_factory(args.endpoint)

        def report_retry(event):
            stderr.write(
                f"Retrying {event.purpose} after {event.reason} "
                f"(attempt {event.attempt}/{event.max_attempts}, "
                f"delay {event.delay:.2f}s).\n"
            )

        result = run_turn(
            client,
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
        )
    except DeepThinkError as error:
        stderr.write(f"deep-think: {error}\n")
        return 2

    stdout.write(result.text.rstrip() + "\n")
    stderr.write(f"Transcript: {result.transcript_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
