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


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_module()

    def args(self, *extra):
        return self.runner._build_parser().parse_args(
            ["ask", "--project", "p", "--prompt", "Q.", *extra]
        )

    def test_provider_and_azure_auth_resolution(self):
        azure = {"AZURE_OPENAI_GPT6_ENDPOINT": AZURE_PREVIEW}
        cases = [
            ({}, [], ("azure", "entra")),
            (azure, [], ("azure", "entra")),
            ({**azure, "AZURE_OPENAI_API_KEY": "k"}, [], ("azure", "key")),
            (
                {**azure, "AZURE_OPENAI_API_KEY": "k"},
                ["--azure-auth", "entra"],
                ("azure", "entra"),
            ),
            ({"OPENAI_API_KEY": "sk"}, [], ("openai", None)),
            ({"OPENAI_API_KEY": "sk", **azure}, [], ("azure", "entra")),
            (
                {"OPENAI_API_KEY": "sk", **azure},
                ["--provider", "openai"],
                ("openai", None),
            ),
            ({"DEEP_THINK_PROVIDER": "openai"}, [], ("openai", None)),
            ({"DEEP_THINK_AZURE_AUTH": "key"}, [], ("azure", "key")),
        ]
        for environment, extra, expected in cases:
            with (
                self.subTest(environment=environment, extra=extra),
                mock.patch.dict("os.environ", environment, clear=True),
            ):
                self.assertEqual(
                    self.runner._resolve_connection(self.args(*extra)), expected
                )

    def test_invalid_provider_settings_are_rejected(self):
        for environment in (
            {"DEEP_THINK_PROVIDER": "gemini"},
            {"DEEP_THINK_AZURE_AUTH": "password"},
        ):
            with (
                self.subTest(environment=environment),
                mock.patch.dict("os.environ", environment, clear=True),
                self.assertRaises(self.runner.DeepThinkError),
            ):
                self.runner._resolve_connection(self.args())

    def test_openai_uses_its_own_model_chain_on_one_resource(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk"}, clear=True):
            router = self.runner._router_from_args(
                self.args("--provider", "openai"), "gpt-6-astra", lambda _: object()
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
            factory = self.runner._configured_client_factory(self.args())
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
                seen.append, self.args("--provider", "openai")
            )
            factory("https://api.openai.com/v1/")
            with self.assertRaises(self.runner.DeepThinkError):
                factory("https://other.example/v1/")
        self.assertEqual(seen, ["https://api.openai.com/v1/"])


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
                    self.assertEqual(
                        self.runner._resolve_connection(args), ("azure", "entra")
                    )
                    built.clear()
                    with mock.patch.object(self.runner, "create_client", record):
                        self.runner._configured_client_factory(args)(AZURE_V1)
                    self.assertEqual(built, [{}])
            args = self.parse("ask", "--provider", "openai", "--endpoint", PROXY_V1)
            self.assertEqual(self.runner._resolve_connection(args), ("openai", None))

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
                    self.runner.DeepThinkError, "--provider azure"
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
                        self.runner.DeepThinkError, "--provider openai"
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
                seen.append, self.parse("cancel", "--provider", "openai")
            )
            openai_trusted(PROXY_V1)
            with self.assertRaisesRegex(self.runner.DeepThinkError, "--provider azure"):
                openai_trusted(AZURE_V1)
            azure_trusted = self.runner._trusted_client_factory(
                seen.append, self.parse("cancel")
            )
            azure_trusted(AZURE_V1)
            with self.assertRaisesRegex(
                self.runner.DeepThinkError, "--provider openai"
            ):
                azure_trusted(PROXY_V1)
            explicit = self.runner._trusted_client_factory(
                seen.append, self.parse("cancel", "--endpoint", OPENAI_V1)
            )
            with self.assertRaisesRegex(
                self.runner.DeepThinkError, "--provider openai"
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

    def assert_refused(self, arguments, log, provider):
        flag = f"--provider {provider}"
        code, stdout, stderr = self.cli(*arguments, log=log)
        if arguments[0] == "resume":
            self.assertEqual(code, 2, stdout)
            self.assertIn(flag, stderr)
        elif arguments[0] == "cancel":
            self.assertEqual(code, 0, stderr)
            [result] = json.loads(stdout)["results"]
            self.assertIn(flag, result["error"])
        else:
            self.assertEqual(code, 0, stderr)
            self.assertTrue(
                any(flag in action for action in json.loads(stdout)["actions"]),
                stdout,
            )

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
            self.assertIn(f"{command} --project recovery --provider openai", steps)

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
                    self.assert_refused(arguments, log, "openai")
            self.assertEqual(log, [])
            code, _stdout, stderr = self.cli("cancel", "--provider", "openai", log=log)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(log, [(PROXY_V1, "cancel", "resp-job")])

    def test_openai_recovery_never_touches_azure_jobs(self):
        self.seed()
        self.start_job("azure", AZURE_V1)
        log = []
        environment = {
            "DEEP_THINK_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk",
            "AZURE_OPENAI_GPT6_ENDPOINT": AZURE_V1,
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            for arguments in (("cancel",), ("reconcile",), ("resume",)):
                with self.subTest(arguments=arguments):
                    self.assert_refused(arguments, log, "azure")
            self.assertEqual(log, [])
            code, _stdout, stderr = self.cli("cancel", "--provider", "azure", log=log)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(log, [(AZURE_V1, "cancel", "resp-job")])


if __name__ == "__main__":
    unittest.main()
