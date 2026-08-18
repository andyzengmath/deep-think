import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "deep_think.py"


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError(f"Runner does not exist: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("deep_think", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeOutput:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, **_kwargs):
        return self.payload


class FakeResponse:
    def __init__(
        self,
        response_id,
        text,
        *,
        input_tokens=100,
        output_tokens=50,
        reasoning_tokens=25,
        status="completed",
        incomplete_reason="max_output_tokens",
        error=None,
    ):
        self.id = response_id
        self.output_text = text
        self.status = status
        self.incomplete_details = (
            None if status == "completed" else SimpleNamespace(reason=incomplete_reason)
        )
        self.error = error
        self.output = [
            FakeOutput(
                {
                    "id": f"reasoning-{response_id}",
                    "type": "reasoning",
                    "encrypted_content": "opaque",
                }
            ),
            FakeOutput(
                {
                    "id": f"message-{response_id}",
                    "type": "message",
                    "role": "assistant",
                    "status": status,
                    "content": [{"type": "output_text", "text": text}],
                }
            ),
        ]
        self.usage = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        )


class FakeResponses:
    def __init__(self, responses):
        self.queued = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.queued.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeClient:
    def __init__(self, responses):
        self.responses = FakeResponses(responses)


def make_status_error(status_code, code, *, headers=None, message="Azure error"):
    import httpx
    import openai

    request = httpx.Request("POST", "https://example.test/openai/v1/responses")
    response = httpx.Response(
        status_code,
        request=request,
        headers=headers or {},
    )
    error_type = (
        openai.RateLimitError
        if status_code == 429
        else openai.BadRequestError
        if status_code == 400
        else openai.InternalServerError
    )
    return error_type(
        message,
        response=response,
        body={"error": {"code": code, "message": message}},
    )


def make_validation_error():
    import httpx
    import openai

    request = httpx.Request("POST", "https://example.test/openai/v1/responses")
    response = httpx.Response(200, request=request)
    return openai.APIResponseValidationError(
        response=response,
        body={"unexpected": "shape"},
        message="Malformed Azure response.",
    )


class RequestConfigurationTests(unittest.TestCase):
    def test_parser_has_no_endpoint_when_environment_is_unconfigured(self):
        deep_think = load_module()

        with mock.patch.dict("os.environ", {}, clear=True):
            args = deep_think._build_parser().parse_args(
                [
                    "ask",
                    "--project",
                    "configuration-test",
                    "--prompt",
                    "Test the configuration contract.",
                ]
            )

        self.assertIsNone(args.endpoint)

    def test_request_uses_pro_mode_and_max_reasoning(self):
        deep_think = load_module()
        history = [{"role": "user", "content": "Solve the construction."}]

        request = deep_think.build_response_request(history, "gpt-5.6-sol")

        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertEqual(request["input"], history)
        self.assertFalse(request["store"])
        self.assertEqual(
            request["reasoning"],
            {
                "mode": "pro",
                "effort": "max",
                "context": "all_turns",
                "summary": "auto",
            },
        )
        self.assertEqual(request["text"], {"verbosity": "high"})
        self.assertEqual(request["max_output_tokens"], 128_000)
        self.assertEqual(request["truncation"], "disabled")
        self.assertEqual(request["instructions"], deep_think.DEEP_MATH_INSTRUCTIONS)

    def test_client_reports_missing_endpoint_configuration(self):
        deep_think = load_module()

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "AZURE_OPENAI_ENDPOINT",
        ):
            deep_think.create_client(
                None,
                credential_factory=object,
                token_provider_factory=lambda *_args: object(),
                openai_factory=lambda **_kwargs: object(),
            )

    def test_client_rejects_credentials_embedded_in_endpoint(self):
        deep_think = load_module()

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "must not contain credentials",
        ):
            deep_think.create_client(
                "https://identity@example.invalid/openai/v1",
                credential_factory=object,
                token_provider_factory=lambda *_args: object(),
                openai_factory=lambda **_kwargs: object(),
            )

    def test_client_rejects_query_parameters_in_endpoint(self):
        deep_think = load_module()

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "must not contain credentials",
        ):
            deep_think.create_client(
                "https://example.invalid/openai/v1?credential=value",
                credential_factory=object,
                token_provider_factory=lambda *_args: object(),
                openai_factory=lambda **_kwargs: object(),
            )

    def test_client_rejects_fragment_in_endpoint(self):
        deep_think = load_module()

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "must not contain credentials",
        ):
            deep_think.create_client(
                "https://example.invalid/openai/v1#credential",
                credential_factory=object,
                token_provider_factory=lambda *_args: object(),
                openai_factory=lambda **_kwargs: object(),
            )

    def test_client_rejects_non_https_endpoint(self):
        deep_think = load_module()

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "must use HTTPS",
        ):
            deep_think.create_client(
                "http://example.invalid/openai/v1",
                credential_factory=object,
                token_provider_factory=lambda *_args: object(),
                openai_factory=lambda **_kwargs: object(),
            )

    def test_client_uses_entra_token_provider_without_api_keys(self):
        deep_think = load_module()
        credential = object()
        provider = object()
        captured = {}

        def token_provider_factory(actual_credential, scope):
            captured["credential"] = actual_credential
            captured["scope"] = scope
            return provider

        def openai_factory(**kwargs):
            captured["openai"] = kwargs
            return "client"

        client = deep_think.create_client(
            "https://example.invalid/openai/v1",
            credential_factory=lambda: credential,
            token_provider_factory=token_provider_factory,
            openai_factory=openai_factory,
        )

        self.assertEqual(client, "client")
        self.assertIs(captured["credential"], credential)
        self.assertEqual(captured["scope"], "https://ai.azure.com/.default")
        self.assertEqual(
            captured["openai"]["base_url"],
            "https://example.invalid/openai/v1/",
        )
        self.assertIs(captured["openai"]["api_key"], provider)
        self.assertEqual(captured["openai"]["max_retries"], 0)


class RetryTests(unittest.TestCase):
    def test_non_finite_retry_delay_is_rejected_before_api_call(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-unused", "Must not be used.")])

        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            (
                self.assertRaisesRegex(
                    deep_think.DeepThinkError,
                    "finite",
                )
            ),
        ):
            deep_think.run_turn(
                client,
                root=Path(temporary_directory),
                project="invalid-delay",
                title="Invalid Delay",
                prompt="Prove it.",
                deployment="gpt-5.6-sol",
                retry_base_delay=float("nan"),
            )

        self.assertEqual(client.responses.calls, [])

    def test_empty_response_retries_and_returns_the_next_complete_response(self):
        deep_think = load_module()
        empty = FakeResponse("resp-empty", "")
        complete = FakeResponse("resp-complete", "Complete proof.")
        client = FakeClient([empty, complete])
        sleeps = []
        events = []

        outcome = deep_think.request_response(
            client,
            {"model": "gpt-5.6-sol", "input": "Prove it."},
            purpose="answer",
            max_attempts=3,
            base_delay=0,
            sleep=sleeps.append,
            random_value=lambda: 0,
            on_retry=events.append,
        )

        self.assertIs(outcome.response, complete)
        self.assertEqual(outcome.retry_count, 1)
        self.assertEqual(len(client.responses.calls), 2)
        self.assertEqual(sleeps, [0])
        self.assertEqual(events[0].reason, "empty response")

    def test_transient_azure_error_retries_using_retry_after(self):
        deep_think = load_module()
        throttled = make_status_error(
            429,
            "no_capacity",
            headers={"retry-after": "2.5"},
        )
        complete = FakeResponse("resp-complete", "Complete proof.")
        client = FakeClient([throttled, complete])
        sleeps = []
        events = []

        outcome = deep_think.request_response(
            client,
            {"model": "gpt-5.6-sol", "input": "Prove it."},
            purpose="answer",
            max_attempts=3,
            sleep=sleeps.append,
            random_value=lambda: 0,
            on_retry=events.append,
        )

        self.assertIs(outcome.response, complete)
        self.assertEqual(outcome.retry_count, 1)
        self.assertEqual(len(client.responses.calls), 2)
        self.assertEqual(sleeps, [2.5])
        self.assertIn("HTTP 429", events[0].reason)
        self.assertIn("no_capacity", events[0].reason)

    def test_malformed_azure_response_is_retried(self):
        deep_think = load_module()
        malformed = make_validation_error()
        complete = FakeResponse("resp-complete", "Complete proof.")
        client = FakeClient([malformed, complete])

        outcome = deep_think.request_response(
            client,
            {"model": "gpt-5.6-sol", "input": "Prove it."},
            purpose="answer",
            max_attempts=2,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
        )

        self.assertIs(outcome.response, complete)
        self.assertEqual(outcome.retry_count, 1)
        self.assertEqual(len(client.responses.calls), 2)

    def test_partial_completed_response_is_retried_as_malformed(self):
        deep_think = load_module()

        class PartialResponse:
            def __init__(self):
                self.id = "resp-partial"
                self.status = "completed"
                self.output = []
                self.usage = SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=25),
                )

            @property
            def output_text(self):
                raise TypeError("response body was partially decoded")

        complete = FakeResponse("resp-complete", "Complete proof.")
        client = FakeClient([PartialResponse(), complete])
        events = []

        outcome = deep_think.request_response(
            client,
            {"model": "gpt-5.6-sol", "input": "Prove it."},
            purpose="answer",
            max_attempts=2,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
            on_retry=events.append,
        )

        self.assertIs(outcome.response, complete)
        self.assertEqual(outcome.retry_count, 1)
        self.assertEqual(len(client.responses.calls), 2)
        self.assertEqual(events[0].reason, "malformed Azure response")

    def test_nested_malformed_completed_response_is_retried(self):
        deep_think = load_module()
        malformed = FakeResponse("resp-malformed", "Visible but malformed.")
        malformed.output = [
            FakeOutput(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "not-a-list",
                }
            )
        ]
        complete = FakeResponse("resp-complete", "Complete proof.")
        client = FakeClient([malformed, complete])
        events = []

        outcome = deep_think.request_response(
            client,
            {"model": "gpt-5.6-sol", "input": "Prove it."},
            purpose="answer",
            max_attempts=2,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
            on_retry=events.append,
        )

        self.assertIs(outcome.response, complete)
        self.assertEqual(outcome.retry_count, 1)
        self.assertEqual(len(client.responses.calls), 2)
        self.assertEqual(events[0].reason, "malformed Azure response")

    def test_empty_response_exhaustion_reports_attempt_count(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-empty-1", ""),
                FakeResponse("resp-empty-2", ""),
                FakeResponse("resp-empty-3", ""),
            ]
        )

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "failed after 3 attempts: empty response",
        ):
            deep_think.request_response(
                client,
                {"model": "gpt-5.6-sol", "input": "Prove it."},
                purpose="answer",
                max_attempts=3,
                base_delay=0,
                sleep=lambda _delay: None,
                random_value=lambda: 0,
            )

        self.assertEqual(len(client.responses.calls), 3)

    def test_permanent_azure_error_is_not_retried(self):
        deep_think = load_module()
        invalid = make_status_error(400, "invalid_request")
        client = FakeClient([invalid, FakeResponse("resp-unused", "Must not be used.")])

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "failed without retry.*HTTP 400.*invalid_request",
        ):
            deep_think.request_response(
                client,
                {"model": "gpt-5.6-sol", "input": "Prove it."},
                purpose="answer",
                max_attempts=3,
                base_delay=0,
                sleep=lambda _delay: None,
            )

        self.assertEqual(len(client.responses.calls), 1)

    def test_failed_response_with_server_error_is_retried(self):
        deep_think = load_module()
        failed = FakeResponse(
            "resp-failed",
            "",
            status="failed",
            error=SimpleNamespace(
                code="server_error",
                message="Temporary Azure failure.",
            ),
        )
        complete = FakeResponse("resp-complete", "Complete proof.")
        client = FakeClient([failed, complete])
        events = []

        outcome = deep_think.request_response(
            client,
            {"model": "gpt-5.6-sol", "input": "Prove it."},
            purpose="answer",
            max_attempts=3,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
            on_retry=events.append,
        )

        self.assertIs(outcome.response, complete)
        self.assertEqual(outcome.retry_count, 1)
        self.assertIn("server_error", events[0].reason)
        self.assertEqual(len(client.responses.calls), 2)

    def test_context_limit_status_error_is_classified_for_recovery(self):
        deep_think = load_module()
        context_error = make_status_error(
            400,
            "context_length_exceeded",
            message="Maximum context length exceeded.",
        )
        client = FakeClient([context_error])

        with self.assertRaisesRegex(
            deep_think.ContextLimitError,
            "context limit",
        ):
            deep_think.request_response(
                client,
                {"model": "gpt-5.6-sol", "input": "Prove it."},
                purpose="answer",
                max_attempts=3,
                base_delay=0,
                sleep=lambda _delay: None,
            )

        self.assertEqual(len(client.responses.calls), 1)

    def test_refusal_is_not_retried_as_an_empty_response(self):
        deep_think = load_module()
        refused = FakeResponse("resp-refused", "")
        refused.output = [
            FakeOutput(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "refusal",
                            "refusal": "Cannot comply with this request.",
                        }
                    ],
                }
            )
        ]
        client = FakeClient([refused, FakeResponse("resp-unused", "Must not be used.")])

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "refused.*Cannot comply",
        ):
            deep_think.request_response(
                client,
                {"model": "gpt-5.6-sol", "input": "Prove it."},
                purpose="answer",
                max_attempts=3,
                base_delay=0,
                sleep=lambda _delay: None,
            )

        self.assertEqual(len(client.responses.calls), 1)

    def test_failed_forbidden_response_is_reported_without_retry(self):
        deep_think = load_module()
        forbidden = FakeResponse(
            "resp-forbidden",
            "",
            status="failed",
            incomplete_reason=None,
            error=SimpleNamespace(
                code="forbidden",
                message="Access is forbidden.",
            ),
        )
        client = FakeClient([forbidden])

        with self.assertRaisesRegex(
            deep_think.DeepThinkError,
            "failed without retry.*forbidden.*Access is forbidden",
        ):
            deep_think.request_response(
                client,
                {"model": "gpt-5.6-sol", "input": "Prove it."},
                purpose="answer",
                max_attempts=3,
                base_delay=0,
                sleep=lambda _delay: None,
            )

        self.assertEqual(len(client.responses.calls), 1)

    def test_oversized_visible_transcript_is_summarized_in_chunks(self):
        deep_think = load_module()
        transcript = "x" * (deep_think.MAX_INPUT_TOKENS + 100)
        client = FakeClient(
            [
                FakeResponse("resp-chunk-1", "Chunk summary 1."),
                FakeResponse("resp-chunk-2", "Chunk summary 2."),
                FakeResponse("resp-chunk-3", "Chunk summary 3."),
                FakeResponse(
                    "resp-final",
                    "# Continuation Summary\n\nCombined summary.",
                ),
            ]
        )

        outcome = deep_think._request_visible_transcript_summary(
            client,
            transcript=transcript,
            deployment="gpt-5.6-sol",
            minimum_response_tokens=25_000,
            max_attempts=2,
            retry_base_delay=0,
            retry_max_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
            on_retry=None,
        )

        self.assertEqual(outcome.response.id, "resp-final")
        self.assertEqual(len(client.responses.calls), 4)
        for call in client.responses.calls[:3]:
            self.assertLessEqual(
                len(call["input"][0]["content"].encode("utf-8")),
                deep_think.MAX_INPUT_TOKENS,
            )
        self.assertIn(
            "Chunk summary 3.",
            client.responses.calls[3]["input"][0]["content"],
        )


class PersistenceTests(unittest.TestCase):
    def test_existing_project_lock_blocks_concurrent_writer(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-unused", "Must not be used.")])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_dir = root / "concurrent-project"
            project_dir.mkdir()
            (project_dir / ".deep-think.lock").write_text(
                "another process",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "already locked",
            ):
                deep_think.run_turn(
                    client,
                    root=root,
                    project="concurrent-project",
                    title="Concurrent Project",
                    prompt="Prove it.",
                    deployment="gpt-5.6-sol",
                )

        self.assertEqual(client.responses.calls, [])

    def test_run_turn_retries_empty_response_and_persists_only_success(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-empty", ""),
                FakeResponse("resp-complete", "Complete proof."),
            ]
        )
        events = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result = deep_think.run_turn(
                client,
                root=root,
                project="retry-persistence",
                title="Retry Persistence",
                prompt="Prove the theorem.",
                deployment="gpt-5.6-sol",
                max_attempts=2,
                retry_base_delay=0,
                sleep=lambda _delay: None,
                random_value=lambda: 0,
                on_retry=events.append,
            )
            project_dir = root / "retry-persistence"
            context = json.loads((project_dir / "0001-context.json").read_text("utf-8"))
            transcript = (project_dir / "0001-transcript.md").read_text("utf-8")

        self.assertEqual(result.text, "Complete proof.")
        self.assertEqual(len(context), 3)
        self.assertNotIn("resp-empty", json.dumps(context))
        self.assertIn("Application retries: 1", transcript)
        self.assertEqual(len(events), 1)

    def test_context_error_forces_rollover_and_replays_prompt_once(self):
        deep_think = load_module()
        context_error = make_status_error(
            400,
            "context_length_exceeded",
            message="Maximum context length exceeded.",
        )
        client = FakeClient(
            [
                FakeResponse("resp-1", "Initial result."),
                context_error,
                FakeResponse(
                    "resp-summary",
                    "# Continuation Summary\n\nCarry the initial result.",
                ),
                FakeResponse("resp-2", "Recovered continuation."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "reactive-rollover",
                "deployment": "gpt-5.6-sol",
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="Reactive Rollover",
                prompt="First turn.",
            )
            result = deep_think.run_turn(
                **common,
                prompt="Second turn.",
            )

            project_dir = root / "reactive-rollover"
            state = json.loads((project_dir / "state.json").read_text("utf-8"))
            old_context = json.loads(
                (project_dir / "0001-context.json").read_text("utf-8")
            )
            new_context = json.loads(
                (project_dir / "0002-context.json").read_text("utf-8")
            )

        self.assertTrue(result.rolled_over)
        self.assertEqual(result.volume, 2)
        self.assertEqual(state["turn"], 1)
        self.assertNotIn("Second turn.", json.dumps(old_context))
        self.assertEqual(
            sum(
                item.get("role") == "user" and item.get("content") == "Second turn."
                for item in new_context
            ),
            1,
        )
        self.assertEqual(len(client.responses.calls), 4)

    def test_constrained_output_limit_forces_rollover_and_retries(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Large initial result.",
                    input_tokens=700_000,
                    output_tokens=100_000,
                ),
                FakeResponse(
                    "resp-incomplete",
                    "Partial answer.",
                    status="incomplete",
                    incomplete_reason="max_output_tokens",
                ),
                FakeResponse(
                    "resp-summary",
                    "# Continuation Summary\n\nCarry the initial result.",
                ),
                FakeResponse("resp-2", "Recovered complete answer."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "output-recovery",
                "deployment": "gpt-5.6-sol",
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="Output Recovery",
                prompt="First turn.",
            )
            result = deep_think.run_turn(
                **common,
                prompt="Second turn.",
            )

            project_dir = root / "output-recovery"
            state = json.loads((project_dir / "state.json").read_text("utf-8"))
            new_context = json.loads(
                (project_dir / "0002-context.json").read_text("utf-8")
            )

        self.assertTrue(result.rolled_over)
        self.assertEqual(state["volume"], 2)
        self.assertNotIn("Partial answer.", json.dumps(new_context))
        self.assertIn("Recovered complete answer.", json.dumps(new_context))
        self.assertEqual(len(client.responses.calls), 4)

    def test_output_limit_without_larger_fresh_budget_fails_without_answer_retry(self):
        deep_think = load_module()
        summary_text = "# Continuation Summary\n\n" + ("Carry.\n" * 28)
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Initial result.",
                    input_tokens=250,
                    output_tokens=250,
                ),
                FakeResponse(
                    "resp-incomplete",
                    "Partial answer.",
                    status="incomplete",
                    incomplete_reason="max_output_tokens",
                ),
                FakeResponse("resp-summary", summary_text),
                FakeResponse("resp-unused", "Must not be used."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "output-budget-guard",
                "deployment": "gpt-5.6-sol",
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="Output Budget Guard",
                prompt="First turn.",
            )
            prompt = "x" * (deep_think.DEFAULT_ROLLOVER_TOKENS - 500 - 25_100)

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "split|narrow",
            ):
                deep_think.run_turn(
                    **common,
                    prompt=prompt,
                )

            project_dir = root / "output-budget-guard"
            state = json.loads((project_dir / "state.json").read_text("utf-8"))
            new_context = json.loads(
                (project_dir / "0002-context.json").read_text("utf-8")
            )

        self.assertEqual(len(client.responses.calls), 3)
        self.assertEqual(state["volume"], 2)
        self.assertEqual(state["turn"], 0)
        self.assertEqual(new_context[0]["role"], "developer")
        self.assertNotIn(prompt, json.dumps(new_context))

    def test_persisted_rollover_counts_carried_context_before_next_turn_preflight(self):
        deep_think = load_module()
        summary_text = "# Continuation Summary\n\n" + ("Carry.\n" * 8_000)
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Initial result.",
                    input_tokens=250,
                    output_tokens=250,
                ),
                FakeResponse(
                    "resp-incomplete",
                    "Partial answer.",
                    status="incomplete",
                    incomplete_reason="max_output_tokens",
                ),
                FakeResponse("resp-summary", summary_text),
                FakeResponse(
                    "resp-summary-2",
                    "# Continuation Summary\n\nCompressed carry.",
                ),
                FakeResponse("resp-2", "Recovered after the next rollover."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "persisted-rollover-budget",
                "deployment": "gpt-5.6-sol",
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="Persisted Rollover Budget",
                prompt="First turn.",
            )
            estimated_carried_context = (
                f"{deep_think.CARRIED_CONTEXT_PREFIX.rstrip()}\n\n"
                f"{summary_text.rstrip()}"
            )
            estimated_state_context = deep_think._estimate_tokens(
                deep_think._canonical_json_text(
                    {
                        "type": "message",
                        "role": "developer",
                        "content": estimated_carried_context,
                    }
                )
            )
            prompt = "x" * min(
                deep_think.DEFAULT_ROLLOVER_TOKENS - 500 - 40_000,
                deep_think.MAX_INPUT_TOKENS - estimated_state_context - 1,
            )

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "split|narrow",
            ):
                deep_think.run_turn(
                    **common,
                    prompt=prompt,
                )

            project_dir = root / "persisted-rollover-budget"
            state = json.loads((project_dir / "state.json").read_text("utf-8"))
            new_context = json.loads(
                (project_dir / "0002-context.json").read_text("utf-8")
            )
            carried_context = new_context[0]["content"]
            oversized_prompt = "y" * (
                deep_think.MAX_INPUT_TOKENS
                - deep_think._estimate_tokens(carried_context)
                + 1
            )

            deep_think.run_turn(
                **common,
                prompt=oversized_prompt,
            )

        self.assertGreaterEqual(
            state["context_tokens"],
            deep_think._estimate_tokens(carried_context),
        )
        self.assertEqual(
            client.responses.calls[3]["input"][-1]["content"],
            deep_think.ROLLOVER_SUMMARY_PROMPT,
        )

    def test_summary_context_error_retries_from_visible_transcript(self):
        deep_think = load_module()
        summary_context_error = make_status_error(
            400,
            "context_length_exceeded",
            message="Maximum context length exceeded.",
        )
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Initial result.",
                    input_tokens=780_000,
                    output_tokens=100_000,
                ),
                summary_context_error,
                FakeResponse(
                    "resp-summary",
                    "# Continuation Summary\n\nRecovered from the transcript.",
                ),
                FakeResponse("resp-2", "Continued safely."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "visible-summary-recovery",
                "deployment": "gpt-5.6-sol",
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="Visible Summary Recovery",
                prompt="First turn.",
            )
            result = deep_think.run_turn(
                **common,
                prompt="Second turn.",
            )
            old_transcript = (
                root / "visible-summary-recovery" / "0001-transcript.md"
            ).read_text("utf-8")

        recovery_input = client.responses.calls[2]["input"]
        self.assertTrue(result.rolled_over)
        self.assertIn(
            deep_think.VISIBLE_TRANSCRIPT_SUMMARY_PROMPT,
            recovery_input[0]["content"],
        )
        self.assertNotIn("encrypted_content", recovery_input[0]["content"])
        self.assertIn("visible transcript recovery", old_transcript)
        self.assertEqual(len(client.responses.calls), 4)

    def test_invalid_encrypted_content_recovers_from_visible_transcript(self):
        deep_think = load_module()
        replay_error = make_status_error(
            400,
            "invalid_encrypted_content",
            message="The encrypted content could not be verified.",
        )
        client = FakeClient(
            [
                FakeResponse("resp-1", "Initial result."),
                replay_error,
                FakeResponse(
                    "resp-summary",
                    "# Continuation Summary\n\nRecovered from the visible transcript.",
                ),
                FakeResponse("resp-2", "Continued safely."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "invalid-encrypted-content",
                "deployment": "gpt-5.6-sol",
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="Invalid Encrypted Content",
                prompt="First turn.",
            )
            result = deep_think.run_turn(
                **common,
                prompt="Second turn.",
            )
            old_transcript = (
                root / "invalid-encrypted-content" / "0001-transcript.md"
            ).read_text("utf-8")

        recovery_input = client.responses.calls[2]["input"]
        self.assertTrue(result.rolled_over)
        self.assertIn(
            deep_think.VISIBLE_TRANSCRIPT_SUMMARY_PROMPT,
            recovery_input[0]["content"],
        )
        self.assertNotIn("encrypted_content", recovery_input[0]["content"])
        self.assertIn("visible transcript recovery", old_transcript)
        self.assertEqual(len(client.responses.calls), 4)

    def test_unrelated_bad_request_does_not_trigger_visible_recovery(self):
        deep_think = load_module()
        invalid = make_status_error(
            400,
            "invalid_request",
            message="The encrypted content could not be verified.",
        )
        client = FakeClient(
            [
                FakeResponse("resp-1", "Initial result."),
                invalid,
                FakeResponse("resp-unused", "Must not be used."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "no-visible-recovery",
                "deployment": "gpt-5.6-sol",
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="No Visible Recovery",
                prompt="First turn.",
            )
            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "invalid_request",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 2)

    def test_unreplayable_context_rolls_over_directly_from_transcript(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Initial visible result.",
                    input_tokens=850_000,
                    output_tokens=100_000,
                ),
                FakeResponse(
                    "resp-summary",
                    "# Continuation Summary\n\nRecovered directly.",
                ),
                FakeResponse("resp-2", "Continued safely."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "unreplayable-context",
                "deployment": "gpt-5.6-sol",
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="Unreplayable Context",
                prompt="First turn.",
            )
            result = deep_think.run_turn(
                **common,
                prompt="Second turn.",
            )
            old_transcript = (
                root / "unreplayable-context" / "0001-transcript.md"
            ).read_text("utf-8")

        self.assertTrue(result.rolled_over)
        self.assertIn(
            deep_think.VISIBLE_TRANSCRIPT_SUMMARY_PROMPT,
            client.responses.calls[1]["input"][0]["content"],
        )
        self.assertIn("visible transcript recovery", old_transcript)
        self.assertEqual(len(client.responses.calls), 3)

    def test_first_turn_context_error_fails_without_creating_state(self):
        deep_think = load_module()
        client = FakeClient(
            [
                make_status_error(
                    400,
                    "context_length_exceeded",
                    message="Maximum context length exceeded.",
                )
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "first-turn context.*split the prompt",
            ):
                deep_think.run_turn(
                    client,
                    root=root,
                    project="first-turn-context",
                    title="First Turn Context",
                    prompt="A large first prompt.",
                    deployment="gpt-5.6-sol",
                    retry_base_delay=0,
                    sleep=lambda _delay: None,
                )
            state_path = root / "first-turn-context" / "state.json"
            state_exists = state_path.exists()

        self.assertFalse(state_exists)
        self.assertEqual(len(client.responses.calls), 1)

    def test_first_turn_creates_local_context_and_markdown_transcript(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "# Deep Think Result\n\nA candidate construction.",
                    input_tokens=1_000,
                    output_tokens=500,
                    reasoning_tokens=300,
                )
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result = deep_think.run_turn(
                client,
                root=root,
                project="banach-construction",
                title="Banach Construction",
                prompt="Construct the required Banach space.",
                deployment="gpt-5.6-sol",
            )

            project_dir = root / "banach-construction"
            state = json.loads((project_dir / "state.json").read_text("utf-8"))
            context = json.loads((project_dir / "0001-context.json").read_text("utf-8"))
            transcript = (project_dir / "0001-transcript.md").read_text("utf-8")

        self.assertEqual(
            result.text, "# Deep Think Result\n\nA candidate construction."
        )
        self.assertEqual(result.volume, 1)
        self.assertFalse(result.rolled_over)
        self.assertEqual(state["project"], "banach-construction")
        self.assertEqual(state["turn"], 1)
        self.assertEqual(state["context_tokens"], 1_500)
        self.assertEqual(
            context[0],
            {
                "type": "message",
                "role": "user",
                "content": "Construct the required Banach space.",
            },
        )
        self.assertEqual(context[1]["type"], "reasoning")
        self.assertEqual(context[2]["role"], "assistant")
        self.assertIn("# Banach Construction - Volume 0001", transcript)
        self.assertIn("## Conversation 1", transcript)
        self.assertIn("### User", transcript)
        self.assertIn("### Assistant", transcript)
        self.assertIn("### Usage", transcript)
        self.assertIn("Reasoning effort: `max`", transcript)

    def test_follow_up_replays_every_locally_stored_output_item(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-1", "First analysis."),
                FakeResponse("resp-2", "Second analysis."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "local-history",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Local History",
                prompt="Analyze the first lemma.",
            )
            deep_think.run_turn(
                **common,
                prompt="Use it in the second lemma.",
            )

            project_dir = root / "local-history"
            context = json.loads((project_dir / "0001-context.json").read_text("utf-8"))
            transcript = (project_dir / "0001-transcript.md").read_text("utf-8")

        second_input = client.responses.calls[1]["input"]
        self.assertEqual(second_input[:3], context[:3])
        self.assertEqual(second_input[1]["type"], "reasoning")
        self.assertEqual(second_input[2]["role"], "assistant")
        self.assertEqual(
            second_input[3],
            {
                "type": "message",
                "role": "user",
                "content": "Use it in the second lemma.",
            },
        )
        self.assertEqual(len(context), 6)
        self.assertIn("## Conversation 1", transcript)
        self.assertIn("## Conversation 2", transcript)

    def test_threshold_summarizes_old_volume_and_seeds_a_new_volume(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Initial investigation.",
                    input_tokens=780_000,
                    output_tokens=100_000,
                ),
                FakeResponse(
                    "resp-summary",
                    "# Continuation Summary\n\nKeep lemma A and reject route B.",
                    input_tokens=110,
                    output_tokens=20,
                ),
                FakeResponse(
                    "resp-2",
                    "Continued from the summary.",
                    input_tokens=80,
                    output_tokens=30,
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "rollover-proof",
                "deployment": "gpt-5.6-sol",
                "rollover_tokens": 900_000,
            }
            deep_think.run_turn(
                **common,
                title="Rollover Proof",
                prompt="Begin the proof.",
            )
            result = deep_think.run_turn(
                **common,
                prompt="Continue with the obstruction.",
            )

            project_dir = root / "rollover-proof"
            old_transcript = (project_dir / "0001-transcript.md").read_text("utf-8")
            new_transcript = (project_dir / "0002-transcript.md").read_text("utf-8")
            new_context = json.loads(
                (project_dir / "0002-context.json").read_text("utf-8")
            )
            state = json.loads((project_dir / "state.json").read_text("utf-8"))

        self.assertTrue(result.rolled_over)
        self.assertEqual(result.volume, 2)
        self.assertEqual(len(client.responses.calls), 3)
        self.assertEqual(
            client.responses.calls[1]["input"][-1]["content"],
            deep_think.ROLLOVER_SUMMARY_PROMPT,
        )
        self.assertIn("## Volume rollover summary", old_transcript)
        self.assertIn("Keep lemma A and reject route B.", old_transcript)
        self.assertIn("# Rollover Proof - Volume 0002", new_transcript)
        self.assertIn("## Carried context", new_transcript)
        self.assertIn("Keep lemma A and reject route B.", new_transcript)
        self.assertEqual(new_context[0]["role"], "developer")
        self.assertIn("Keep lemma A", new_context[0]["content"])
        self.assertEqual(new_context[1]["role"], "user")
        self.assertEqual(state["volume"], 2)
        self.assertEqual(state["turn"], 1)

    def test_pending_prompt_triggers_rollover_before_crossing_threshold(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Initial investigation.",
                    input_tokens=750_000,
                    output_tokens=125_000,
                ),
                FakeResponse("resp-summary", "# Continuation Summary\n\nCarry."),
                FakeResponse("resp-2", "Safe continuation."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "prompt-rollover",
                "deployment": "gpt-5.6-sol",
                "rollover_tokens": 900_000,
            }
            deep_think.run_turn(
                **common,
                title="Prompt Rollover",
                prompt="Begin.",
            )
            result = deep_think.run_turn(
                **common,
                prompt="x" * 60,
            )

        self.assertTrue(result.rolled_over)
        self.assertEqual(result.volume, 2)
        self.assertEqual(len(client.responses.calls), 3)

    def test_proactive_rollover_does_not_consume_reactive_context_recovery(self):
        deep_think = load_module()
        context_error = make_status_error(
            400,
            "context_length_exceeded",
            message="Maximum context length exceeded.",
        )
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Initial investigation.",
                    input_tokens=750_000,
                    output_tokens=125_000,
                ),
                FakeResponse(
                    "resp-summary-1",
                    "# Continuation Summary\n\nCarry the first volume.",
                ),
                context_error,
                FakeResponse(
                    "resp-summary-2",
                    "# Continuation Summary\n\nRecover after the proactive rollover.",
                ),
                FakeResponse("resp-2", "Recovered after reactive rollover."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "proactive-then-reactive",
                "deployment": "gpt-5.6-sol",
                "rollover_tokens": 900_000,
                "retry_base_delay": 0,
                "sleep": lambda _delay: None,
            }
            deep_think.run_turn(
                **common,
                title="Proactive Then Reactive",
                prompt="Begin.",
            )
            result = deep_think.run_turn(
                **common,
                prompt="x" * 60,
            )

            project_dir = root / "proactive-then-reactive"
            state = json.loads((project_dir / "state.json").read_text("utf-8"))
            second_volume = json.loads(
                (project_dir / "0002-context.json").read_text("utf-8")
            )
            third_volume = json.loads(
                (project_dir / "0003-context.json").read_text("utf-8")
            )

        self.assertTrue(result.rolled_over)
        self.assertEqual(result.volume, 3)
        self.assertEqual(state["volume"], 3)
        self.assertEqual(state["turn"], 1)
        self.assertNotIn("x" * 60, json.dumps(second_volume))
        self.assertEqual(
            sum(
                item.get("role") == "user" and item.get("content") == "x" * 60
                for item in third_volume
            ),
            1,
        )
        self.assertEqual(len(client.responses.calls), 5)

    def test_existing_project_rejects_a_missing_context_file(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-1", "Saved result."),
                FakeResponse("resp-2", "Must not be used."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "missing-context",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Missing Context",
                prompt="First turn.",
            )
            (root / "missing-context" / "0001-context.json").unlink()

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "context file is missing",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 1)

    def test_existing_project_rejects_a_missing_transcript_file(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-1", "Saved result.")])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "missing-transcript",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Missing Transcript",
                prompt="First turn.",
            )
            (root / "missing-transcript" / "0001-transcript.md").unlink()

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "transcript file is missing",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 1)

    def test_existing_project_rejects_a_corrupt_context_file(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-1", "Saved result.")])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "corrupt-context",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Corrupt Context",
                prompt="First turn.",
            )
            (root / "corrupt-context" / "0001-context.json").write_text(
                "{not-json",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "Could not read JSON",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 1)

    def test_existing_project_rejects_a_non_array_context(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-1", "Saved result."),
                FakeResponse("resp-2", "Must not be used."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "invalid-context-shape",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Invalid Context Shape",
                prompt="First turn.",
            )
            (root / "invalid-context-shape" / "0001-context.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "must contain a JSON array",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 1)

    def test_existing_project_rejects_context_changed_outside_transaction(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-1", "Saved result."),
                FakeResponse("resp-2", "Must not be used."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "context-checksum",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Context Checksum",
                prompt="First turn.",
            )
            context_path = root / "context-checksum" / "0001-context.json"
            context = json.loads(context_path.read_text("utf-8"))
            context.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": "Uncommitted turn.",
                }
            )
            context_path.write_text(
                json.dumps(context, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "context checksum",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 1)

    def test_existing_project_rejects_valid_shape_state_tampering(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-1", "Saved result."),
                FakeResponse("resp-2", "Must not be used."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "state-digest",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="State Digest",
                prompt="First turn.",
            )
            state_path = root / "state-digest" / "state.json"
            state = json.loads(state_path.read_text("utf-8"))
            state["turn"] = 99
            state_path.write_text(
                json.dumps(state, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "state digest",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 1)

    def test_current_state_rejects_missing_context_checksum(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-1", "Saved result."),
                FakeResponse("resp-2", "Must not be used."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "missing-state-checksum",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Missing State Checksum",
                prompt="First turn.",
            )
            state_path = root / "missing-state-checksum" / "state.json"
            state = json.loads(state_path.read_text("utf-8"))
            state.pop("context_sha256", None)
            state_path.write_text(
                json.dumps(state, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "requires.*checksum",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 1)

    def test_legacy_state_is_migrated_on_next_successful_turn(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-1", "Saved result."),
                FakeResponse("resp-2", "Continued safely."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "legacy-state",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Legacy State",
                prompt="First turn.",
            )
            state_path = root / "legacy-state" / "state.json"
            legacy_state = json.loads(state_path.read_text("utf-8"))
            legacy_state["version"] = 1
            legacy_state.pop("schema", None)
            legacy_state.pop("context_sha256", None)
            legacy_state.pop("transcript_sha256", None)
            legacy_state.pop("state_sha256", None)
            state_path.write_text(
                json.dumps(legacy_state, indent=2) + "\n",
                encoding="utf-8",
            )

            deep_think.run_turn(
                **common,
                prompt="Second turn.",
            )
            migrated_state = json.loads(state_path.read_text("utf-8"))

        self.assertEqual(migrated_state["version"], 2)
        self.assertIn("schema", migrated_state)
        self.assertEqual(len(migrated_state["context_sha256"]), 64)
        self.assertEqual(len(migrated_state["transcript_sha256"]), 64)
        self.assertEqual(len(migrated_state["state_sha256"]), 64)

    def test_existing_project_rejects_invalid_state_shape(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-1", "Saved result.")])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "invalid-state",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Invalid State",
                prompt="First turn.",
            )
            (root / "invalid-state" / "state.json").write_text(
                '{"version": 1}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                deep_think.DeepThinkError,
                "State file is invalid",
            ):
                deep_think.run_turn(
                    **common,
                    prompt="Second turn.",
                )

        self.assertEqual(len(client.responses.calls), 1)

    def test_output_cap_preserves_room_below_the_rollover_threshold(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse(
                    "resp-1",
                    "Large context.",
                    input_tokens=700_000,
                    output_tokens=100_000,
                ),
                FakeResponse("resp-2", "Bounded continuation."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = {
                "client": client,
                "root": root,
                "project": "bounded-output",
                "deployment": "gpt-5.6-sol",
            }
            deep_think.run_turn(
                **common,
                title="Bounded Output",
                prompt="First turn.",
            )
            deep_think.run_turn(
                **common,
                prompt="Continue.",
            )

        self.assertLessEqual(
            client.responses.calls[1]["max_output_tokens"],
            100_000,
        )
        self.assertGreaterEqual(
            client.responses.calls[1]["max_output_tokens"],
            deep_think.MIN_RESPONSE_TOKENS,
        )

    def test_first_prompt_over_maximum_input_is_rejected_before_api_call(self):
        deep_think = load_module()
        client = FakeClient([])

        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            (
                self.assertRaisesRegex(
                    deep_think.DeepThinkError,
                    "922,000-token maximum input",
                )
            ),
        ):
            deep_think.run_turn(
                client,
                root=Path(temporary_directory),
                project="oversized-prompt",
                title="Oversized Prompt",
                prompt="x" * (deep_think.MAX_INPUT_TOKENS + 1),
                deployment="gpt-5.6-sol",
            )

        self.assertEqual(client.responses.calls, [])


class CommandLineTests(unittest.TestCase):
    def test_ask_reads_prompt_file_and_prints_structured_result(self):
        deep_think = load_module()
        client = FakeClient(
            [FakeResponse("resp-cli", "# Deep Think Result\n\nCLI result.")]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prompt_file = root / "problem.txt"
            prompt_file.write_text("Solve the CLI problem.", encoding="utf-8")

            exit_code = deep_think.main(
                [
                    "ask",
                    "--project",
                    "cli-problem",
                    "--title",
                    "CLI Problem",
                    "--prompt-file",
                    str(prompt_file),
                    "--root",
                    str(root / "transcripts"),
                ],
                client_factory=lambda _endpoint: client,
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stdout.getvalue(),
            "# Deep Think Result\n\nCLI result.\n",
        )
        self.assertIn("0001-transcript.md", stderr.getvalue())
        self.assertEqual(
            client.responses.calls[0]["input"][0]["content"],
            "Solve the CLI problem.",
        )

    def test_cli_reports_retry_progress(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-empty", ""),
                FakeResponse("resp-cli", "Recovered result."),
            ]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            exit_code = deep_think.main(
                [
                    "ask",
                    "--project",
                    "cli-retry",
                    "--title",
                    "CLI Retry",
                    "--prompt",
                    "Prove it.",
                    "--root",
                    str(Path(temporary_directory) / "transcripts"),
                    "--max-attempts",
                    "2",
                    "--retry-base-delay",
                    "0",
                ],
                client_factory=lambda _endpoint: client,
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "Recovered result.\n")
        self.assertIn(
            "Retrying answer after empty response (attempt 1/2",
            stderr.getvalue(),
        )

    def test_cli_reports_atomic_write_failures_without_tracebacks(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-cli", "CLI result.")])
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            mock.patch.object(
                deep_think.os,
                "replace",
                side_effect=OSError("disk full"),
            ),
        ):
            exit_code = deep_think.main(
                [
                    "ask",
                    "--project",
                    "cli-disk-full",
                    "--title",
                    "CLI Disk Full",
                    "--prompt",
                    "Prove it.",
                    "--root",
                    str(Path(temporary_directory) / "transcripts"),
                ],
                client_factory=lambda _endpoint: client,
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("deep-think:", stderr.getvalue())
        self.assertIn("disk full", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
