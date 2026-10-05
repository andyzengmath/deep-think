import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_deep_think import FakeClient, FakeResponse, load_module, make_status_error
from test_journal_recovery import ProjectFixture, journal_records

AZURE_PREVIEW = (
    "https://example-resource.openai.azure.com/openai/responses"
    "?api-version=2025-04-01-preview"
)
AZURE_V1 = "https://contoso-east.openai.azure.com/openai/v1/"
OPENAI_V1 = "https://api.openai.com/v1/"
PROXY_V1 = "https://llm-proxy.example/v1/"


def completed_body(text="Key answer."):
    return {
        "id": "resp-key",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-astra",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg-1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 15,
        },
    }


def recording_sdk(requests, body):
    import httpx
    import openai

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    def factory(**options):
        transport = httpx.MockTransport(handle)
        return openai.OpenAI(**options, http_client=httpx.Client(transport=transport))

    return factory


def no_entra(*_args, **_kwargs):
    raise AssertionError("Entra credentials must not be used for key authentication")


class KeyAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_module()

    def test_azure_key_sends_only_the_api_key_header(self):
        requests = []
        client = self.runner.create_client(
            AZURE_PREVIEW,
            auth="azure-key",
            api_key="azure-test-key",
            credential_factory=no_entra,
            token_provider_factory=no_entra,
            openai_factory=recording_sdk(requests, completed_body()),
        )
        client.responses.create(model="gpt-6-astra", input="Hello.")
        client.responses.retrieve("resp-key")
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.headers["api-key"], "azure-test-key")
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(request.url.host, "example-resource.openai.azure.com")
            self.assertEqual(request.url.params["api-version"], "2025-04-01-preview")

    def test_openai_key_uses_bearer_auth_on_the_default_base_url(self):
        requests = []
        client = self.runner.create_client(
            None,
            auth="openai-key",
            api_key="sk-test-key",
            credential_factory=no_entra,
            token_provider_factory=no_entra,
            openai_factory=recording_sdk(requests, completed_body()),
        )
        client.responses.create(model="gpt-6-astra", input="Hello.")
        [request] = requests
        self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
        self.assertEqual(request.headers["authorization"], "Bearer sk-test-key")
        self.assertNotIn("api-key", request.headers)

    def test_missing_key_or_unknown_auth_fails_without_echoing_secrets(self):
        for auth, endpoint in (("azure-key", AZURE_PREVIEW), ("openai-key", None)):
            with (
                self.subTest(auth=auth),
                self.assertRaisesRegex(self.runner.DeepThinkError, "API key"),
            ):
                self.runner.create_client(
                    endpoint, auth=auth, api_key=" ", openai_factory=lambda **_: 0
                )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "auth") as raised:
            self.runner.create_client(
                AZURE_PREVIEW,
                auth="password",
                api_key="secret-value",
                openai_factory=lambda **_: 0,
            )
        self.assertNotIn("secret-value", str(raised.exception))

    def test_openai_endpoint_rejects_insecure_or_credentialed_urls(self):
        for endpoint in (
            "http://api.openai.com/v1/",
            "https://user:pw@api.openai.com/v1/",
            "https://api.openai.com/v1/?api-version=2025-04-01-preview",
        ):
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaises(self.runner.DeepThinkError),
            ):
                self.runner.create_client(
                    endpoint,
                    auth="openai-key",
                    api_key="sk-test",
                    openai_factory=lambda **_: 0,
                )

    def test_keys_never_reach_project_files(self):
        requests = []
        client = self.runner.create_client(
            None,
            auth="openai-key",
            api_key="sk-never-persisted",
            openai_factory=recording_sdk(requests, completed_body("Persisted.")),
        )
        with tempfile.TemporaryDirectory() as root:
            result = self.runner.run_turn(
                client,
                root=root,
                project="keys",
                prompt="Question.",
                deployment="gpt-6-astra",
                sleep=lambda _delay: None,
            )
            self.assertEqual(result.text, "Persisted.")
            files = [path for path in Path(root).rglob("*") if path.is_file()]
            self.assertTrue(files)
            for path in files:
                self.assertNotIn(b"sk-never-persisted", path.read_bytes(), path)


class MethodSelectionTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_module()

    def args(self, *extra):
        return self.runner._build_parser().parse_args(
            ["ask", "--project", "p", "--prompt", "Q.", *extra]
        )

    def test_auth_methods_are_detected_or_listed_in_priority_order(self):
        azure = {"AZURE_OPENAI_GPT6_ENDPOINT": AZURE_PREVIEW}
        cases = [
            ({}, [], ("entra",)),
            (azure, [], ("entra",)),
            (
                {**azure, "AZURE_OPENAI_API_KEY": "k"},
                [],
                ("azure-key", "entra"),
            ),
            (
                {**azure, "AZURE_OPENAI_GPT6_API_KEY": "k"},
                [],
                ("azure-key", "entra"),
            ),
            (
                {**azure, "AZURE_OPENAI_API_KEY": "k"},
                ["--auth", "entra"],
                ("entra",),
            ),
            ({"OPENAI_API_KEY": "sk"}, [], ("openai-key",)),
            ({"OPENAI_API_KEY": "sk", **azure}, [], ("entra",)),
            (
                {"OPENAI_API_KEY": "sk", **azure},
                ["--auth", "openai-key"],
                ("openai-key",),
            ),
            ({"DEEP_THINK_AUTH": " Entra, OpenAI-Key "}, [], ("entra", "openai-key")),
            (
                {"DEEP_THINK_AUTH": "openai-key"},
                ["--auth", "entra,azure-key"],
                ("entra", "azure-key"),
            ),
        ]
        for environment, extra, expected in cases:
            with (
                self.subTest(environment=environment, extra=extra),
                mock.patch.dict("os.environ", environment, clear=True),
            ):
                self.assertEqual(
                    self.runner._resolve_methods(self.args(*extra)), expected
                )

    def test_invalid_auth_settings_are_rejected(self):
        for value in ("gemini", "entra,entra", "entra,,openai-key", "key"):
            with (
                self.subTest(value=value),
                mock.patch.dict("os.environ", {"DEEP_THINK_AUTH": value}, clear=True),
                self.assertRaisesRegex(self.runner.DeepThinkError, "azure-key"),
            ):
                self.runner._resolve_methods(self.args())

    def test_openai_uses_its_own_model_chain_on_one_resource(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk"}, clear=True):
            router = self.runner._router_from_args(
                self.args("--auth", "openai-key"), "gpt-6-astra", lambda _: object()
            )
        self.assertEqual(
            [target.deployment for target in router.targets],
            ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.4-pro"],
        )
        self.assertEqual(
            {target.resource for target in router.targets},
            {"https://api.openai.com/v1/"},
        )

    def test_azure_keys_are_selected_per_resource(self):
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": "https://east.openai.azure.com/openai/v1/",
            "AZURE_OPENAI_GPT6_API_KEY": "east-key",
            "AZURE_OPENAI_GPT6_BACKUP_ENDPOINT": "https://south.openai.azure.com/openai/v1/",
            "AZURE_OPENAI_ENDPOINT": "https://legacy.openai.azure.com/openai/v1/",
            "AZURE_OPENAI_API_KEY": "shared-key",
        }
        calls = []

        def record(resource, **options):
            calls.append(options)
            return object()

        with mock.patch.dict("os.environ", environment, clear=True):
            factory = self.runner._configured_client_factories(("azure-key",))["azure"]
            with mock.patch.object(self.runner, "create_client", record):
                factory("https://east.openai.azure.com/openai/v1/")
                factory("https://south.openai.azure.com/openai/v1/")
                factory("https://legacy.openai.azure.com/openai/v1")
        self.assertEqual(
            [options["api_key"] for options in calls],
            ["east-key", "shared-key", "shared-key"],
        )
        self.assertEqual({options["auth"] for options in calls}, {"azure-key"})

    def test_cli_trusts_the_configured_openai_base_url(self):
        seen = []
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk"}, clear=True):
            factory = self.runner._trusted_client_factory(
                seen.append, self.args("--auth", "openai-key"), "openai"
            )
            factory("https://api.openai.com/v1/")
            with self.assertRaises(self.runner.DeepThinkError):
                factory("https://other.example/v1/")
        self.assertEqual(seen, ["https://api.openai.com/v1/"])


class BackupMethodTests(unittest.TestCase):
    """Later --auth methods back up earlier ones, like the model chain."""

    def setUp(self):
        self.runner = load_module()

    def args(self, *extra):
        return self.runner._build_parser().parse_args(
            ["ask", "--project", "p", "--prompt", "Q.", *extra]
        )

    def test_entra_falls_back_to_the_azure_key_when_sign_in_fails(self):
        from azure.core.exceptions import ClientAuthenticationError

        entra = FakeClient([ClientAuthenticationError("no token")])
        key = FakeClient([FakeResponse("resp-key", "Key answer.")])
        switches = []
        client = self.runner.AuthFallbackClient(
            [("entra", entra), ("azure-key", key)],
            on_switch=lambda previous, current, _error: switches.append(
                (previous, current)
            ),
        )
        self.assertEqual(client.responses.create(model="m", input="Q.").id, "resp-key")
        self.assertEqual(switches, [("entra", "azure-key")])
        key.responses.queued.append(FakeResponse("resp-again", "Again."))
        client.responses.create(model="m", input="Q.")
        self.assertEqual(len(entra.responses.calls), 1)
        self.assertEqual(len(key.responses.calls), 2)

    def test_only_sign_in_failures_switch_methods(self):
        from azure.core.exceptions import ClientAuthenticationError

        for error, switches in (
            (make_status_error(401, "unauthorized"), True),
            (make_status_error(403, "PermissionDenied"), True),
            (make_status_error(429, "rate_limit_exceeded"), False),
        ):
            with self.subTest(status=error.status_code):
                first = FakeClient([error])
                second = FakeClient([FakeResponse("resp-ok", "OK.")])
                client = self.runner.AuthFallbackClient(
                    [("entra", first), ("azure-key", second)]
                )
                if switches:
                    self.assertEqual(client.responses.create(model="m").id, "resp-ok")
                else:
                    with self.assertRaises(type(error)):
                        client.responses.create(model="m")
                    self.assertEqual(second.responses.calls, [])
        last = self.runner.AuthFallbackClient(
            [("entra", FakeClient([ClientAuthenticationError("no token")]))]
        )
        with self.assertRaises(ClientAuthenticationError):
            last.responses.create(model="m")

    def test_configured_factories_follow_the_listed_order(self):
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "AZURE_OPENAI_API_KEY": "azure-key-value",
            "OPENAI_API_KEY": "sk-value",
        }
        calls = []

        def record(resource, **options):
            calls.append((resource, options.get("auth", "entra")))
            return object()

        with (
            mock.patch.dict("os.environ", environment, clear=True),
            mock.patch.object(self.runner, "create_client", record),
        ):
            factories = self.runner._configured_client_factories(
                ("entra", "openai-key", "azure-key")
            )
            self.assertEqual(list(factories), ["azure", "openai"])
            azure = factories["azure"](AZURE_V1)
            factories["openai"](OPENAI_V1)
        self.assertIsInstance(azure, self.runner.AuthFallbackClient)
        self.assertEqual(
            calls,
            [(AZURE_V1, "entra"), (AZURE_V1, "azure-key"), (OPENAI_V1, "openai-key")],
        )

    def test_backup_provider_chain_follows_the_primary_chain(self):
        legacy = "https://legacy.openai.azure.com/openai/v1/"
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "AZURE_OPENAI_ENDPOINT": legacy,
            "OPENAI_API_KEY": "sk",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            router = self.runner._router_from_args(
                self.args("--auth", "entra,openai-key"),
                "gpt-6-astra",
                lambda _resource: object(),
            )
        self.assertEqual(router.providers, ("azure", "openai"))
        self.assertEqual(
            [(t.provider, t.deployment, t.resource) for t in router.targets],
            [
                ("azure", "gpt-6-astra", AZURE_V1),
                ("azure", "gpt-5.6-sol", legacy),
                ("azure", "gpt-5.4-pro", legacy),
                ("openai", "gpt-6-astra", OPENAI_V1),
                ("openai", "gpt-5.6-sol", OPENAI_V1),
                ("openai", "gpt-5.4-pro", OPENAI_V1),
            ],
        )

    def test_sign_in_failure_moves_to_the_backup_provider(self):
        from azure.core.exceptions import ClientAuthenticationError

        for failure in (
            ClientAuthenticationError("no token"),
            make_status_error(401, "unauthorized"),
        ):
            with self.subTest(failure=type(failure).__name__):
                azure = FakeClient([failure])
                backup = FakeClient([FakeResponse("resp-openai", "Backup answer.")])
                target = self.runner.RouteTarget
                router = self.runner.DeploymentRouter(
                    [
                        target("gpt-6-astra", azure, AZURE_V1, "primary", "azure"),
                        target("gpt-5.4-pro", azure, AZURE_V1, "fallback", "azure"),
                        target("gpt-6-astra", backup, OPENAI_V1, "primary", "openai"),
                    ]
                )
                events = []
                outcome = self.runner.request_response(
                    router,
                    {"model": "gpt-6-astra", "input": "Q."},
                    purpose="answer",
                    sleep=lambda _delay: None,
                    on_retry=events.append,
                )
                self.assertEqual(outcome.response.id, "resp-openai")
                self.assertEqual(len(azure.responses.calls), 1)
                self.assertIn("openai", events[0].reason)

    def test_cli_attempts_default_to_one_per_target(self):
        self.assertIsNone(self.args().max_attempts)
        throttled = [make_status_error(429, "rate_limit_exceeded")] * 6
        client = FakeClient([*throttled, FakeResponse("resp-7", "Seventh.")])
        router = self.runner.DeploymentRouter(
            [self.runner.RouteTarget(f"model-{i}", client, "A") for i in range(7)]
        )
        outcome = self.runner.request_response(
            router,
            {"model": "model-0", "input": "Q."},
            purpose="answer",
            max_attempts=None,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
        )
        self.assertEqual(outcome.response.id, "resp-7")


class FallbackChainTests(unittest.TestCase):
    """Model backup plans are universal by default and configurable locally."""

    def setUp(self):
        self.runner = load_module()

    def chain(self, environment, *extra, deployment="gpt-6-astra"):
        with mock.patch.dict("os.environ", environment, clear=True):
            args = self.runner._build_parser().parse_args(
                ["ask", "--project", "p", "--prompt", "Q.", *extra]
            )
            router = self.runner._router_from_args(
                args, deployment, lambda _resource: object()
            )
        return [(target.deployment, target.resource) for target in router.targets]

    def test_azure_and_openai_default_to_the_same_universal_chain(self):
        azure = {"AZURE_OPENAI_GPT6_ENDPOINT": "A", "AZURE_OPENAI_ENDPOINT": "L"}
        self.assertEqual(
            self.chain(azure),
            [("gpt-6-astra", "A"), ("gpt-5.6-sol", "L"), ("gpt-5.4-pro", "L")],
        )
        self.assertEqual(
            self.chain({"OPENAI_API_KEY": "sk"}),
            [
                ("gpt-6-astra", OPENAI_V1),
                ("gpt-5.6-sol", OPENAI_V1),
                ("gpt-5.4-pro", OPENAI_V1),
            ],
        )

    def test_fallback_lists_are_configurable(self):
        azure = {
            "AZURE_OPENAI_GPT6_ENDPOINT": "A",
            "AZURE_OPENAI_FALLBACK_ENDPOINT": "L",
            "AZURE_OPENAI_FALLBACK_DEPLOYMENTS": " gpt-5.6-sol, gpt-5.6-sol-alt ,gpt-5.4-pro",
        }
        self.assertEqual(
            [name for name, _ in self.chain(azure)],
            ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-sol-alt", "gpt-5.4-pro"],
        )
        self.assertEqual(
            self.chain(azure, deployment="gpt-5.6-sol-alt"),
            [("gpt-5.6-sol-alt", "L"), ("gpt-5.4-pro", "L")],
        )
        self.assertEqual(
            self.chain({**azure, "AZURE_OPENAI_FALLBACK_DEPLOYMENTS": "none"}),
            [("gpt-6-astra", "A")],
        )
        self.assertEqual(
            [
                name
                for name, _ in self.chain(
                    {"OPENAI_API_KEY": "sk", "OPENAI_FALLBACK_MODELS": "gpt-5.4-pro"}
                )
            ],
            ["gpt-6-astra", "gpt-5.4-pro"],
        )

    def test_invalid_fallback_lists_are_rejected(self):
        for value in ("gpt-5.6-sol,,gpt-5.4-pro", "a,a", "bad name"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    self.runner.DeepThinkError, "AZURE_OPENAI_FALLBACK_DEPLOYMENTS"
                ),
            ):
                self.chain(
                    {
                        "AZURE_OPENAI_GPT6_ENDPOINT": "A",
                        "AZURE_OPENAI_FALLBACK_DEPLOYMENTS": value,
                    }
                )


class CrossProviderEndpointTests(unittest.TestCase):
    """Credentials must never reach the other provider's servers."""

    def setUp(self):
        self.runner = load_module()

    def parse(self, command, *extra):
        return self.runner._build_parser().parse_args(
            [command, "--project", "p", *extra]
        )

    def test_explicit_endpoints_select_azure_unless_openai_is_requested(self):
        built = []

        def record(_resource, **options):
            built.append(options)

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-leak"}, clear=True):
            for argv in (
                ("ask", "--endpoint", AZURE_V1),
                ("ask", "--backup-endpoint", AZURE_V1),
                ("ask", "--fallback-endpoint", AZURE_V1),
                ("resume", "--endpoint", AZURE_V1),
                ("cancel", "--endpoint", AZURE_V1),
                ("reconcile", "--endpoint", AZURE_V1),
            ):
                with self.subTest(argv=argv):
                    args = self.parse(*argv)
                    methods = self.runner._resolve_methods(args)
                    self.assertEqual(methods, ("entra",))
                    built.clear()
                    with mock.patch.object(self.runner, "create_client", record):
                        self.runner._configured_client_factories(methods)["azure"](
                            AZURE_V1
                        )
                    self.assertEqual(built, [{}])
            args = self.parse("ask", "--auth", "openai-key", "--endpoint", PROXY_V1)
            self.assertEqual(self.runner._resolve_methods(args), ("openai-key",))

    def test_clients_refuse_the_other_providers_hosts(self):
        built = []

        def sdk(**options):
            built.append(options)

        for endpoint in (
            AZURE_V1,
            "https://contoso.cognitiveservices.azure.com/openai/v1/",
            "https://contoso.services.ai.azure.com/openai/v1/",
            "https://contoso.openai.azure.us/openai/v1/",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(
                    self.runner.DeepThinkError, "--auth entra"
                ) as raised:
                    self.runner.create_client(
                        endpoint,
                        auth="openai-key",
                        api_key="sk-leak",
                        openai_factory=sdk,
                    )
                self.assertNotIn("sk-leak", str(raised.exception))
        for auth in ("entra", "azure-key"):
            for endpoint in (OPENAI_V1, "https://API.OpenAI.com./v1/"):
                with (
                    self.subTest(auth=auth, endpoint=endpoint),
                    self.assertRaisesRegex(
                        self.runner.DeepThinkError, "--auth openai-key"
                    ),
                ):
                    self.runner.create_client(
                        endpoint,
                        auth=auth,
                        api_key="azure-leak",
                        credential_factory=no_entra,
                        token_provider_factory=no_entra,
                        openai_factory=sdk,
                    )
        self.assertEqual(built, [])

    def test_trusted_endpoints_belong_to_the_selected_provider(self):
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "OPENAI_API_KEY": "sk",
            "OPENAI_BASE_URL": PROXY_V1,
        }
        seen = []
        with mock.patch.dict("os.environ", environment, clear=True):
            openai_trusted = self.runner._trusted_client_factory(
                seen.append, self.parse("cancel", "--auth", "openai-key"), "openai"
            )
            openai_trusted(PROXY_V1)
            with self.assertRaisesRegex(self.runner.DeepThinkError, "--auth entra"):
                openai_trusted(AZURE_V1)
            azure_trusted = self.runner._trusted_client_factory(
                seen.append, self.parse("cancel"), "azure"
            )
            azure_trusted(AZURE_V1)
            with self.assertRaisesRegex(
                self.runner.DeepThinkError, "--auth openai-key"
            ):
                azure_trusted(PROXY_V1)
            explicit = self.runner._trusted_client_factory(
                seen.append, self.parse("cancel", "--endpoint", OPENAI_V1), "azure"
            )
            with self.assertRaisesRegex(
                self.runner.DeepThinkError, "--auth openai-key"
            ):
                explicit(OPENAI_V1)
        self.assertEqual(seen, [PROXY_V1, AZURE_V1])


class LoggingClient:
    """Fake SDK client that logs each remote call with its destination."""

    def __init__(self, resource, log):
        def call(operation, response_id, status):
            log.append((resource, operation, response_id))
            return FakeResponse(response_id, "", status=status, incomplete_reason=None)

        self.responses = SimpleNamespace(
            retrieve=lambda response_id, **_options: call(
                "retrieve", response_id, "in_progress"
            ),
            cancel=lambda response_id: call("cancel", response_id, "cancelled"),
        )


class CrossProviderRecoveryTests(ProjectFixture):
    """Journaled jobs are recovered only through the provider that created them."""

    def start_job(self, provider, resource):
        router = self.runner.create_routed_client(
            resource,
            "gpt-6-astra",
            provider=provider,
            client_factory=lambda _resource: FakeClient(
                [FakeResponse("resp-job", "", status="queued", incomplete_reason=None)],
                [make_status_error(401, "unauthorized")],
            ),
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "resp-job"):
            self.ask(router)

    def cli(self, *arguments, log):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = self.runner.main(
            [arguments[0], "--project", self.project, "--root", str(self.root)]
            + list(arguments[1:]),
            client_factory=lambda resource: LoggingClient(resource, log),
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_refused(self, arguments, log, method):
        flag = f"--auth {method}"
        code, stdout, stderr = self.cli(*arguments, log=log)
        if arguments[0] == "resume":
            self.assertEqual(code, 2, stdout)
            self.assertIn(flag, stderr)
        elif arguments[0] == "cancel":
            self.assertEqual(code, 1, stderr)
            [result] = json.loads(stdout)["results"]
            self.assertIn(flag, result["error"])
        else:
            self.assertEqual(code, 1, stderr)
            self.assertTrue(
                any(flag in action for action in json.loads(stdout)["actions"]),
                stdout,
            )

    def forge_openai_job(self):
        """Add an accepted OpenAI job to the open turn, as a backup would."""
        [*_, azure] = [
            record
            for record in journal_records(self.project_dir)
            if record["event"] == "submitting"
        ]
        common = {
            "schema": "deep-think-request-journal/v1",
            "recorded_at": "2026-10-04T00:00:00+00:00",
            "lock_id": None,
            "turn_id": azure["turn_id"],
            "attempt_id": "openai-attempt",
        }
        submitting = {
            **{key: azure[key] for key in ("purpose", "logical_sha256")},
            "event": "submitting",
            "attempt": 2,
            "deployment": "gpt-6-astra",
            "resource": PROXY_V1,
            "role": "primary",
            "provider": "openai",
        }
        accepted = {
            "event": "accepted",
            "response_id": "resp-openai",
            "status": "queued",
        }
        path = self.project_dir / "requests" / "journal.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            for record in (submitting, accepted):
                stream.write(json.dumps({**common, **record}) + "\n")

    def test_jobs_record_their_provider_for_status(self):
        self.seed()
        self.start_job("openai", PROXY_V1)
        self.assertEqual(
            [
                record["provider"]
                for record in journal_records(self.project_dir)
                if record["event"] == "submitting"
            ],
            ["azure", "openai"],
        )
        status = self.status()
        [job] = status["outstanding"]
        self.assertEqual((job["provider"], job["resource"]), ("openai", PROXY_V1))
        steps = " ".join(status["next_steps"])
        for command in ("resume", "cancel", "reconcile"):
            self.assertIn(f"{command} --project recovery --auth openai-key", steps)

    def test_legacy_attempts_without_a_provider_are_azure(self):
        view = self.runner.JournalView([{"event": "submitting", "attempt_id": "a"}])
        summary = self.runner._attempt_summary(view.attempts["a"], "active")
        self.assertEqual(summary["provider"], "azure")

    def test_azure_recovery_never_touches_openai_jobs(self):
        self.seed()
        self.start_job("openai", PROXY_V1)
        log = []
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "OPENAI_API_KEY": "sk",
            "OPENAI_BASE_URL": PROXY_V1,
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            for arguments in (
                ("cancel",),
                ("cancel", "--response-id", "resp-job"),
                ("cancel", "--endpoint", PROXY_V1),
                ("reconcile",),
                ("resume",),
                ("resume", "--endpoint", PROXY_V1),
            ):
                with self.subTest(arguments=arguments):
                    self.assert_refused(arguments, log, "openai-key")
            self.assertEqual(log, [])
            code, _stdout, stderr = self.cli("cancel", "--auth", "openai-key", log=log)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(log, [(PROXY_V1, "cancel", "resp-job")])

    def test_openai_recovery_never_touches_azure_jobs(self):
        self.seed()
        self.start_job("azure", AZURE_V1)
        log = []
        environment = {
            "DEEP_THINK_AUTH": "openai-key",
            "OPENAI_API_KEY": "sk",
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            for arguments in (("cancel",), ("reconcile",), ("resume",)):
                with self.subTest(arguments=arguments):
                    self.assert_refused(arguments, log, "entra")
            self.assertEqual(log, [])
            code, _stdout, stderr = self.cli("cancel", "--auth", "entra", log=log)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(log, [(AZURE_V1, "cancel", "resp-job")])

    def test_listed_backup_methods_recover_jobs_on_both_providers(self):
        self.seed()
        self.start_job("azure", AZURE_V1)
        self.forge_openai_job()
        log = []
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "OPENAI_API_KEY": "sk",
            "OPENAI_BASE_URL": PROXY_V1,
            "DEEP_THINK_AUTH": "entra,openai-key",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            code, stdout, stderr = self.cli("cancel", log=log)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            sorted(log),
            [
                (AZURE_V1, "cancel", "resp-job"),
                (PROXY_V1, "cancel", "resp-openai"),
            ],
        )
        self.assertEqual(
            {result["status"] for result in json.loads(stdout)["results"]},
            {"cancelled"},
        )


class NotFoundClient:
    """Fake SDK client whose jobs are invisible (HTTP 404) to this credential."""

    def __init__(self, resource, log):
        def call(operation, response_id):
            log.append((resource, operation, response_id))
            raise make_status_error(404, "not_found", message="No response found.")

        self.responses = SimpleNamespace(
            retrieve=lambda response_id, **_options: call("retrieve", response_id),
            cancel=lambda response_id: call("cancel", response_id),
        )


class ProviderProjectFixture(ProjectFixture):
    """Project fixture with CLI helpers for provider tests."""

    def cli(self, *arguments, factory=None, log=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = self.runner.main(
            [arguments[0], "--project", self.project, "--root", str(self.root)]
            + list(arguments[1:]),
            client_factory=factory or (lambda resource: LoggingClient(resource, log)),
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def start_job(self, provider, resource):
        router = self.runner.create_routed_client(
            resource,
            "gpt-6-astra",
            provider=provider,
            client_factory=lambda _resource: FakeClient(
                [FakeResponse("resp-job", "", status="queued", incomplete_reason=None)],
                [make_status_error(401, "unauthorized")],
            ),
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "resp-job"):
            self.ask(router)

    def parse(self, command, *extra):
        return self.runner._build_parser().parse_args(
            [command, "--project", "p", *extra]
        )


class ReviewRegressionTests(ProviderProjectFixture):
    """Regressions for issues found while reviewing the multi-provider change."""

    def test_endpoints_configured_for_the_other_provider_are_never_trusted(self):
        gateway = "https://gateway.example/openai/v1/"
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": gateway,
            "OPENAI_API_KEY": "sk",
            "OPENAI_BASE_URL": PROXY_V1,
        }
        seen = []
        with mock.patch.dict("os.environ", environment, clear=True):
            azure = self.runner._trusted_client_factory(
                seen.append, self.parse("cancel", "--endpoint", PROXY_V1), "azure"
            )
            with self.assertRaisesRegex(
                self.runner.DeepThinkError, "--auth openai-key"
            ):
                azure(PROXY_V1)
            openai_trusted = self.runner._trusted_client_factory(
                seen.append,
                self.parse("cancel", "--auth", "openai-key", "--endpoint", gateway),
                "openai",
            )
            with self.assertRaisesRegex(self.runner.DeepThinkError, "--auth entra"):
                openai_trusted(gateway)
        self.assertEqual(seen, [])

    def test_azure_endpoints_shared_with_openai_tools_stay_usable(self):
        # Some tools point OPENAI_BASE_URL at an Azure v1 endpoint.
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "OPENAI_BASE_URL": AZURE_V1,
        }
        seen = []
        with mock.patch.dict("os.environ", environment, clear=True):
            factory = self.runner._trusted_client_factory(
                seen.append, self.parse("cancel"), "azure"
            )
            factory(AZURE_V1)
        self.assertEqual(seen, [AZURE_V1])

    def test_endpoint_option_requires_a_single_provider(self):
        self.seed()
        log = []
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "OPENAI_API_KEY": "sk",
            "DEEP_THINK_AUTH": "entra,openai-key",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            code, _stdout, stderr = self.cli(
                "cancel", "--response-id", "resp-x", "--endpoint", PROXY_V1, log=log
            )
        self.assertEqual(code, 2)
        self.assertIn("--endpoint", stderr)
        self.assertIn("single provider", stderr)
        self.assertEqual(log, [])

    def test_openai_not_found_keeps_the_job_blocking_until_confirmed(self):
        self.seed()
        self.start_job("openai", PROXY_V1)
        log = []
        environment = {"OPENAI_API_KEY": "sk", "OPENAI_BASE_URL": PROXY_V1}
        with mock.patch.dict("os.environ", environment, clear=True):
            code, stdout, stderr = self.cli(
                "cancel",
                factory=lambda resource: NotFoundClient(resource, log),
            )
        self.assertEqual(code, 1, stderr)
        [result] = json.loads(stdout)["results"]
        self.assertIn("unknown", result["error"])
        status = self.status()
        self.assertFalse(status["can_submit_new_request"])
        self.assertEqual([item["state"] for item in status["outstanding"]], ["unknown"])
        with mock.patch.dict("os.environ", environment, clear=True):
            code, _stdout, stderr = self.cli(
                "reconcile",
                "--confirm-no-remote-job",
                "--reason",
                "Verified in the OpenAI dashboard.",
                log=log,
            )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(self.status()["can_submit_new_request"])

    def test_azure_not_found_still_means_the_job_is_gone(self):
        self.seed()
        self.start_job("azure", AZURE_V1)
        log = []
        with mock.patch.dict(
            "os.environ", {"AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1}, clear=True
        ):
            code, _stdout, stderr = self.cli(
                "cancel", factory=lambda resource: NotFoundClient(resource, log)
            )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(self.status()["can_submit_new_request"])

    def test_custom_fallback_projects_still_upgrade_to_gpt6(self):
        self.runner.run_turn(
            FakeClient([FakeResponse("resp-old", "Old answer.")]),
            root=self.root,
            project=self.project,
            prompt="First.",
            deployment="gpt-5.6-sol-alt",
            sleep=lambda _delay: None,
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "already"):
            self.ask(FakeClient([FakeResponse("resp-new", "New answer.")]))
        result = self.runner.run_turn(
            FakeClient([FakeResponse("resp-new", "New answer.")]),
            root=self.root,
            project=self.project,
            prompt="Second.",
            deployment="gpt-6-astra",
            upgradable_deployments=("gpt-5.6-sol", "gpt-5.6-sol-alt"),
            sleep=lambda _delay: None,
        )
        self.assertEqual(result.text, "New answer.")
        self.assertEqual(self.state()["deployment"], "gpt-6-astra")
        environment = {
            "AZURE_OPENAI_FALLBACK_DEPLOYMENTS": "gpt-5.6-sol-alt,gpt-5.4-pro",
            "OPENAI_FALLBACK_MODELS": "gpt-5.4-pro",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            self.assertEqual(
                self.runner._upgradable_deployments(),
                ("gpt-5.6-sol", "gpt-5.4-pro", "gpt-5.6-sol-alt"),
            )

    def test_openai_model_not_found_moves_down_the_chain(self):
        client = FakeClient(
            [
                make_status_error(404, "model_not_found"),
                FakeResponse("resp-ok", "Fallback answer."),
            ]
        )
        outcome = self.runner.request_response(
            client,
            {"model": "gpt-6-astra", "input": "Q."},
            purpose="answer",
            sleep=lambda _delay: None,
        )
        self.assertEqual(outcome.deployment, "gpt-5.6-sol")

    def test_gpt54_named_deployments_get_the_gpt54_profile(self):
        request = {
            "model": "gpt-6-astra",
            "reasoning": {"mode": "pro", "effort": "max", "context": "all_turns"},
        }
        routed = self.runner._request_for_deployment(request, "gpt-5.4-pro-eu")
        self.assertEqual(routed["reasoning"], {"effort": "xhigh"})
        self.assertEqual(
            self.runner._reasoning_profile("gpt-5.4-pro-eu"),
            ("not configurable", "xhigh"),
        )

    def test_backup_methods_without_credentials_are_skipped_per_resource(self):
        legacy = "https://legacy.openai.azure.com/openai/v1/"
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "AZURE_OPENAI_GPT6_API_KEY": "gpt6-key",
            "AZURE_OPENAI_FALLBACK_ENDPOINT": legacy,
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            factory = self.runner._configured_client_factories(("entra", "azure-key"))[
                "azure"
            ]
            self.assertIsInstance(factory(AZURE_V1), self.runner.AuthFallbackClient)
            self.assertNotIsInstance(factory(legacy), self.runner.AuthFallbackClient)
            only_keys = self.runner._configured_client_factories(("azure-key",))
            with self.assertRaises(self.runner.MissingCredentialError):
                only_keys["azure"](legacy)

    def test_signed_out_providers_are_skipped_after_wrapping_around(self):
        target = self.runner.RouteTarget
        azure = FakeClient([make_status_error(401, "unauthorized")])
        first = FakeClient(
            [
                make_status_error(429, "rate_limit_exceeded"),
                FakeResponse("resp-ok", "Answer."),
            ]
        )
        second = FakeClient([make_status_error(429, "rate_limit_exceeded")])
        router = self.runner.DeploymentRouter(
            [
                target("gpt-6-astra", azure, AZURE_V1, "primary", "azure"),
                target("gpt-6-astra", first, OPENAI_V1, "primary", "openai"),
                target("gpt-5.4-pro", second, OPENAI_V1, "fallback", "openai"),
            ]
        )
        outcome = self.runner.request_response(
            router,
            {"model": "gpt-6-astra", "input": "Q."},
            purpose="answer",
            max_attempts=4,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
        )
        self.assertEqual(outcome.response.id, "resp-ok")
        self.assertEqual(len(azure.responses.calls), 1)

    def test_tampered_provider_values_fail_closed(self):
        view = self.runner.JournalView(
            [{"event": "submitting", "attempt_id": "a", "provider": ["azure"]}]
        )
        attempt = view.attempts["a"]
        self.assertEqual(
            self.runner._attempt_summary(attempt, "active")["provider"], "invalid"
        )
        self.assertIn(
            "journal", self.runner._provider_mismatch(attempt, ("azure", "openai"))
        )

    def test_azure_clients_do_not_send_openai_account_headers(self):
        requests = []
        environment = {"OPENAI_ORG_ID": "org-test", "OPENAI_PROJECT_ID": "proj-test"}
        with mock.patch.dict("os.environ", environment, clear=True):
            azure = self.runner.create_client(
                AZURE_PREVIEW,
                auth="azure-key",
                api_key="azure-test-key",
                openai_factory=recording_sdk(requests, completed_body()),
            )
            openai_client = self.runner.create_client(
                None,
                auth="openai-key",
                api_key="sk-test",
                openai_factory=recording_sdk(requests, completed_body()),
            )
        azure.responses.create(model="gpt-6-astra", input="Q.")
        openai_client.responses.create(model="gpt-6-astra", input="Q.")
        azure_request, openai_request = requests
        self.assertNotIn("openai-organization", azure_request.headers)
        self.assertNotIn("openai-project", azure_request.headers)
        self.assertEqual(openai_request.headers["openai-organization"], "org-test")

    def test_primary_model_may_not_be_listed_as_an_azure_fallback(self):
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "AZURE_OPENAI_FALLBACK_DEPLOYMENTS": "gpt-6-astra,gpt-5.4-pro",
        }
        with (
            mock.patch.dict("os.environ", environment, clear=True),
            self.assertRaisesRegex(
                self.runner.DeepThinkError, "AZURE_OPENAI_FALLBACK_DEPLOYMENTS"
            ),
        ):
            self.runner._router_from_args(
                self.parse("ask", "--prompt", "Q."),
                "gpt-6-astra",
                lambda _resource: object(),
            )

    def test_unparsable_hosts_are_refused(self):
        with self.assertRaises(self.runner.DeepThinkError):
            self.runner._refuse_cross_provider_host("azure", "https://[bad")

    def test_journaled_errors_redact_api_key_fragments(self):
        self.seed()
        failure = make_status_error(
            401,
            "invalid_api_key",
            message="Incorrect API key provided: sk-proj-****WXYZ. Check it.",
        )
        with self.assertRaises(self.runner.DeepThinkError):
            self.ask(FakeClient([failure]))
        journal = (self.project_dir / "requests" / "journal.jsonl").read_text("utf-8")
        self.assertIn("turn_error", journal)
        self.assertNotIn("WXYZ", journal)


GATEWAY = "https://contoso.azure-api.net/openai/v1/"


class ReviewRoundTwoTests(ProviderProjectFixture):
    """Regressions for the second review of the multi-provider change."""

    def route(self, *targets, provider="azure"):
        return self.runner.DeploymentRouter(
            [self.runner.RouteTarget(*target) for target in targets],
            provider=provider,
        )

    def test_openai_403_model_not_found_moves_down_the_chain(self):
        denied = make_status_error(
            403,
            "model_not_found",
            message="Project `proj_x` does not have access to model `gpt-6-astra`",
        )
        client = FakeClient([denied, FakeResponse("resp-ok", "Fallback answer.")])
        router = self.route(
            ("gpt-6-astra", client, OPENAI_V1, "primary", "openai"),
            ("gpt-5.6-sol", client, OPENAI_V1, "fallback", "openai"),
            provider="openai",
        )
        outcome = self.runner.request_response(
            router,
            {"model": "gpt-6-astra", "input": "Q."},
            purpose="answer",
            sleep=lambda _delay: None,
        )
        self.assertEqual(outcome.deployment, "gpt-5.6-sol")
        first = FakeClient([denied])
        second = FakeClient([FakeResponse("resp-unused", "Unused.")])
        fallback = self.runner.AuthFallbackClient(
            [("azure-key", first), ("entra", second)]
        )
        with self.assertRaises(type(denied)):
            fallback.responses.create(model="gpt-6-astra")
        self.assertEqual(second.responses.calls, [])

    def test_sign_in_failure_returns_to_a_provider_that_was_only_throttled(self):
        azure = FakeClient(
            [
                make_status_error(429, "rate_limit_exceeded"),
                FakeResponse("resp-ok", "Answer."),
            ]
        )
        backup = FakeClient([make_status_error(401, "invalid_api_key")])
        router = self.route(
            ("gpt-6-astra", azure, AZURE_V1, "primary", "azure"),
            ("gpt-6-astra", backup, OPENAI_V1, "primary", "openai"),
        )
        outcome = self.runner.request_response(
            router,
            {"model": "gpt-6-astra", "input": "Q."},
            purpose="answer",
            max_attempts=4,
            base_delay=0,
            sleep=lambda _delay: None,
            random_value=lambda: 0,
        )
        self.assertEqual(outcome.response.id, "resp-ok")
        self.assertEqual(len(backup.responses.calls), 1)

    def test_polling_errors_are_redacted_everywhere(self):
        self.seed()
        leaked = make_status_error(
            401,
            "invalid_api_key",
            message="Incorrect API key provided: sk-proj-****WXYZ.",
        )
        client = FakeClient(
            [FakeResponse("resp-job", "", status="queued", incomplete_reason=None)],
            [leaked],
        )
        with self.assertRaises(self.runner.DeepThinkError) as raised:
            self.ask(client)
        self.assertNotIn("WXYZ", str(raised.exception))
        journal = (self.project_dir / "requests" / "journal.jsonl").read_text("utf-8")
        self.assertIn("poll_stopped", journal)
        self.assertNotIn("WXYZ", journal)

    def test_unavailable_backup_methods_are_skipped_with_one_note(self):
        def fake_create(resource, **options):
            if options.get("auth") == "azure-key":
                return f"key client for {resource}"
            raise self.runner.DeepThinkError(
                "Install dependencies with: python -m pip install azure-identity"
            )

        notes = []
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "AZURE_OPENAI_API_KEY": "azure-key-value",
        }
        with (
            mock.patch.dict("os.environ", environment, clear=True),
            mock.patch.object(self.runner, "create_client", fake_create),
        ):
            factory = self.runner._configured_client_factories(
                ("azure-key", "entra"),
                on_unavailable=lambda method, error: notes.append(method),
            )["azure"]
            self.assertEqual(factory(AZURE_V1), f"key client for {AZURE_V1}")
            self.assertEqual(factory(GATEWAY), f"key client for {GATEWAY}")
            only_entra = self.runner._configured_client_factories(("entra",))
            with self.assertRaisesRegex(self.runner.DeepThinkError, "azure-identity"):
                only_entra["azure"](AZURE_V1)
        self.assertEqual(notes, ["entra"])

    def test_production_clients_never_follow_redirects(self):
        captured = []

        def sdk(**options):
            captured.append(options)
            return SimpleNamespace()

        with mock.patch.object(self.runner, "_import_openai", lambda: sdk):
            self.runner.create_client(AZURE_PREVIEW, auth="azure-key", api_key="k")
            self.runner.create_client(None, auth="openai-key", api_key="sk")
            self.runner.create_client(
                AZURE_PREVIEW,
                credential_factory=lambda: object(),
                token_provider_factory=lambda _credential, _scope: lambda: "token",
            )
        self.assertEqual(len(captured), 3)
        for options in captured:
            self.assertFalse(options["http_client"].follow_redirects)

    def test_openai_jobs_without_a_resource_are_refused(self):
        seen = []
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk"}, clear=True):
            factory = self.runner._trusted_client_factory(
                seen.append, self.parse("cancel", "--auth", "openai-key"), "openai"
            )
            with self.assertRaises(self.runner.DeepThinkError):
                factory(None)
        self.assertEqual(seen, [])

    def test_relative_config_home_and_nul_values_are_rejected(self):
        default = self.runner.default_env_file({"XDG_CONFIG_HOME": "relative-cfg"})
        self.assertTrue(default is None or default.is_absolute())
        path = self.root / "nul.env"
        path.write_bytes(b"DEEP_THINK_AUTH=entra\x00secret-tail\n")
        with self.assertRaises(self.runner.DeepThinkError) as raised:
            self.runner.load_env_file(path, environ={})
        self.assertNotIn("secret-tail", str(raised.exception))

    def test_endpoints_configured_for_both_providers_stay_usable(self):
        environment = {
            "AZURE_OPENAI_ENDPOINT": GATEWAY,
            "OPENAI_BASE_URL": GATEWAY,
            "OPENAI_API_KEY": "sk",
        }
        seen = []
        with mock.patch.dict("os.environ", environment, clear=True):
            self.runner._trusted_client_factory(
                seen.append, self.parse("cancel"), "azure"
            )(GATEWAY)
            self.runner._trusted_client_factory(
                seen.append, self.parse("cancel", "--auth", "openai-key"), "openai"
            )(GATEWAY)
        self.assertEqual(seen, [GATEWAY, GATEWAY])

    def test_gpt6_backup_deployment_applies_only_to_the_configured_primary(self):
        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": "A",
            "AZURE_OPENAI_GPT6_BACKUP_ENDPOINT": "B",
            "AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT": "gpt-6-astra-backup",
            "AZURE_OPENAI_ENDPOINT": "L",
        }

        def backup(deployment):
            with mock.patch.dict("os.environ", environment, clear=True):
                router = self.runner._router_from_args(
                    self.parse("ask", "--prompt", "Q."),
                    deployment,
                    lambda _resource: object(),
                )
            return [
                (t.deployment, t.resource) for t in router.targets if t.role == "backup"
            ]

        self.assertEqual(backup("gpt-6-astra"), [("gpt-6-astra-backup", "B")])
        self.assertEqual(backup("my-model"), [("my-model", "B")])

    def test_cancel_continues_past_one_jobs_error(self):
        self.seed()
        self.start_job("azure", AZURE_V1)
        CrossProviderRecoveryTests.forge_openai_job(self)
        log = []

        def factory(resource):
            if resource == PROXY_V1:
                raise self.runner.DeepThinkError("An API key is required.")
            return LoggingClient(resource, log)

        environment = {
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
            "OPENAI_BASE_URL": PROXY_V1,
            "DEEP_THINK_AUTH": "entra,openai-key",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            code, stdout, stderr = self.cli("cancel", factory=factory)
        self.assertEqual(code, 1, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["failed"], 1)
        results = {item["response_id"]: item for item in report["results"]}
        self.assertEqual(results["resp-job"]["status"], "cancelled")
        self.assertIn("API key", results["resp-openai"]["error"])
        self.assertEqual(log, [(AZURE_V1, "cancel", "resp-job")])

    def test_tampered_journal_field_types_are_reported(self):
        self.seed()
        record = {
            "schema": "deep-think-request-journal/v1",
            "recorded_at": "2026-10-04T00:00:00+00:00",
            "lock_id": None,
            "event": "submitting",
            "attempt_id": "bad",
            "turn_id": "turn",
            "resource": ["not", "a", "url"],
        }
        path = self.project_dir / "requests" / "journal.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        with self.assertRaisesRegex(self.runner.DeepThinkError, "tampered"):
            self.status()

    def test_openai_failures_are_labelled_openai(self):
        client = FakeClient([make_status_error(400, "invalid_request_error")])
        router = self.route(
            ("gpt-6-astra", client, OPENAI_V1, "primary", "openai"),
            provider="openai",
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "OpenAI HTTP 400"):
            self.runner.request_response(
                router,
                {"model": "gpt-6-astra", "input": "Q."},
                purpose="answer",
                sleep=lambda _delay: None,
            )


class FinalReviewTests(ProviderProjectFixture):
    """Regressions for the final review of the multi-provider change."""

    def openai_router(self, client):
        target = self.runner.RouteTarget(
            "gpt-6-astra", client, OPENAI_V1, "primary", "openai"
        )
        return self.runner.DeploymentRouter([target], provider="openai")

    def test_unknown_openai_submissions_keep_openai_guidance(self):
        self.seed()
        gateway = make_status_error(502, "bad_gateway")
        with self.assertRaises(self.runner.SubmissionUnknownError) as first:
            self.ask(self.openai_router(FakeClient([gateway])))
        with self.assertRaises(self.runner.SubmissionUnknownError) as second:
            self.ask(self.openai_router(FakeClient([])))
        for raised in (first, second):
            message = str(raised.exception)
            self.assertIn("OpenAI dashboard", message)
            self.assertIn("reconcile --auth openai-key", message)
            self.assertNotIn("Azure telemetry", message)

    def test_openai_response_failures_are_labelled_openai(self):
        failed = FakeResponse(
            "resp-failed",
            "",
            status="failed",
            error=SimpleNamespace(code="server_error", message="Unavailable."),
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "OpenAI response"):
            self.runner.request_response(
                self.openai_router(FakeClient([failed])),
                {"model": "gpt-6-astra", "input": "Q."},
                purpose="answer",
                max_attempts=1,
                sleep=lambda _delay: None,
            )

    def test_tampered_journal_values_are_reported_not_crashing(self):
        for field, value in (
            ("status", ["completed"]),
            ("attempt_ids", 5),
            ("state_sha256", ["x"]),
        ):
            with self.subTest(field=field):
                self.tearDown()
                self.setUp()
                self.seed()
                record = {
                    "schema": "deep-think-request-journal/v1",
                    "recorded_at": "2026-10-04T00:00:00+00:00",
                    "lock_id": None,
                    "event": "poll_status",
                    "attempt_id": "x",
                    field: value,
                }
                path = self.project_dir / "requests" / "journal.jsonl"
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
                with self.assertRaisesRegex(self.runner.DeepThinkError, "tampered"):
                    self.status()

    def test_backup_provider_without_credentials_does_not_block_the_primary(self):
        notes = []

        def missing(_resource):
            raise self.runner.MissingCredentialError("An API key is required.")

        factories = {"azure": lambda _resource: object(), "openai": missing}
        environment = {"AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1}
        with mock.patch.dict("os.environ", environment, clear=True):
            router = self.runner._router_from_args(
                self.parse("ask", "--prompt", "Q.", "--auth", "entra,openai-key"),
                "gpt-6-astra",
                factories,
                on_unavailable=lambda provider, _error: notes.append(provider),
            )
            self.assertEqual({t.provider for t in router.targets}, {"azure"})
            self.assertEqual(notes, ["openai"])
            with self.assertRaises(self.runner.MissingCredentialError):
                self.runner._router_from_args(
                    self.parse("ask", "--prompt", "Q.", "--auth", "openai-key,entra"),
                    "gpt-6-astra",
                    factories,
                )


if __name__ == "__main__":
    unittest.main()
