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
    def __init__(self, responses, retrieved=None):
        self.queued = list(responses)
        self.retrieved = list(retrieved or [])
        self.calls = []
        self.retrieve_calls = []
        self.retrieve_timeouts = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.queued.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def retrieve(self, response_id, *, timeout=None):
        self.retrieve_calls.append(response_id)
        self.retrieve_timeouts.append(timeout)
        result = self.retrieved.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeClient:
    def __init__(self, responses, retrieved=None):
        self.responses = FakeResponses(responses, retrieved)


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
    def test_default_credential_allows_slow_azure_cli_token_commands(self):
        deep_think = load_module()
        captured = {}

        class FakeCredential:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with mock.patch("azure.identity.DefaultAzureCredential", FakeCredential):
            deep_think.create_client(
                "https://example.invalid/openai/v1/",
                token_provider_factory=lambda *_args: lambda: "test-token",
                openai_factory=lambda **_kwargs: object(),
            )

        self.assertEqual(captured, {"process_timeout": 60})

    def test_default_model_and_attempt_budget_cover_gpt6_chain(self):
        deep_think = load_module()
        with mock.patch.dict("os.environ", {}, clear=True):
            args = deep_think._build_parser().parse_args(
                ["ask", "--project", "upgrade", "--prompt", "Continue."]
            )
        self.assertEqual(args.deployment, "gpt-6-astra")
        self.assertEqual(args.max_attempts, 5)

    def test_preview_responses_url_preserves_api_version_for_create_and_poll(self):
        import httpx
        import openai

        deep_think = load_module()
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={"id": "resp-preview", "status": "completed", "output": []},
            )

        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
            client = deep_think.create_client(
                "https://example.invalid/openai/responses"
                "?api-version=2025-04-01-preview",
                credential_factory=object,
                token_provider_factory=lambda *_args: lambda: "test-token",
                openai_factory=lambda **kwargs: openai.OpenAI(
                    **kwargs, http_client=http_client
                ),
            )
            client.responses.create(model="gpt-6-astra", input="Hello.")
            client.responses.retrieve("resp-preview")
        self.assertEqual(
            [request.url.path for request in requests],
            ["/openai/responses", "/openai/responses/resp-preview"],
        )
        for request in requests:
            self.assertEqual(request.url.params["api-version"], "2025-04-01-preview")

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
        self.assertTrue(request["background"])
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

    def test_preview_url_rejects_extra_duplicate_or_invalid_query_parameters(self):
        deep_think = load_module()
        for query in [
            "api-version=2025-04-01-preview&token=secret",
            "api-version=2025-04-01-preview&api-version=2025-04-01-preview",
            "api-version=not-a-version",
        ]:
            with (
                self.subTest(query=query),
                self.assertRaisesRegex(
                    deep_think.DeepThinkError, "must not contain credentials"
                ),
            ):
                deep_think.create_client(
                    "https://example.invalid/openai/responses?" + query,
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
        self.assertEqual(captured["openai"]["timeout"], 3600)


class CrossEndpointFailoverTests(unittest.TestCase):
    def test_backup_deployment_can_differ_from_primary_model_name(self):
        deep_think = load_module()
        primary = FakeClient([make_status_error(429, "rate_limit_exceeded")])
        backup = FakeClient([FakeResponse("resp-backup", "Complete.")])
        clients = {"primary": primary, "backup": backup}
        router = deep_think.create_routed_client(
            "primary",
            "gpt-6-astra",
            backup_endpoint="backup",
            backup_deployment="gpt-6-astra-backup",
            client_factory=clients.__getitem__,
        )
        outcome = deep_think.request_response(
            router,
            deep_think.build_response_request([], "gpt-6-astra"),
            purpose="answer",
            sleep=lambda _: None,
        )
        self.assertEqual(outcome.deployment, "gpt-6-astra-backup")
        self.assertEqual(backup.responses.calls[0]["reasoning"]["mode"], "pro")
        self.assertEqual(backup.responses.calls[0]["reasoning"]["effort"], "max")

    def make_router(self, deep_think, results, retrieved=None):
        primary = FakeClient(results[:1])
        backup = FakeClient(results[1:2], retrieved)
        legacy = FakeClient(results[2:])
        clients = {"primary": primary, "backup": backup, "legacy": legacy}
        router = deep_think.create_routed_client(
            "primary",
            "gpt-6-astra",
            backup_endpoint="backup",
            fallback_endpoint="legacy",
            client_factory=clients.__getitem__,
        )
        return router, primary, backup, legacy

    def test_all_five_targets_are_tried_in_priority_order_with_correct_profiles(self):
        deep_think = load_module()
        router, primary, backup, legacy = self.make_router(
            deep_think,
            [
                make_status_error(429, "rate_limit_exceeded"),
                make_status_error(500, "server_error"),
                make_status_error(503, "server_error"),
                make_status_error(429, "no_capacity"),
                FakeResponse("resp-fallback", "Complete."),
            ],
        )
        request = deep_think.build_response_request([], "gpt-6-astra")
        outcome = deep_think.request_response(
            router, request, purpose="answer", sleep=lambda _: None
        )
        calls = (
            primary.responses.calls + backup.responses.calls + legacy.responses.calls
        )
        self.assertEqual(
            [call["model"] for call in calls],
            [
                "gpt-6-astra",
                "gpt-6-astra",
                "gpt-5.6-sol",
                "gpt-5.6-sol-nofilters",
                "gpt-5.4-pro",
            ],
        )
        for call in calls[:4]:
            self.assertEqual(call["reasoning"], request["reasoning"])
        self.assertEqual(calls[4]["reasoning"], {"effort": "xhigh", "summary": "auto"})
        self.assertEqual(outcome.deployment, "gpt-5.4-pro")
        self.assertEqual(outcome.retry_count, 4)

    def test_transient_submission_errors_advance_to_backup(self):
        import httpx
        import openai

        deep_think = load_module()
        request = httpx.Request("POST", "https://test")
        refused = openai.APIConnectionError(request=request)
        refused.__cause__ = httpx.ConnectError("refused", request=request)
        connect_timeout = openai.APITimeoutError(request=request)
        connect_timeout.__cause__ = httpx.ConnectTimeout("timeout", request=request)
        malformed = FakeResponse("resp-malformed", "Bad.")
        malformed.usage = None
        failures = [
            make_status_error(408, "timeout"),
            make_status_error(409, "conflict"),
            make_status_error(500, "server_error"),
            make_status_error(404, "DeploymentNotFound"),
            refused,
            connect_timeout,
            malformed,
            FakeResponse("resp-empty", ""),
            FakeResponse(
                "resp-failed",
                "",
                status="failed",
                error=SimpleNamespace(code="server_error", message="Unavailable."),
            ),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                router, primary, backup, _ = self.make_router(
                    deep_think, [failure, FakeResponse("resp-ok", "Complete.")]
                )
                deep_think.request_response(
                    router,
                    deep_think.build_response_request([], "gpt-6-astra"),
                    purpose="answer",
                    sleep=lambda _: None,
                )
                self.assertEqual(len(primary.responses.calls), 1)
                self.assertEqual(len(backup.responses.calls), 1)

    def test_backup_poll_stays_on_original_endpoint_and_next_request_resets(self):
        deep_think = load_module()
        router, primary, backup, legacy = self.make_router(
            deep_think,
            [
                make_status_error(429, "rate_limit_exceeded"),
                FakeResponse("resp-job", "", status="queued"),
            ],
            [
                make_status_error(500, "server_error"),
                FakeResponse("resp-job", "Complete."),
            ],
        )
        request = deep_think.build_response_request([], "gpt-6-astra")
        deep_think.request_response(
            router, request, purpose="answer", sleep=lambda _: None
        )
        primary.responses.queued.append(FakeResponse("resp-new", "New answer."))
        deep_think.request_response(
            router, request, purpose="rollover", sleep=lambda _: None
        )
        self.assertEqual(backup.responses.retrieve_calls, ["resp-job", "resp-job"])
        self.assertEqual(primary.responses.retrieve_calls, [])
        self.assertEqual(len(primary.responses.calls), 2)
        self.assertEqual(len(backup.responses.calls), 1)
        self.assertEqual(legacy.responses.calls, [])

    def test_poll_exhaustion_does_not_submit_another_job(self):
        deep_think = load_module()
        router, primary, backup, legacy = self.make_router(
            deep_think,
            [
                make_status_error(429, "rate_limit_exceeded"),
                FakeResponse("resp-job", "", status="queued"),
            ],
            [make_status_error(500, "server_error") for _ in range(2)],
        )
        with self.assertRaisesRegex(deep_think.DeepThinkError, "poll failed"):
            deep_think.request_response(
                router,
                deep_think.build_response_request([], "gpt-6-astra"),
                purpose="answer",
                max_attempts=2,
                sleep=lambda _: None,
            )
        self.assertEqual(len(primary.responses.calls), 1)
        self.assertEqual(len(backup.responses.calls), 1)
        self.assertEqual(legacy.responses.calls, [])

    def test_permanent_errors_do_not_fail_over(self):
        deep_think = load_module()
        for status, code in [
            (400, "invalid_request"),
            (401, "unauthorized"),
            (403, "forbidden"),
            (404, "not_found"),
        ]:
            with self.subTest(status=status):
                router, primary, backup, legacy = self.make_router(
                    deep_think,
                    [
                        make_status_error(
                            status, code, headers={"x-should-retry": "true"}
                        )
                    ],
                )
                with self.assertRaisesRegex(
                    deep_think.DeepThinkError, "failed without retry"
                ):
                    deep_think.request_response(
                        router,
                        deep_think.build_response_request([], "gpt-6-astra"),
                        purpose="answer",
                        sleep=lambda _: None,
                    )
                self.assertEqual(len(primary.responses.calls), 1)
                self.assertEqual(backup.responses.calls + legacy.responses.calls, [])


class RetryTests(unittest.TestCase):
    def test_background_response_is_polled_without_resubmitting(self):
        deep_think = load_module()
        queued = FakeResponse(
            "resp-background",
            "",
            status="queued",
            incomplete_reason=None,
        )
        in_progress = FakeResponse(
            "resp-background",
            "",
            status="in_progress",
            incomplete_reason=None,
        )
        complete = FakeResponse("resp-background", "Complete proof.")
        client = FakeClient([queued], [in_progress, complete])
        sleeps = []

        outcome = deep_think.request_response(
            client,
            {
                "model": "gpt-5.6-sol",
                "input": "Prove it.",
                "background": True,
            },
            purpose="answer",
            poll_interval=0,
            sleep=sleeps.append,
        )

        self.assertIs(outcome.response, complete)
        self.assertEqual(outcome.retry_count, 0)
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(
            client.responses.retrieve_calls,
            ["resp-background", "resp-background"],
        )
        self.assertEqual(sleeps, [0, 0])

    def test_transient_poll_error_retries_retrieve_without_resubmitting(self):
        deep_think = load_module()
        queued = FakeResponse(
            "resp-background",
            "",
            status="queued",
            incomplete_reason=None,
        )
        transient = make_status_error(500, "server_error")
        complete = FakeResponse("resp-background", "Complete proof.")
        client = FakeClient([queued], [transient, complete])
        events = []

        outcome = deep_think.request_response(
            client,
            {
                "model": "gpt-5.6-sol",
                "input": "Prove it.",
                "background": True,
            },
            purpose="answer",
            max_attempts=2,
            base_delay=0,
            poll_interval=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
            on_retry=events.append,
        )

        self.assertIs(outcome.response, complete)
        self.assertEqual(outcome.retry_count, 1)
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(
            client.responses.retrieve_calls,
            ["resp-background", "resp-background"],
        )
        self.assertIn("HTTP 500", events[0].reason)

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
        self.assertIn("empty response; switching deployment", events[0].reason)

    def test_transient_azure_error_retries_using_retry_after(self):
        deep_think = load_module()
        throttled = make_status_error(
            429,
            "no_capacity",
            headers={
                "retry-after": "2.5",
                "x-should-retry": "false",
            },
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
        self.assertEqual(
            [call["model"] for call in client.responses.calls],
            ["gpt-5.6-sol", "gpt-5.6-sol-nofilters"],
        )
        self.assertEqual(outcome.deployment, "gpt-5.6-sol-nofilters")
        self.assertEqual(sleeps, [2.5])
        self.assertIn("HTTP 429", events[0].reason)
        self.assertIn("no_capacity", events[0].reason)
        self.assertIn(
            "switching deployment to gpt-5.6-sol-nofilters",
            events[0].reason,
        )

    def test_all_fallback_429s_switch_back_to_primary_deployment(self):
        deep_think = load_module()
        client = FakeClient(
            [
                make_status_error(429, "rate_limit_exceeded"),
                make_status_error(429, "rate_limit_exceeded"),
                make_status_error(429, "rate_limit_exceeded"),
                FakeResponse("resp-complete", "Complete proof."),
            ]
        )

        outcome = deep_think.request_response(
            client,
            {"model": "gpt-5.6-sol", "input": "Prove it."},
            purpose="answer",
            max_attempts=4,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
        )

        self.assertEqual(
            [call["model"] for call in client.responses.calls],
            [
                "gpt-5.6-sol",
                "gpt-5.6-sol-nofilters",
                "gpt-5.4-pro",
                "gpt-5.6-sol",
            ],
        )
        self.assertEqual(outcome.deployment, "gpt-5.6-sol")
        self.assertEqual(outcome.retry_count, 3)

    def test_5_4_fallback_uses_xhigh_without_reasoning_mode(self):
        deep_think = load_module()
        client = FakeClient(
            [
                make_status_error(429, "rate_limit_exceeded"),
                make_status_error(429, "rate_limit_exceeded"),
                FakeResponse("resp-complete", "Complete proof."),
            ]
        )

        outcome = deep_think.request_response(
            client,
            {
                "model": "gpt-5.6-sol",
                "input": "Prove it.",
                "reasoning": {
                    "mode": "pro",
                    "effort": "max",
                    "context": "all_turns",
                    "summary": "auto",
                },
            },
            purpose="answer",
            max_attempts=3,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
        )

        self.assertEqual(outcome.deployment, "gpt-5.4-pro")
        self.assertEqual(
            client.responses.calls[2]["reasoning"],
            {
                "effort": "xhigh",
                "summary": "auto",
            },
        )

    def test_poll_429_does_not_resubmit_on_fallback_deployment(self):
        deep_think = load_module()
        queued = FakeResponse(
            "resp-background",
            "",
            status="queued",
            incomplete_reason=None,
        )
        throttled = make_status_error(
            429,
            "rate_limit_exceeded",
            headers={"x-should-retry": "false"},
        )
        complete = FakeResponse("resp-background", "Complete proof.")
        client = FakeClient([queued], [throttled, complete])

        outcome = deep_think.request_response(
            client,
            {
                "model": "gpt-5.6-sol",
                "input": "Prove it.",
                "background": True,
            },
            purpose="answer",
            max_attempts=2,
            base_delay=0,
            poll_interval=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
        )

        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(client.responses.calls[0]["model"], "gpt-5.6-sol")
        self.assertEqual(
            client.responses.retrieve_calls,
            ["resp-background", "resp-background"],
        )
        self.assertEqual(outcome.deployment, "gpt-5.6-sol")
        self.assertEqual(outcome.retry_count, 1)

    def test_malformed_creation_without_id_is_not_resubmitted(self):
        deep_think = load_module()
        malformed = make_validation_error()
        complete = FakeResponse("resp-complete", "Must not be requested.")
        client = FakeClient([malformed, complete])

        with self.assertRaisesRegex(deep_think.SubmissionUnknownError, "unknown"):
            deep_think.request_response(
                client,
                {"model": "gpt-5.6-sol", "input": "Prove it."},
                purpose="answer",
                max_attempts=2,
                base_delay=0,
                sleep=lambda _delay: None,
                random_value=lambda: 0,
            )

        self.assertEqual(len(client.responses.calls), 1)

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
        self.assertIn(
            "malformed Azure response; switching deployment", events[0].reason
        )

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
        self.assertIn(
            "malformed Azure response; switching deployment", events[0].reason
        )

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

    def test_5_4_fallback_records_actual_reasoning_profile(self):
        deep_think = load_module()
        client = FakeClient(
            [
                make_status_error(429, "rate_limit_exceeded"),
                make_status_error(429, "rate_limit_exceeded"),
                FakeResponse("resp-complete", "Complete proof."),
            ]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            deep_think.run_turn(
                client,
                root=root,
                project="fallback-transcript",
                title="Fallback Transcript",
                prompt="Prove the theorem.",
                deployment="gpt-5.6-sol",
                max_attempts=3,
                retry_base_delay=0,
                sleep=lambda _delay: None,
                random_value=lambda: 0,
            )
            transcript = (
                root / "fallback-transcript" / "0001-transcript.md"
            ).read_text("utf-8")

        self.assertIn("Deployment: `gpt-5.4-pro`", transcript)
        self.assertIn("Reasoning mode: `not configurable`", transcript)
        self.assertIn("Reasoning effort: `xhigh`", transcript)
        self.assertIn('primary-deployment: "gpt-5.6-sol"', transcript)
        self.assertIn("primary-reasoning-mode: pro", transcript)
        self.assertIn("primary-reasoning-effort: max", transcript)
        self.assertNotIn("\nmodel:", transcript)

    def test_5_4_primary_records_its_effective_profile_in_frontmatter(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-complete", "Complete proof.")])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            deep_think.run_turn(
                client,
                root=root,
                project="gpt54-primary-transcript",
                title="GPT-5.4 Primary Transcript",
                prompt="Prove the theorem.",
                deployment="gpt-5.4-pro",
            )
            transcript = (
                root / "gpt54-primary-transcript" / "0001-transcript.md"
            ).read_text("utf-8")

        self.assertIn('primary-deployment: "gpt-5.4-pro"', transcript)
        self.assertIn("primary-reasoning-mode: not configurable", transcript)
        self.assertIn("primary-reasoning-effort: xhigh", transcript)

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
    def test_explicit_legacy_deployment_uses_legacy_resource(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-old", "Complete.")])
        factory = mock.Mock(return_value=client)
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "AZURE_OPENAI_GPT6_ENDPOINT": "primary",
                    "AZURE_OPENAI_GPT6_BACKUP_ENDPOINT": "backup",
                    "AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT": "gpt-6-astra-backup",
                    "AZURE_OPENAI_ENDPOINT": "legacy",
                },
                clear=True,
            ),
            tempfile.TemporaryDirectory() as root,
        ):
            exit_code = deep_think.main(
                [
                    "ask",
                    "--project",
                    "legacy",
                    "--prompt",
                    "Continue.",
                    "--deployment",
                    "gpt-5.6-sol",
                    "--root",
                    root,
                ],
                client_factory=factory,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(exit_code, 0)
        factory.assert_called_once_with("legacy")

    def test_cli_routes_new_model_endpoints_and_retains_legacy_resource(self):
        deep_think = load_module()
        clients = {
            "primary": FakeClient([make_status_error(429, "no_capacity")]),
            "backup": FakeClient([make_status_error(500, "server_error")]),
            "legacy": FakeClient([FakeResponse("resp-legacy", "Recovered.")]),
        }
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "AZURE_OPENAI_GPT6_ENDPOINT": "primary",
                    "AZURE_OPENAI_GPT6_BACKUP_ENDPOINT": "backup",
                    "AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT": "gpt-6-astra-backup",
                    "AZURE_OPENAI_ENDPOINT": "legacy",
                },
                clear=True,
            ),
            tempfile.TemporaryDirectory() as root,
        ):
            exit_code = deep_think.main(
                [
                    "ask",
                    "--project",
                    "cli-routing",
                    "--prompt",
                    "Continue.",
                    "--root",
                    root,
                    "--retry-base-delay",
                    "0",
                ],
                client_factory=clients.__getitem__,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(clients["primary"].responses.calls[0]["model"], "gpt-6-astra")
        self.assertEqual(
            clients["backup"].responses.calls[0]["model"], "gpt-6-astra-backup"
        )
        self.assertEqual(clients["legacy"].responses.calls[0]["model"], "gpt-5.6-sol")

    def test_existing_5_6_project_upgrades_without_losing_history(self):
        deep_think = load_module()
        client = FakeClient(
            [
                FakeResponse("resp-old", "Old result."),
                FakeResponse("resp-new", "New result."),
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            deep_think.run_turn(
                client,
                root=root,
                project="upgrade",
                prompt="Old question.",
                deployment="gpt-5.6-sol",
            )
            deep_think.run_turn(
                client,
                root=root,
                project="upgrade",
                prompt="Continue.",
                deployment="gpt-6-astra",
            )
            state = json.loads((Path(root) / "upgrade" / "state.json").read_text())
            transcript = (Path(root) / "upgrade" / "0001-transcript.md").read_text(
                encoding="utf-8"
            )
        self.assertEqual(state["deployment"], "gpt-6-astra")
        self.assertEqual(state["turn"], 2)
        self.assertEqual(
            client.responses.calls[1]["input"][0]["content"], "Old question."
        )
        self.assertIn("Old result.", transcript)
        self.assertIn("gpt-6-astra", transcript)

    def test_cli_reconfigures_cp1252_stdout_to_utf8(self):
        deep_think = load_module()
        client = FakeClient([FakeResponse("resp-cli", "Proof complete. ∎")])
        raw_stdout = io.BytesIO()
        stdout = io.TextIOWrapper(raw_stdout, encoding="cp1252")
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as temporary_directory:
            exit_code = deep_think.main(
                [
                    "ask",
                    "--project",
                    "cli-unicode",
                    "--title",
                    "CLI Unicode",
                    "--prompt",
                    "Prove it.",
                    "--root",
                    str(Path(temporary_directory) / "transcripts"),
                ],
                client_factory=lambda _endpoint: client,
                stdout=stdout,
                stderr=stderr,
            )
            stdout.flush()
            output = raw_stdout.getvalue().decode("utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.splitlines(), ["Proof complete. ∎"])

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
            "Retrying answer after empty response; switching deployment",
            stderr.getvalue(),
        )
        self.assertIn("(attempt 1/2", stderr.getvalue())

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
