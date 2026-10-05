import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_deep_think import FakeClient, FakeResponse, load_module, make_status_error


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


def failed_job(response_id):
    return SimpleNamespace(
        id=response_id,
        status="failed",
        error=SimpleNamespace(code="server_error", message="Support ID: wfr-test"),
        output=[],
        output_text="",
        usage=None,
        incomplete_details=None,
    )


class PollDeadlineTests(unittest.TestCase):
    def test_default_deadline_and_recovery_policy(self):
        runner = load_module()
        args = runner._build_parser().parse_args(
            ["ask", "--project", "defaults", "--prompt", "Question."]
        )
        self.assertEqual(args.poll_timeout, 3600)
        self.assertFalse(args.recover_service_errors)

    def test_sdk_retrieval_receives_remaining_timeout(self):
        import httpx
        import openai

        runner = load_module()
        clock = Clock()
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"id": "job-sdk", "status": "queued", "output": []}
            )

        with (
            httpx.Client(transport=httpx.MockTransport(handle)) as http_client,
            openai.OpenAI(
                base_url="https://example.invalid/openai/v1/",
                api_key=lambda: "test-token",
                max_retries=0,
                http_client=http_client,
            ) as client,
            mock.patch.object(runner.time, "monotonic", clock.monotonic),
            self.assertRaisesRegex(runner.DeepThinkError, "poll.*deadline"),
        ):
            runner.request_response(
                client,
                runner.build_response_request([], "gpt-6-astra"),
                purpose="answer",
                poll_timeout=5,
                sleep=clock.sleep,
            )
        self.assertEqual(
            [request.method for request in requests], ["POST", "GET", "GET"]
        )
        for request, timeout in zip(requests[1:], [3, 1]):
            self.assertEqual(set(request.extensions["timeout"].values()), {timeout})

    def test_queued_job_stops_at_deadline_without_failover(self):
        runner = load_module()
        clock = Clock()
        queued = FakeResponse("job-stalled", "", status="queued")
        primary = FakeClient([queued], [queued] * 10)
        backup = FakeClient([])
        router = runner.DeploymentRouter(
            [("gpt-6-astra", primary), ("gpt-6-astra", backup)]
        )
        with (
            mock.patch.object(runner.time, "monotonic", clock.monotonic),
            self.assertRaisesRegex(
                runner.DeepThinkError, "poll.*deadline.*job-stalled"
            ) as raised,
        ):
            runner.request_response(
                router,
                runner.build_response_request([], "gpt-6-astra"),
                purpose="answer",
                poll_timeout=5,
                sleep=clock.sleep,
            )
        self.assertEqual(clock.now, 5)
        self.assertEqual(primary.responses.retrieve_calls, ["job-stalled"] * 2)
        self.assertEqual(primary.responses.retrieve_timeouts, [3, 1])
        self.assertEqual(len(primary.responses.calls), 1)
        self.assertEqual(backup.responses.calls, [])
        self.assertIn("target 1/2", str(raised.exception))
        self.assertIn("may still be running", str(raised.exception))

    def test_retry_after_cannot_extend_poll_deadline(self):
        runner = load_module()
        clock = Clock()
        client = FakeClient(
            [FakeResponse("job-retry", "", status="queued")],
            [make_status_error(429, "no_capacity", headers={"Retry-After": "30"})],
        )
        with (
            mock.patch.object(runner.time, "monotonic", clock.monotonic),
            self.assertRaisesRegex(runner.DeepThinkError, "poll.*deadline"),
        ):
            runner.request_response(
                client,
                runner.build_response_request([], "gpt-6-astra"),
                purpose="answer",
                poll_timeout=5,
                sleep=clock.sleep,
            )
        self.assertEqual(clock.now, 5)
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(client.responses.retrieve_calls, ["job-retry"])
        self.assertEqual(clock.waits, [2, 3])

    def test_poll_exhaustion_identifies_original_response_and_target(self):
        runner = load_module()
        client = FakeClient(
            [FakeResponse("job-lost", "", status="queued")],
            [make_status_error(500, "server_error")],
        )
        with self.assertRaisesRegex(
            runner.DeepThinkError, "job-lost.*gpt-6-astra.*target 1/3"
        ):
            runner.request_response(
                client,
                runner.build_response_request([], "gpt-6-astra"),
                purpose="answer",
                max_attempts=1,
                sleep=lambda _: None,
            )
        self.assertEqual(len(client.responses.calls), 1)

    def test_invalid_poll_timeout_is_rejected_before_submission(self):
        runner = load_module()
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout):
                client = FakeClient([])
                with self.assertRaisesRegex(runner.DeepThinkError, "poll timeout"):
                    runner.request_response(
                        client,
                        runner.build_response_request([], "gpt-6-astra"),
                        purpose="answer",
                        poll_timeout=timeout,
                    )
                self.assertEqual(client.responses.calls, [])

    def test_cli_deadline_applies_to_accepted_jobs(self):
        runner = load_module()
        clock = Clock()
        client = FakeClient(
            [FakeResponse("job-cli", "", status="queued")],
            [FakeResponse("job-cli", "", status="in_progress")] * 4,
        )
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(runner.time, "monotonic", clock.monotonic),
        ):
            # The real sleep is short; advance the clock on each retrieval.
            original = client.responses.retrieve

            def retrieve(response_id, **kwargs):
                result = original(response_id, **kwargs)
                clock.now += 10
                return result

            client.responses.retrieve = retrieve
            code = runner.main(
                [
                    "ask",
                    "--project",
                    "deadline",
                    "--root",
                    root,
                    "--prompt",
                    "Question.",
                    "--endpoint",
                    "primary",
                    "--poll-timeout",
                    "0.01",
                ],
                client_factory=lambda _: client,
                stdout=io.StringIO(),
                stderr=stderr,
            )
        self.assertEqual(code, 2)
        self.assertIn("job-cli", stderr.getvalue())
        self.assertIn("deadline", stderr.getvalue())
        self.assertEqual(len(client.responses.calls), 1)


class ServiceRecoveryTests(unittest.TestCase):
    def seed(self, runner, root, text="Existing result."):
        runner.run_turn(
            FakeClient([FakeResponse("old", text)]),
            root=root,
            project="recovery",
            prompt="Original question.",
            deployment="gpt-6-astra",
        )
        return {
            "root": root,
            "project": "recovery",
            "prompt": "Continue.",
            "deployment": "gpt-6-astra",
            "max_attempts": 1,
            "sleep": lambda _: None,
        }

    def test_terminal_failure_diagnostics_include_id_budget_and_support_message(self):
        runner = load_module()
        client = FakeClient([failed_job("job-service")])
        with self.assertRaises(runner.DeepThinkError) as raised:
            runner.request_response(
                client,
                runner.build_response_request([], "gpt-6-astra", 25014),
                purpose="answer",
                max_attempts=1,
            )
        message = str(raised.exception)
        for value in (
            "job-service",
            "gpt-6-astra",
            "target 1/3",
            "25014",
            "wfr-test",
            "request_bytes=",
            "request_sha256=",
        ):
            self.assertIn(value, message)

    def test_service_recovery_is_opt_in(self):
        runner = load_module()
        with tempfile.TemporaryDirectory() as root:
            options = self.seed(runner, root)
            project = Path(root) / "recovery"

            def committed_files():
                # The request journal changes by design; committed files must not.
                return {
                    p.name: p.read_bytes() for p in project.iterdir() if p.is_file()
                }

            before = committed_files()
            client = FakeClient([failed_job("failed")])
            with self.assertRaises(runner.DeepThinkError):
                runner.run_turn(client, **options)
            self.assertEqual(before, committed_files())
        self.assertEqual(len(client.responses.calls), 1)

    def test_opt_in_recovers_from_visible_history_once(self):
        runner = load_module()
        events = []
        with tempfile.TemporaryDirectory() as root:
            options = self.seed(runner, root)
            client = FakeClient(
                [
                    failed_job("failed"),
                    FakeResponse("summary", "Preserved summary."),
                    FakeResponse("answer", "Recovered answer."),
                ]
            )
            result = runner.run_turn(
                client, **options, recover_service_errors=True, on_retry=events.append
            )
            project = Path(root) / "recovery"
            state = json.loads((project / "state.json").read_text())
            old = (project / "0001-transcript.md").read_text(encoding="utf-8")
            self.assertIn("Existing result.", old)
            self.assertIn("terminal server_error", old)
            self.assertEqual(state["volume"], 2)
            runner._validate_state(state, "recovery")
        self.assertEqual(result.text, "Recovered answer.")
        summary_request = client.responses.calls[1]
        self.assertEqual(len(summary_request["input"]), 1)
        self.assertIn("Existing result.", summary_request["input"][0]["content"])
        self.assertNotIn("encrypted_content", json.dumps(summary_request["input"]))
        self.assertIn(
            "Preserved summary.", client.responses.calls[2]["input"][0]["content"]
        )
        self.assertTrue(any("server_error" in event.reason for event in events))

    def test_failed_recovered_answer_keeps_summary_and_does_not_loop(self):
        runner = load_module()
        with tempfile.TemporaryDirectory() as root:
            options = self.seed(runner, root)
            client = FakeClient(
                [
                    failed_job("original"),
                    FakeResponse("summary", "Continuation."),
                    failed_job("second"),
                ]
            )
            with self.assertRaisesRegex(runner.DeepThinkError, "second"):
                runner.run_turn(client, **options, recover_service_errors=True)
            project = Path(root) / "recovery"
            state = json.loads((project / "state.json").read_text())
            self.assertEqual(state["volume"], 2)
            self.assertEqual(state["turn"], 0)
            self.assertIn(
                "Continuation.",
                (project / "0002-transcript.md").read_text(encoding="utf-8"),
            )
            runner._validate_state(state, "recovery")
        self.assertEqual(len(client.responses.calls), 3)

    def test_service_recovery_uses_smaller_chunks_and_keeps_entire_transcript(self):
        runner = load_module()
        with tempfile.TemporaryDirectory() as root:
            options = self.seed(runner, root, text="A" * 450_000)
            source = (Path(root) / "recovery" / "0001-transcript.md").read_text(
                encoding="utf-8"
            )
            client = FakeClient(
                [
                    failed_job("failed"),
                    FakeResponse("chunk-1", "First part."),
                    FakeResponse("chunk-2", "Second part."),
                    FakeResponse("chunk-3", "Third part."),
                    FakeResponse("summary", "Combined summary."),
                    FakeResponse("answer", "Recovered."),
                ]
            )
            runner.run_turn(client, **options, recover_service_errors=True)
            archived = (Path(root) / "recovery" / "0001-transcript.md").read_text(
                encoding="utf-8"
            )
            state = json.loads((Path(root) / "recovery" / "state.json").read_text())
            self.assertEqual(state["cumulative_tokens"], 900)
        self.assertTrue(archived.startswith(source))
        chunks = [
            call["input"][0]["content"]
            .split("--- BEGIN CHUNK ---\n", 1)[1]
            .rsplit("\n--- END CHUNK ---", 1)[0]
            for call in client.responses.calls[1:4]
        ]
        self.assertEqual("".join(chunks), source)
        for call in client.responses.calls[1:5]:
            self.assertLess(len(call["input"][0]["content"].encode("utf-8")), 400_000)
        self.assertEqual(len(client.responses.calls), 6)

    def test_proactive_rollover_service_failure_recovers_only_once(self):
        runner = load_module()
        with tempfile.TemporaryDirectory() as root:
            # A small ceiling forces the next turn through the proactive rollover.
            runner.run_turn(
                FakeClient([FakeResponse("old", "Existing.", input_tokens=800)]),
                root=root,
                project="recovery",
                prompt="Question.",
                deployment="gpt-6-astra",
                rollover_tokens=1000,
            )
            client = FakeClient(
                [
                    failed_job("rollover"),
                    FakeResponse("summary", "Continuation."),
                    failed_job("answer"),
                ]
            )
            with self.assertRaisesRegex(runner.DeepThinkError, "response_id=answer"):
                runner.run_turn(
                    client,
                    root=root,
                    project="recovery",
                    prompt="Continue.",
                    deployment="gpt-6-astra",
                    recover_service_errors=True,
                    max_attempts=1,
                    sleep=lambda _: None,
                )
            state = json.loads((Path(root) / "recovery" / "state.json").read_text())
            self.assertEqual(state["volume"], 2)
            self.assertEqual(state["turn"], 0)
        self.assertEqual(len(client.responses.calls), 3)

    def test_poll_timeout_never_starts_visible_recovery(self):
        runner = load_module()
        clock = Clock()
        with tempfile.TemporaryDirectory() as root:
            options = self.seed(runner, root)
            options["sleep"] = clock.sleep
            client = FakeClient(
                [FakeResponse("job-running", "", status="queued")],
                [FakeResponse("job-running", "", status="in_progress")] * 5,
            )
            with (
                mock.patch.object(runner.time, "monotonic", clock.monotonic),
                self.assertRaisesRegex(runner.DeepThinkError, "poll.*deadline"),
            ):
                runner.run_turn(
                    client, **options, poll_timeout=5, recover_service_errors=True
                )
        self.assertEqual(len(client.responses.calls), 1)

    def test_context_recovery_cannot_restart_service_recovery(self):
        runner = load_module()
        with tempfile.TemporaryDirectory() as root:
            runner.run_turn(
                FakeClient([FakeResponse("old", "Existing.", input_tokens=800)]),
                root=root,
                project="recovery",
                prompt="Question.",
                deployment="gpt-6-astra",
                rollover_tokens=1000,
            )
            client = FakeClient(
                [
                    failed_job("rollover"),
                    FakeResponse("summary", "Continuation."),
                    make_status_error(400, "context_length_exceeded"),
                    failed_job("second-rollover"),
                ]
            )
            with self.assertRaisesRegex(
                runner.DeepThinkError, "response_id=second-rollover"
            ):
                runner.run_turn(
                    client,
                    root=root,
                    project="recovery",
                    prompt="Continue.",
                    deployment="gpt-6-astra",
                    recover_service_errors=True,
                    max_attempts=1,
                    sleep=lambda _: None,
                )
        self.assertEqual(len(client.responses.calls), 4)

    def test_visible_summary_deadline_is_not_replaced_by_default(self):
        runner = load_module()
        for original in ("Short result.", "A" * 450_000):
            with self.subTest(chunked=len(original) > 400_000):
                clock = Clock()
                with tempfile.TemporaryDirectory() as root:
                    options = self.seed(runner, root, text=original)
                    options["sleep"] = clock.sleep
                    client = FakeClient(
                        [
                            failed_job("original"),
                            FakeResponse("summary-job", "", status="queued"),
                        ],
                        [FakeResponse("summary-job", "", status="in_progress")] * 4,
                    )
                    with (
                        mock.patch.object(runner.time, "monotonic", clock.monotonic),
                        self.assertRaisesRegex(runner.DeepThinkError, "poll.*deadline"),
                    ):
                        runner.run_turn(
                            client,
                            **options,
                            poll_timeout=5,
                            recover_service_errors=True,
                        )
                    self.assertEqual(clock.now, 5)
                    self.assertEqual(len(client.responses.calls), 2)
                    state = json.loads(
                        (Path(root) / "recovery" / "state.json").read_text()
                    )
                    self.assertEqual(state["volume"], 1)

    def test_cli_enables_service_recovery(self):
        runner = load_module()
        with tempfile.TemporaryDirectory() as root:
            self.seed(runner, root)
            client = FakeClient(
                [
                    failed_job("cli-failed"),
                    FakeResponse("summary", "Continuation."),
                    FakeResponse("answer", "Recovered."),
                ]
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.dict("os.environ", {}, clear=True):
                code = runner.main(
                    [
                        "ask",
                        "--project",
                        "recovery",
                        "--root",
                        root,
                        "--endpoint",
                        "primary",
                        "--prompt",
                        "Continue.",
                        "--max-attempts",
                        "1",
                        "--recover-service-errors",
                    ],
                    client_factory=lambda _: client,
                    stdout=stdout,
                    stderr=stderr,
                )
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue(), "Recovered.\n")
            self.assertIn("cli-failed", stderr.getvalue())

    def test_other_errors_and_first_turns_do_not_trigger_recovery(self):
        runner = load_module()
        for failure in (
            make_status_error(401, "unauthorized"),
            make_status_error(429, "no_capacity"),
            make_status_error(500, "server_error"),
        ):
            with (
                self.subTest(failure=str(failure)),
                tempfile.TemporaryDirectory() as root,
            ):
                options = self.seed(runner, root)
                client = FakeClient([failure])
                with self.assertRaises(runner.DeepThinkError):
                    runner.run_turn(client, **options, recover_service_errors=True)
                self.assertEqual(len(client.responses.calls), 1)
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient([failed_job("first-turn")])
            with self.assertRaisesRegex(runner.DeepThinkError, "first-turn"):
                runner.run_turn(
                    client,
                    root=root,
                    project="new",
                    prompt="Question.",
                    deployment="gpt-6-astra",
                    max_attempts=1,
                    recover_service_errors=True,
                )
            self.assertEqual(len(client.responses.calls), 1)


if __name__ == "__main__":
    unittest.main()
