import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_deep_think import (
    FakeClient,
    FakeResponse,
    load_module,
    make_status_error,
    make_validation_error,
)

TESTS_DIR = Path(__file__).parent
NO_SLEEP = {"sleep": lambda _delay: None, "random_value": lambda: 0}


def journal_records(project_dir):
    path = Path(project_dir) / "requests" / "journal.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def events(project_dir):
    return [record["event"] for record in journal_records(project_dir)]


def last_record(project_dir, event):
    matches = [r for r in journal_records(project_dir) if r["event"] == event]
    return matches[-1] if matches else None


def transport_error(cause):
    import httpx
    import openai

    request = httpx.Request("POST", "https://example.test/openai/v1/responses")
    wrapper = (
        openai.APITimeoutError(request=request)
        if isinstance(cause, httpx.TimeoutException)
        else openai.APIConnectionError(request=request)
    )
    wrapper.__cause__ = cause
    return wrapper


def httpx_error(name):
    import httpx

    request = httpx.Request("POST", "https://example.test/openai/v1/responses")
    return getattr(httpx, name)("transport failure", request=request)


def dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=60)
    return process.pid


def write_lock(project_dir, owner):
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / ".deep-think.lock"
    path.write_text(
        owner if isinstance(owner, str) else json.dumps(owner), encoding="utf-8"
    )
    return path


class CancelClient(FakeClient):
    def __init__(self, responses=(), retrieved=None, cancelled=None):
        super().__init__(list(responses), retrieved)
        self.cancel_calls = []
        self.cancelled = list(cancelled or [])
        self.responses.cancel = self._cancel

    def _cancel(self, response_id):
        self.cancel_calls.append(response_id)
        result = self.cancelled.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.runner = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = "recovery"
        self.project_dir = self.root / self.project

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self):
        self.runner.run_turn(
            FakeClient([FakeResponse("resp-seed", "Seed answer.")]),
            root=self.root,
            project=self.project,
            prompt="Seed question.",
            deployment="gpt-6-astra",
            **NO_SLEEP,
        )

    def ask(self, client, prompt="Continue.", **options):
        return self.runner.run_turn(
            client,
            root=self.root,
            project=self.project,
            prompt=prompt,
            deployment="gpt-6-astra",
            **{**NO_SLEEP, **options},
        )

    def resume(self, client, **options):
        return self.runner.resume_turn(
            lambda _deployment: client,
            root=self.root,
            project=self.project,
            **{**NO_SLEEP, **options},
        )

    def status(self):
        return self.runner.project_status(self.root, self.project)

    def state(self):
        return json.loads((self.project_dir / "state.json").read_text("utf-8"))

    def start_active_job(self, prompt="Continue."):
        """Leave an accepted job outstanding by stopping polling on a 401."""
        client = FakeClient(
            [FakeResponse("resp-job", "", status="queued", incomplete_reason=None)],
            [make_status_error(401, "unauthorized")],
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "resp-job"):
            self.ask(client, prompt)
        return client


CRASH_SCRIPT = r"""
import os
import sys
import types
from pathlib import Path

# Importing the real SDK costs tens of seconds per uncached process on some
# machines. The crash scenarios only need its exception classes.
openai = types.ModuleType("openai")


class APIError(Exception):
    pass


class APIConnectionError(APIError):
    pass


class APIResponseValidationError(APIError):
    pass


class APIStatusError(APIError):
    def __init__(self, status_code, code):
        super().__init__(code)
        self.status_code = status_code
        self.body = {{"error": {{"code": code, "message": code}}}}
        self.response = types.SimpleNamespace(headers={{}})


openai.APIConnectionError = APIConnectionError
openai.APIResponseValidationError = APIResponseValidationError
openai.APIStatusError = APIStatusError
sys.modules["openai"] = openai

sys.path.insert(0, {tests_dir!r})
from test_deep_think import FakeClient, FakeResponse, load_module

runner = load_module()
root = Path({root!r})
point = {point!r}
marker = root / "server-accepted.txt"


def crash(*_args, **_kwargs):
    sys.stdout.flush()
    os._exit(17)


class AcceptThenCrash(FakeClient):
    def __init__(self):
        super().__init__([])
        self.responses.create = self.create

    def create(self, **_request):
        marker.write_text("accepted", encoding="utf-8")
        crash()


class CrashOnSecondPoll(FakeClient):
    def __init__(self, responses):
        super().__init__(responses)
        self.polls = 0
        self.responses.retrieve = self.retrieve

    def retrieve(self, response_id, **_options):
        self.polls += 1
        if self.polls > 1:
            crash()
        return FakeResponse(response_id, "", status="in_progress", incomplete_reason=None)


client = FakeClient([FakeResponse("resp-job", "", status="queued", incomplete_reason=None)])
if point == "before_submission":
    runner._request_for_deployment = crash
elif point == "after_acceptance":
    client = AcceptThenCrash()
elif point == "after_id_persistence":
    runner._poll_background_response = crash
elif point == "during_polling":
    clients = {{
        "primary": FakeClient([APIStatusError(429, "rate_limit_exceeded")]),
        "backup": CrashOnSecondPoll(
            [FakeResponse("resp-backup", "", status="queued", incomplete_reason=None)]
        ),
    }}
    client = runner.create_routed_client(
        "primary",
        "gpt-6-astra",
        backup_endpoint="backup",
        client_factory=clients.__getitem__,
    )
elif point == "before_commit":
    client = FakeClient([FakeResponse("resp-done", "Cached answer.")])
    runner._write_pending_state = crash

runner.run_turn(
    client,
    root=root,
    project="recovery",
    prompt="Continue.",
    deployment="gpt-6-astra",
    sleep=lambda _delay: None,
    random_value=lambda: 0,
)
os._exit(0)
"""


class CrashRecoveryTests(ProjectFixture):
    def crash_at(self, point):
        script = self.root / f"crash-{point}.py"
        script.write_text(
            CRASH_SCRIPT.format(
                tests_dir=str(TESTS_DIR), root=str(self.root), point=point
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, "-B", str(script)],
            capture_output=True,
            check=False,
            text=True,
            timeout=180,
        )
        self.assertEqual(completed.returncode, 17, completed.stderr)
        lock = self.project_dir / ".deep-think.lock"
        self.assertTrue(lock.exists(), "a crash must leave the writer lock behind")
        self.assertEqual(self.status()["writer_lock"]["state"], "stale")

    def test_crash_before_submission_resubmits_safely(self):
        self.seed()
        self.crash_at("before_submission")
        recorded = events(self.project_dir)
        crashed_turn = recorded[len(recorded) - recorded[::-1].index("turn_started") :]
        self.assertNotIn("submitting", crashed_turn)
        self.assertTrue(self.status()["can_submit_new_request"])

        client = FakeClient([FakeResponse("resp-new", "Second answer.")])
        result = self.ask(client)

        self.assertEqual(result.text, "Second answer.")
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(self.state()["turn"], 2)
        self.assertIn("stale_lock_recovered", events(self.project_dir))
        self.assertNotIn("submission_unknown", events(self.project_dir))

    def test_crash_after_server_acceptance_requires_reconciliation(self):
        self.seed()
        self.crash_at("after_acceptance")
        self.assertTrue((self.root / "server-accepted.txt").exists())
        report = self.status()
        self.assertFalse(report["can_submit_new_request"])
        [unknown] = report["outstanding"]
        self.assertEqual(unknown["state"], "unknown")

        client = FakeClient([FakeResponse("resp-duplicate", "Must not run.")])
        for retry in (lambda: self.ask(client), lambda: self.resume(client)):
            with self.assertRaises(self.runner.SubmissionUnknownError):
                retry()
        self.assertEqual(client.responses.calls, [])
        self.assertIn("submission_unknown", events(self.project_dir))

        self.runner.reconcile_project(
            lambda _resource: FakeClient(
                [], [FakeResponse("resp-found", "", status="in_progress")]
            ),
            root=self.root,
            project=self.project,
            attempt_id=unknown["attempt_id"],
            response_id="resp-found",
            reason="Found in Azure request logs.",
        )
        recovered = FakeClient([], [FakeResponse("resp-found", "Recovered answer.")])
        result = self.resume(recovered)

        self.assertEqual(result.text, "Recovered answer.")
        self.assertEqual(recovered.responses.calls, [])
        self.assertEqual(recovered.responses.retrieve_calls, ["resp-found"])
        self.assertEqual(self.state()["last_response_id"], "resp-found")

    def test_confirmed_unknown_submission_allows_explicit_retry(self):
        self.seed()
        self.crash_at("after_acceptance")
        with self.assertRaisesRegex(self.runner.DeepThinkError, "reason"):
            self.runner.reconcile_project(
                lambda _resource: FakeClient([]),
                root=self.root,
                project=self.project,
                confirm_no_remote_job=True,
            )
        self.runner.reconcile_project(
            lambda _resource: FakeClient([]),
            root=self.root,
            project=self.project,
            confirm_no_remote_job=True,
            reason="Azure metrics show no request in that window.",
        )
        client = FakeClient([FakeResponse("resp-retry", "Retried answer.")])
        self.assertEqual(self.ask(client).text, "Retried answer.")
        self.assertEqual(len(client.responses.calls), 1)
        confirmation = last_record(self.project_dir, "operator_confirmed_no_remote_job")
        self.assertEqual(
            confirmation["reason"], "Azure metrics show no request in that window."
        )

    def test_crash_after_id_persistence_resumes_original_job(self):
        self.seed()
        self.crash_at("after_id_persistence")
        accepted = last_record(self.project_dir, "accepted")
        self.assertEqual(accepted["response_id"], "resp-job")
        [active] = self.status()["outstanding"]
        self.assertEqual(active["state"], "active")

        other_prompt = FakeClient([FakeResponse("resp-other", "Must not run.")])
        with self.assertRaisesRegex(self.runner.RemoteStateError, "resp-job"):
            self.ask(other_prompt, prompt="A different question.")
        self.assertEqual(other_prompt.responses.calls, [])

        client = FakeClient([], [FakeResponse("resp-job", "Recovered answer.")])
        result = self.resume(client)

        self.assertEqual(result.text, "Recovered answer.")
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(client.responses.retrieve_calls, ["resp-job"])
        self.assertEqual(self.state()["turn"], 2)
        self.assertTrue(self.status()["can_submit_new_request"])

    def test_crash_during_polling_resumes_on_original_resource(self):
        self.seed()
        self.crash_at("during_polling")
        [active] = self.status()["outstanding"]
        self.assertEqual(active["resource"], "backup")
        self.assertEqual(active["response_id"], "resp-backup")

        clients = {
            "primary": FakeClient([FakeResponse("resp-wrong", "Must not run.")]),
            "backup": FakeClient(
                [], [FakeResponse("resp-backup", "Recovered from backup.")]
            ),
        }
        result = self.runner.resume_turn(
            lambda deployment: self.runner.create_routed_client(
                "primary",
                deployment,
                backup_endpoint="backup",
                client_factory=clients.__getitem__,
            ),
            root=self.root,
            project=self.project,
            **NO_SLEEP,
        )

        self.assertEqual(result.text, "Recovered from backup.")
        self.assertEqual(clients["primary"].responses.calls, [])
        self.assertEqual(clients["primary"].responses.retrieve_calls, [])
        self.assertEqual(clients["backup"].responses.calls, [])
        self.assertEqual(clients["backup"].responses.retrieve_calls, ["resp-backup"])

    def test_crash_before_commit_uses_cached_answer_without_network(self):
        self.seed()
        self.crash_at("before_commit")
        completed = last_record(self.project_dir, "completed")
        payload = self.project_dir / "requests" / completed["payload"]
        self.assertTrue(payload.exists())
        self.assertEqual(self.status()["recoverable"][0]["response_id"], "resp-done")

        client = FakeClient([])
        result = self.ask(client)

        self.assertEqual(result.text, "Cached answer.")
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(client.responses.retrieve_calls, [])
        self.assertEqual(self.state()["last_response_id"], "resp-done")
        self.assertEqual(self.state()["turn"], 2)
        self.assertFalse(payload.exists())
        transcript = (self.project_dir / "0001-transcript.md").read_text("utf-8")
        self.assertIn("Cached answer.", transcript)


class JournalDurabilityTests(ProjectFixture):
    def test_intent_precedes_submission_and_id_precedes_polling(self):
        project_dir = self.project_dir
        observed = {}

        class InspectingClient(FakeClient):
            def __init__(self):
                super().__init__([])
                self.responses.create = self.create
                self.responses.retrieve = self.retrieve

            def create(self, **request):
                observed["create"] = journal_records(project_dir)[-1]
                observed["model"] = request["model"]
                return FakeResponse("resp-new", "", status="queued")

            def retrieve(self, response_id, **_options):
                observed["retrieve"] = journal_records(project_dir)[-1]
                return FakeResponse(response_id, "Durable answer.")

        router = self.runner.create_routed_client(
            "https://primary.example/openai/v1/",
            "gpt-6-astra",
            client_factory=lambda _resource: InspectingClient(),
        )
        self.ask(router)

        intent = observed["create"]
        self.assertEqual(intent["event"], "submitting")
        self.assertEqual(intent["deployment"], "gpt-6-astra")
        self.assertEqual(intent["resource"], "https://primary.example/openai/v1/")
        self.assertEqual(intent["target_index"], 1)
        for key in ("request_sha256", "logical_sha256"):
            self.assertRegex(intent[key], r"^[0-9a-f]{64}$")
        self.assertGreater(intent["request_bytes"], 0)
        self.assertEqual(observed["retrieve"]["event"], "accepted")
        self.assertEqual(observed["retrieve"]["response_id"], "resp-new")
        self.assertEqual(events(project_dir)[-1], "turn_committed", events(project_dir))

    def test_unwritable_journal_prevents_submission(self):
        self.project_dir.mkdir()
        (self.project_dir / "requests").write_text("not a directory", "utf-8")
        client = FakeClient([FakeResponse("resp-unused", "Must not run.")])
        with self.assertRaisesRegex(self.runner.DeepThinkError, "journal"):
            self.ask(client)
        self.assertEqual(client.responses.calls, [])

    def test_torn_tail_is_ignored_then_repaired(self):
        self.seed()
        path = self.project_dir / "requests" / "journal.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"event": "accep')
        self.assertTrue(self.status()["journal"]["torn_tail"])
        self.ask(FakeClient([FakeResponse("resp-next", "Next answer.")]))
        self.assertIn("torn_record_discarded", events(self.project_dir))
        self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_corrupt_complete_record_blocks_submission(self):
        self.seed()
        path = self.project_dir / "requests" / "journal.jsonl"
        with path.open("ab") as stream:
            stream.write(b"not json\n")
        client = FakeClient([FakeResponse("resp-unused", "Must not run.")])
        with self.assertRaisesRegex(self.runner.DeepThinkError, "corrupt"):
            self.ask(client)
        self.assertEqual(client.responses.calls, [])


class SubmissionClassificationTests(ProjectFixture):
    def make_router(self, primary_results, backup_results=()):
        self.clients = {
            "primary": FakeClient(list(primary_results)),
            "backup": FakeClient(list(backup_results)),
        }
        return self.runner.create_routed_client(
            "primary",
            "gpt-6-astra",
            backup_endpoint="backup",
            client_factory=self.clients.__getitem__,
        )

    def test_failures_before_sending_fail_over(self):
        for name in ("ConnectError", "ConnectTimeout", "PoolTimeout"):
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                router = self.make_router(
                    [transport_error(httpx_error(name))],
                    [FakeResponse("resp-backup", "Backup answer.")],
                )
                self.assertEqual(self.ask(router).text, "Backup answer.")
                self.assertEqual(len(self.clients["backup"].responses.calls), 1)
                self.assertIn("not_sent", events(self.project_dir))

    def test_ambiguous_failures_are_never_resubmitted(self):
        failures = {
            "ReadTimeout": transport_error(httpx_error("ReadTimeout")),
            "RemoteProtocolError": transport_error(httpx_error("RemoteProtocolError")),
            "uncaused connection error": transport_error(None),
            "gateway timeout": make_status_error(504, "gateway_timeout"),
            "bad gateway": make_status_error(502, "bad_gateway"),
            "malformed creation without ID": make_validation_error(),
        }
        for label, failure in failures.items():
            with self.subTest(failure=label):
                self.tearDown()
                self.setUp()
                router = self.make_router(
                    [failure], [FakeResponse("resp-backup", "Must not run.")]
                )
                with self.assertRaises(self.runner.SubmissionUnknownError):
                    self.ask(router)
                self.assertEqual(self.clients["backup"].responses.calls, [])
                self.assertIn("submission_unknown", events(self.project_dir))

                repeat = FakeClient([FakeResponse("resp-repeat", "Must not run.")])
                with self.assertRaises(self.runner.SubmissionUnknownError):
                    self.ask(repeat)
                self.assertEqual(repeat.responses.calls, [])

    def test_definitive_http_rejection_still_fails_over(self):
        router = self.make_router(
            [make_status_error(500, "server_error")],
            [FakeResponse("resp-backup", "Backup answer.")],
        )
        self.assertEqual(self.ask(router).text, "Backup answer.")
        self.assertIn("rejected", events(self.project_dir))

    def test_malformed_creation_with_id_polls_that_job(self):
        import httpx
        import openai

        request = httpx.Request("POST", "https://example.test/openai/v1/responses")
        malformed = openai.APIResponseValidationError(
            response=httpx.Response(200, request=request),
            body={"id": "resp-partial", "status": 7},
            message="Malformed Azure response.",
        )
        client = FakeClient([malformed], [FakeResponse("resp-partial", "Polled.")])
        self.assertEqual(self.ask(client).text, "Polled.")
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(client.responses.retrieve_calls, ["resp-partial"])

    def test_interrupt_during_submission_is_recorded_as_unknown(self):
        client = FakeClient([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.ask(client)
        self.assertEqual(
            last_record(self.project_dir, "submission_unknown")["reason"],
            "interrupted by KeyboardInterrupt",
        )
        self.assertFalse(self.status()["can_submit_new_request"])

    def test_authentication_failure_before_sending_is_not_retried(self):
        from azure.core.exceptions import ClientAuthenticationError

        router = self.make_router(
            [ClientAuthenticationError("az login required")],
            [FakeResponse("resp-backup", "Must not run.")],
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "authentication"):
            self.ask(router)
        self.assertEqual(self.clients["backup"].responses.calls, [])
        self.assertIn("not_sent", events(self.project_dir))
        self.assertTrue(self.status()["can_submit_new_request"])


class WriterLockTests(ProjectFixture):
    def owner(self, **fields):
        return {
            "schema": "deep-think-lock/v2",
            "lock_id": "0" * 32,
            "pid": dead_pid(),
            "host": self.runner.socket.gethostname(),
            "process_started_at": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "command": "ask",
            **fields,
        }

    def test_process_liveness(self):
        alive, started = self.runner._process_liveness(os.getpid())
        self.assertTrue(alive)
        if os.name == "nt":
            self.assertIsNotNone(started)
        self.assertFalse(self.runner._process_liveness(dead_pid())[0])

    def test_live_lock_blocks_writers_but_not_status(self):
        self.seed()
        lock = write_lock(self.project_dir, self.owner(pid=os.getpid()))
        before = lock.read_bytes()
        client = FakeClient([FakeResponse("resp-unused", "Must not run.")])
        with self.assertRaisesRegex(self.runner.ProjectLockedError, "already locked"):
            self.ask(client)
        with self.assertRaisesRegex(self.runner.ProjectLockedError, "already locked"):
            self.resume(client)
        report = self.status()
        self.assertEqual(report["writer_lock"]["state"], "live")
        self.assertFalse(report["can_submit_new_request"])
        self.assertEqual(lock.read_bytes(), before)
        self.assertEqual(client.responses.calls, [])

    def test_dead_journaled_writer_is_replaced_automatically(self):
        self.seed()
        write_lock(self.project_dir, self.owner())
        self.ask(FakeClient([FakeResponse("resp-next", "Next answer.")]))
        recovered = last_record(self.project_dir, "stale_lock_recovered")
        self.assertEqual(recovered["previous_owner"]["lock_id"], "0" * 32)
        self.assertNotIn("legacy_writer_unknown", events(self.project_dir))
        self.assertFalse((self.project_dir / ".deep-think.lock").exists())

    def test_reused_pid_is_not_mistaken_for_live_writer(self):
        self.seed()
        write_lock(
            self.project_dir,
            self.owner(
                pid=os.getppid(),
                process_started_at="2000-01-01T00:00:00+00:00",
                created_at="2000-01-01T00:00:00+00:00",
            ),
        )
        self.assertEqual(self.status()["writer_lock"]["state"], "stale")
        self.ask(FakeClient([FakeResponse("resp-next", "Next answer.")]))

    def test_dead_pre_journal_writer_requires_explicit_reconciliation(self):
        self.seed()
        write_lock(
            self.project_dir,
            {"pid": dead_pid(), "created_at": "2026-09-17T07:53:21+00:00"},
        )
        report = self.status()
        self.assertEqual(report["writer_lock"]["state"], "stale")
        self.assertTrue(report["writer_lock"]["legacy_format"])
        self.assertFalse(report["can_submit_new_request"])

        client = FakeClient([FakeResponse("resp-unused", "Must not run.")])
        with self.assertRaisesRegex(self.runner.RemoteStateError, "pre-journal"):
            self.ask(client)
        self.assertEqual(client.responses.calls, [])
        self.assertIn("legacy_writer_unknown", events(self.project_dir))
        self.assertFalse((self.project_dir / ".deep-think.lock").exists())

        observed = CancelClient(
            retrieved=[FakeResponse("resp-september", "", status="failed")]
        )
        self.runner.reconcile_project(
            lambda resource: observed,
            root=self.root,
            project=self.project,
            response_id="resp-september",
            endpoint="https://primary.example/openai/v1/",
            reason="ID found in Azure telemetry.",
        )
        self.assertIn("legacy_resolved", events(self.project_dir))
        self.ask(FakeClient([FakeResponse("resp-next", "Next answer.")]))

    def test_unverifiable_lock_requires_explicit_release(self):
        self.seed()
        write_lock(self.project_dir, self.owner(host="another-host", pid=4321))
        client = FakeClient([FakeResponse("resp-unused", "Must not run.")])
        with self.assertRaisesRegex(self.runner.ProjectLockedError, "release-lock"):
            self.ask(client)
        self.assertEqual(self.status()["writer_lock"]["state"], "unverifiable")
        self.runner.reconcile_project(
            lambda _resource: FakeClient([]),
            root=self.root,
            project=self.project,
            release_lock=True,
        )
        self.assertFalse((self.project_dir / ".deep-think.lock").exists())
        self.ask(FakeClient([FakeResponse("resp-next", "Next answer.")]))

    def test_empty_lock_is_busy_until_grace_period_expires(self):
        self.seed()
        lock = write_lock(self.project_dir, "")
        client = FakeClient([FakeResponse("resp-next", "Next answer.")])
        with self.assertRaisesRegex(self.runner.ProjectLockedError, "already locked"):
            self.ask(client)
        old = time.time() - 600
        os.utime(lock, (old, old))
        self.assertEqual(self.ask(client).text, "Next answer.")


class RecoveryCommandTests(ProjectFixture):
    def run_cli(self, *arguments, client_factory=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = self.runner.main(
            [arguments[0], "--project", self.project, "--root", str(self.root)]
            + list(arguments[1:]),
            client_factory=client_factory or (lambda _resource: FakeClient([])),
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_reports_clean_project(self):
        self.seed()
        code, output, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertTrue(report["can_submit_new_request"])
        self.assertEqual(report["writer_lock"]["state"], "none")
        self.assertEqual(report["committed"]["turn"], 1)
        self.assertEqual(report["outstanding"], [])

    def test_cancel_stops_active_job_and_unblocks_new_turns(self):
        self.seed()
        self.start_active_job()
        cancelled = CancelClient(
            cancelled=[
                FakeResponse("resp-job", "", status="cancelled", incomplete_reason=None)
            ]
        )
        code, output, stderr = self.run_cli(
            "cancel", client_factory=lambda _resource: cancelled
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(cancelled.cancel_calls, ["resp-job"])
        self.assertEqual(json.loads(output)["results"][0]["status"], "cancelled")
        self.assertTrue(self.status()["can_submit_new_request"])
        client = FakeClient([FakeResponse("resp-next", "New topic.")])
        self.assertEqual(self.ask(client, prompt="New topic.").text, "New topic.")

    def test_cancel_after_completion_preserves_the_finished_result(self):
        self.seed()
        self.start_active_job()
        finished = CancelClient(
            retrieved=[FakeResponse("resp-job", "Finished before cancel.")],
            cancelled=[
                make_status_error(
                    400,
                    "invalid_request",
                    message="Cannot cancel a completed response.",
                )
            ],
        )
        code, output, stderr = self.run_cli(
            "cancel", client_factory=lambda _resource: finished
        )
        self.assertEqual(code, 0, stderr)
        report = json.loads(output)
        self.assertEqual(report["results"][0]["status"], "completed")
        self.assertEqual(report["status"]["outstanding"], [])
        self.assertEqual(report["status"]["recoverable"][0]["response_id"], "resp-job")

        offline = FakeClient([])
        code, output, stderr = self.run_cli(
            "resume", client_factory=lambda _resource: offline
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, "Finished before cancel.\n")
        self.assertEqual(offline.responses.calls, [])

    def test_reconcile_caches_completed_job_then_resume_commits_it(self):
        self.seed()
        self.start_active_job()
        observed = FakeClient([], [FakeResponse("resp-job", "Finished remotely.")])
        code, output, stderr = self.run_cli(
            "reconcile", client_factory=lambda _resource: observed
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(output)["status"]["recoverable"][0]["state"], "terminal"
        )

        offline = FakeClient([])
        code, output, stderr = self.run_cli(
            "resume", client_factory=lambda _resource: offline
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(output, "Finished remotely.\n")
        self.assertEqual(offline.responses.calls, [])
        self.assertEqual(offline.responses.retrieve_calls, [])

    def test_reconcile_marks_expired_job_unavailable(self):
        self.seed()
        self.start_active_job()
        expired = FakeClient([], [make_status_error(404, "not_found")])
        self.run_cli("reconcile", client_factory=lambda _resource: expired)
        self.assertIn("remote_unavailable", events(self.project_dir))
        self.assertTrue(self.status()["can_submit_new_request"])

    def test_abandon_requires_reason_and_no_remote_work(self):
        self.seed()
        self.start_active_job()
        code, _, stderr = self.run_cli("reconcile", "--abandon-turn")
        self.assertEqual(code, 2)
        self.assertIn("--reason", stderr)
        still_running = FakeClient(
            [], [FakeResponse("resp-job", "", status="in_progress")]
        )
        code, _, stderr = self.run_cli(
            "reconcile",
            "--abandon-turn",
            "--reason",
            "Superseded.",
            client_factory=lambda _resource: still_running,
        )
        self.assertEqual(code, 2)
        self.assertIn("cancel", stderr)

    def test_resume_without_unfinished_turn_is_explicit(self):
        self.seed()
        code, _, stderr = self.run_cli("resume")
        self.assertEqual(code, 2)
        self.assertIn("nothing to resume", stderr)

    def test_failed_ask_points_to_status(self):
        self.seed()
        self.start_active_job()
        code, _, stderr = self.run_cli(
            "ask",
            "--prompt",
            "Different question.",
            "--endpoint",
            "https://primary.example/openai/v1/",
            client_factory=lambda _resource: FakeClient([]),
        )
        self.assertEqual(code, 2)
        self.assertIn("resp-job", stderr)
        self.assertIn("status --project recovery", stderr)


class ReviewRegressionTests(ProjectFixture):
    def seed_large(self):
        self.runner.run_turn(
            FakeClient([FakeResponse("resp-seed", "Seed.", input_tokens=800_000)]),
            root=self.root,
            project=self.project,
            prompt="Seed question.",
            deployment="gpt-6-astra",
            **NO_SLEEP,
        )

    def test_resume_replays_recorded_failure_to_reach_running_job(self):
        self.seed_large()
        crashed = FakeClient(
            [
                FakeResponse("resp-short", "", status="incomplete"),
                FakeResponse("resp-summary", "Continuation summary."),
                FakeResponse("resp-after", "", status="queued", incomplete_reason=None),
            ],
            [make_status_error(401, "unauthorized")],
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "resp-after"):
            self.ask(crashed)

        client = FakeClient([], [FakeResponse("resp-after", "Answer after rollover.")])
        result = self.resume(client)

        self.assertEqual(result.text, "Answer after rollover.")
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(client.responses.retrieve_calls, ["resp-after"])
        self.assertEqual(self.state()["volume"], 2)

    def test_new_submission_is_refused_while_turn_has_unreached_running_job(self):
        with self.assertRaises(self.runner.DeepThinkError):
            self.runner.run_turn(
                FakeClient(
                    [
                        FakeResponse(
                            "resp-job", "", status="queued", incomplete_reason=None
                        )
                    ],
                    [make_status_error(401, "unauthorized")],
                ),
                root=self.root,
                project=self.project,
                prompt="Continue.",
                deployment="gpt-6-astra",
                rollover_tokens=100_000,
                **NO_SLEEP,
            )
        # Same prompt, different default budget: a different request fingerprint.
        client = FakeClient([FakeResponse("resp-duplicate", "Must not run.")])
        with self.assertRaisesRegex(self.runner.RemoteStateError, "resp-job"):
            self.ask(client)
        self.assertEqual(client.responses.calls, [])

    def test_transient_terminal_failure_is_retried_on_rerun(self):
        self.seed()
        failed = FakeResponse(
            "resp-failed",
            "",
            status="failed",
            incomplete_reason=None,
            error=SimpleNamespace(code="server_error", message="Unavailable."),
        )
        with self.assertRaises(self.runner.TerminalServiceError):
            self.ask(FakeClient([failed]), max_attempts=1)
        client = FakeClient([FakeResponse("resp-retry", "Retried answer.")])
        self.assertEqual(self.ask(client, max_attempts=1).text, "Retried answer.")
        self.assertEqual(len(client.responses.calls), 1)

    def test_attached_id_not_found_keeps_submission_unknown(self):
        self.seed()
        with self.assertRaises(self.runner.SubmissionUnknownError):
            self.ask(FakeClient([make_status_error(504, "gateway_timeout")]))
        [unknown] = self.status()["outstanding"]
        self.runner.reconcile_project(
            lambda _resource: FakeClient([], [make_status_error(404, "not_found")]),
            root=self.root,
            project=self.project,
            attempt_id=unknown["attempt_id"],
            response_id="resp-typo",
            reason="Possibly mistyped.",
        )
        report = self.status()
        self.assertFalse(report["can_submit_new_request"])
        self.assertEqual(report["outstanding"][0]["state"], "unknown")
        self.assertIn("attachment_rejected", events(self.project_dir))

    def test_attached_id_cannot_override_recorded_resource(self):
        self.seed()
        router = self.runner.create_routed_client(
            "primary",
            "gpt-6-astra",
            client_factory=lambda _resource: FakeClient(
                [make_status_error(504, "gateway_timeout")]
            ),
        )
        with self.assertRaises(self.runner.SubmissionUnknownError):
            self.ask(router)
        [unknown] = self.status()["outstanding"]
        with self.assertRaisesRegex(self.runner.DeepThinkError, "recorded resource"):
            self.runner.reconcile_project(
                lambda _resource: FakeClient([]),
                root=self.root,
                project=self.project,
                attempt_id=unknown["attempt_id"],
                response_id="resp-found",
                endpoint="https://wrong.example/openai/v1/",
                reason="Found in logs.",
            )

    def test_pre_journal_lock_evidence_survives_failed_journal_write(self):
        self.project_dir.mkdir()
        write_lock(
            self.project_dir,
            {"pid": dead_pid(), "created_at": "2026-09-17T07:53:21+00:00"},
        )
        blocker = self.project_dir / "requests"
        blocker.write_text("not a directory", "utf-8")
        client = FakeClient([FakeResponse("resp-unused", "Must not run.")])
        with self.assertRaisesRegex(self.runner.DeepThinkError, "journal"):
            self.ask(client)
        blocker.unlink()

        report = self.status()
        self.assertFalse(report["can_submit_new_request"])
        self.assertTrue(report["unresolved_unknown"])
        with self.assertRaisesRegex(self.runner.RemoteStateError, "pre-journal"):
            self.ask(client)
        self.assertEqual(client.responses.calls, [])

    def test_authentication_failure_in_recovery_commands_is_clean(self):
        from azure.core.exceptions import ClientAuthenticationError

        self.seed()
        self.start_active_job()
        broken = CancelClient(
            retrieved=[ClientAuthenticationError("az login required")] * 2,
            cancelled=[ClientAuthenticationError("az login required")],
        )
        for command in ("reconcile", "cancel"):
            stdout, stderr = io.StringIO(), io.StringIO()
            code = self.runner.main(
                [command, "--project", self.project, "--root", str(self.root)],
                client_factory=lambda _resource: broken,
                stdout=stdout,
                stderr=stderr,
            )
            # The failure is reported for the job; nothing else is changed.
            self.assertEqual(code, 0, command)
            self.assertIn("authentication failed", stdout.getvalue())
            self.assertIn("no remote state was changed", stdout.getvalue())
        self.assertEqual(
            [item["state"] for item in self.status()["outstanding"]], ["active"]
        )

    def seed_small_budget(self):
        self.runner.run_turn(
            FakeClient([FakeResponse("resp-seed", "Seed.", input_tokens=800)]),
            root=self.root,
            project=self.project,
            prompt="Seed question.",
            deployment="gpt-6-astra",
            rollover_tokens=1000,
            **NO_SLEEP,
        )

    def test_prompt_that_never_fits_fails_before_paying_for_rollover(self):
        self.seed_small_budget()
        client = FakeClient([FakeResponse("resp-summary", "Must not run.")])
        with self.assertRaisesRegex(self.runner.DeepThinkError, "too little room"):
            self.ask(client, prompt="x" * 900)
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(self.state()["volume"], 1)

    def test_proactive_rollover_is_kept_when_prompt_is_still_too_large(self):
        self.seed_small_budget()
        summary = FakeClient([FakeResponse("resp-summary", "S" * 300)])
        with self.assertRaisesRegex(self.runner.DeepThinkError, "too little room"):
            self.ask(summary, prompt="x" * 300)
        self.assertEqual(len(summary.responses.calls), 1)
        self.assertEqual(self.state()["volume"], 2)
        self.assertTrue(self.status()["can_submit_new_request"])

        retry = FakeClient([FakeResponse("resp-again", "Must not run.")])
        with self.assertRaisesRegex(self.runner.DeepThinkError, "too little room"):
            self.ask(retry, prompt="x" * 300)
        self.assertEqual(retry.responses.calls, [])
        self.assertEqual(self.state()["volume"], 2)

        client = FakeClient([FakeResponse("resp-answer", "Smaller answer.")])
        self.assertEqual(self.ask(client, prompt="Go.").text, "Smaller answer.")
        self.assertEqual(len(client.responses.calls), 1)

    def test_retried_service_error_is_not_replayed_after_a_different_failure(self):
        self.seed()
        failed = FakeResponse(
            "resp-failed",
            "",
            status="failed",
            incomplete_reason=None,
            error=SimpleNamespace(code="server_error", message="Unavailable."),
        )
        with self.assertRaises(self.runner.TerminalServiceError):
            self.ask(FakeClient([failed]), max_attempts=1)
        with self.assertRaisesRegex(self.runner.DeepThinkError, "without retry"):
            self.ask(
                FakeClient([make_status_error(400, "invalid_request")]),
                max_attempts=1,
            )
        client = FakeClient([FakeResponse("resp-third", "Third time.")])
        self.assertEqual(self.ask(client, max_attempts=1).text, "Third time.")
        self.assertEqual(len(client.responses.calls), 1)

    def test_reactive_rollover_allowance_survives_resume(self):
        self.seed()
        first = FakeClient(
            [
                make_status_error(400, "context_length_exceeded"),
                FakeResponse("resp-summary", "Summary."),
                make_status_error(400, "context_length_exceeded"),
            ]
        )
        with self.assertRaises(self.runner.ContextLimitError):
            self.ask(first, recover_service_errors=True)
        self.assertEqual(self.state()["volume"], 2)

        retry = FakeClient([FakeResponse("resp-again", "Must not run.")])
        with self.assertRaisesRegex(self.runner.DeepThinkError, "after rollover"):
            self.ask(retry, recover_service_errors=True)
        self.assertEqual(retry.responses.calls, [])
        self.assertEqual(self.state()["volume"], 2)

    def test_failed_answer_after_rollover_keeps_the_rollover(self):
        self.seed()
        first = FakeClient(
            [
                make_status_error(400, "context_length_exceeded"),
                FakeResponse("resp-summary", "Summary."),
                make_status_error(400, "context_length_exceeded"),
            ]
        )
        with self.assertRaises(self.runner.ContextLimitError):
            self.ask(first)
        self.assertEqual(self.state()["volume"], 2)
        self.assertTrue(self.status()["can_submit_new_request"])

        retry = FakeClient([FakeResponse("resp-again", "Must not run.")])
        with self.assertRaisesRegex(self.runner.DeepThinkError, "after rollover"):
            self.ask(retry)
        self.assertEqual(retry.responses.calls, [])

        client = FakeClient([FakeResponse("resp-narrow", "Narrow answer.")])
        self.assertEqual(self.ask(client, prompt="Narrower.").text, "Narrow answer.")
        self.assertEqual(len(client.responses.calls), 1)

    def test_service_error_after_resumed_rollover_does_not_recover_again(self):
        self.runner.run_turn(
            FakeClient([FakeResponse("resp-seed", "Seed.", input_tokens=880_000)]),
            root=self.root,
            project=self.project,
            prompt="Seed question.",
            deployment="gpt-6-astra",
            **NO_SLEEP,
        )

        def failed(response_id):
            return FakeResponse(
                response_id,
                "",
                status="failed",
                incomplete_reason=None,
                error=SimpleNamespace(code="server_error", message="Unavailable."),
            )

        first = FakeClient([FakeResponse("resp-summary", "Summary."), failed("a")])
        with self.assertRaises(self.runner.TerminalServiceError):
            self.ask(first, recover_service_errors=True, max_attempts=1)
        self.assertEqual(self.state()["volume"], 2)

        second = FakeClient([failed("b"), FakeResponse("resp-x", "Must not run.")])
        with self.assertRaises(self.runner.TerminalServiceError):
            self.ask(second, recover_service_errors=True, max_attempts=1)
        self.assertEqual(len(second.responses.calls), 1)
        self.assertEqual(self.state()["volume"], 2)


class JournalTrustTests(ProjectFixture):
    """A journal may arrive through a shared repository, so treat it as input."""

    def forge(self, **fields):
        record = {
            "schema": "deep-think-request-journal/v1",
            "recorded_at": "2026-10-04T00:00:00+00:00",
            "lock_id": None,
            **fields,
        }
        path = self.project_dir / "requests" / "journal.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def cli(self, *arguments, client_factory):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = self.runner.main(
            [arguments[0], "--project", self.project, "--root", str(self.root)]
            + list(arguments[1:]),
            client_factory=client_factory,
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_tampered_artifact_names_cannot_reach_outside_requests(self):
        for traversal in (True, False):
            self.tearDown()
            self.setUp()
            victim = self.root / "victim.txt"
            name = "../../victim.txt" if traversal else str(victim)
            with self.subTest(name=name):
                victim.write_text("keep", "utf-8")
                self.seed()
                self.forge(
                    event="turn_started",
                    turn_id="forged",
                    prompt_sha256="0" * 64,
                    prompt_file=name,
                    base_state_sha256=None,
                    deployment="gpt-6-astra",
                )
                self.forge(
                    event="submitting",
                    turn_id="forged",
                    attempt_id="forged-attempt",
                    logical_sha256="1" * 64,
                    deployment="gpt-6-astra",
                    resource=None,
                )
                self.forge(
                    event="completed",
                    turn_id="forged",
                    attempt_id="forged-attempt",
                    response_id="resp-forged",
                    payload=name,
                    payload_sha256="0" * 64,
                )
                client = FakeClient([FakeResponse("resp-unused", "Must not run.")])
                with self.assertRaisesRegex(
                    self.runner.DeepThinkError, "artifact name"
                ):
                    self.status()
                with self.assertRaisesRegex(
                    self.runner.DeepThinkError, "artifact name"
                ):
                    self.runner.reconcile_project(
                        lambda _resource: FakeClient([]),
                        root=self.root,
                        project=self.project,
                        abandon_turn=True,
                        reason="Cleanup.",
                    )
                with self.assertRaisesRegex(
                    self.runner.DeepThinkError, "artifact name"
                ):
                    self.ask(client)
                self.assertTrue(victim.exists())
                self.assertEqual(client.responses.calls, [])

    def test_artifact_paths_accept_only_bare_journal_names(self):
        journal = self.runner.RequestJournal(self.project_dir)
        for name in (
            "resp_0a1-B.json",
            "a" * 32 + ".prompt.txt",
            "b" * 32 + ".response.json",
        ):
            self.assertEqual(journal._artifact_path(name).parent, journal.directory)
        for name in (
            "../x.json",
            "..\\x.json",
            "C:\\x.json",
            "/x.json",
            "x/y.json",
            ".json",
            "",
            None,
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(self.runner.DeepThinkError, "artifact name"),
            ):
                journal._artifact_path(name)

    def test_cli_sends_credentials_only_to_configured_endpoints(self):
        self.seed()
        recorded = "https://unconfigured.example/openai/v1/"
        router = self.runner.create_routed_client(
            recorded,
            "gpt-6-astra",
            client_factory=lambda _resource: FakeClient(
                [FakeResponse("resp-job", "", status="queued", incomplete_reason=None)],
                [make_status_error(401, "unauthorized")],
            ),
        )
        with self.assertRaisesRegex(self.runner.DeepThinkError, "resp-job"):
            self.ask(router)
        requested = []

        def factory(resource):
            requested.append(resource)
            return CancelClient(
                retrieved=[FakeResponse("resp-job", "", status="in_progress")],
                cancelled=[
                    FakeResponse(
                        "resp-job", "", status="cancelled", incomplete_reason=None
                    )
                ],
            )

        configured = {
            "AZURE_OPENAI_GPT6_ENDPOINT": "https://primary.example/openai/v1/"
        }
        with mock.patch.dict("os.environ", configured, clear=True):
            # Recovery commands report the refusal per job; resume stops.
            code, output, stderr = self.cli("cancel", client_factory=factory)
            self.assertEqual(code, 0, stderr)
            [result] = json.loads(output)["results"]
            self.assertIn("not a configured endpoint", result["error"])
            code, output, stderr = self.cli("reconcile", client_factory=factory)
            self.assertEqual(code, 0, stderr)
            self.assertIn("not a configured endpoint", output)
            code, _, stderr = self.cli("resume", client_factory=factory)
            self.assertEqual(code, 2)
            self.assertIn("not a configured endpoint", stderr)
            self.assertNotIn(recorded, requested)

            code, output, stderr = self.cli(
                "cancel", "--endpoint", recorded, client_factory=factory
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(requested[-1], recorded)
        self.assertEqual(json.loads(output)["results"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
