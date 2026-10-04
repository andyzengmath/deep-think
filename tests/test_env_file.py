import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_deep_think import FakeClient, FakeResponse, load_module

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deep_think.py"


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, text, name=".env"):
        path = self.directory / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_reads_settings_without_overriding_the_environment(self):
        path = self.write(
            "\ufeff# Local deep-think settings\n"
            "\n"
            "export AZURE_OPENAI_GPT6_ENDPOINT=https://east.example/openai/v1/  # main\n"
            "AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT='gpt-6-astra-backup'\n"
            'DEEP_THINK_AUTH="entra, openai-key"\n'
            "OPENAI_BASE_URL=\n"
            "AZURE_OPENAI_ENDPOINT=https://file.example/openai/v1/\n"
        )
        environ = {"AZURE_OPENAI_ENDPOINT": "https://shell.example/openai/v1/"}
        self.assertEqual(self.runner.load_env_file(path, environ=environ), path)
        self.assertEqual(
            environ,
            {
                "AZURE_OPENAI_ENDPOINT": "https://shell.example/openai/v1/",
                "AZURE_OPENAI_GPT6_ENDPOINT": "https://east.example/openai/v1/",
                "AZURE_OPENAI_GPT6_BACKUP_DEPLOYMENT": "gpt-6-astra-backup",
                "DEEP_THINK_AUTH": "entra, openai-key",
            },
        )

    def test_rejects_unrelated_or_malformed_lines_without_echoing_values(self):
        for text, expected in (
            ("PATH=C:\\evil-secret-value\n", "PATH"),
            ("OPENAI_API_KEY sk-secret-value\n", ":1"),
            ('OPENAI_API_KEY="sk-secret-value\n', "OPENAI_API_KEY"),
            ("DEEP_THINK_AUTH='entra' trailing-secret-value\n", "DEEP_THINK_AUTH"),
        ):
            with self.subTest(text=text):
                environ = {}
                with self.assertRaisesRegex(
                    self.runner.DeepThinkError, expected
                ) as raised:
                    self.runner.load_env_file(self.write(text), environ=environ)
                self.assertNotIn("secret-value", str(raised.exception))
                self.assertEqual(environ, {})

    def test_explicit_file_must_exist_and_the_default_file_is_optional(self):
        missing = self.directory / "missing.env"
        with self.assertRaisesRegex(self.runner.DeepThinkError, "DEEP_THINK_ENV_FILE"):
            self.runner.load_env_file(environ={"DEEP_THINK_ENV_FILE": str(missing)})
        config = {"XDG_CONFIG_HOME": str(self.directory)}
        self.assertIsNone(self.runner.load_env_file(environ=dict(config)))

        default = self.directory / "deep-think" / ".env"
        default.parent.mkdir()
        default.write_text("DEEP_THINK_AUTH=openai-key\n", encoding="utf-8")
        environ = dict(config)
        self.assertEqual(self.runner.load_env_file(environ=environ), default)
        self.assertEqual(environ["DEEP_THINK_AUTH"], "openai-key")

        disabled = {**config, "DEEP_THINK_ENV_FILE": os.devnull}
        self.runner.load_env_file(environ=disabled)
        self.assertNotIn("DEEP_THINK_AUTH", disabled)

    def test_cli_loads_the_env_file_before_parsing_arguments(self):
        root = self.directory / "transcripts"
        self.runner.run_turn(
            FakeClient([FakeResponse("resp-1", "Answer.")]),
            root=root,
            project="demo",
            prompt="Question.",
            deployment="gpt-6-astra",
            sleep=lambda _delay: None,
        )
        env_file = self.write(f"DEEP_THINK_TRANSCRIPTS_ROOT={root}\n")
        stdout = io.StringIO()
        with (
            mock.patch.dict(
                "os.environ", {"DEEP_THINK_ENV_FILE": str(env_file)}, clear=True
            ),
            mock.patch("sys.stdout", stdout),
        ):
            code = self.runner.cli(["status", "--project", "demo"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["project_dir"], str(root / "demo")
        )

    def test_script_reports_env_file_errors(self):
        env_file = self.write("NOT_A_SETTING=1\n")
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "status", "--project", "demo"],
            capture_output=True,
            check=False,
            env={**os.environ, "DEEP_THINK_ENV_FILE": str(env_file)},
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("NOT_A_SETTING", completed.stderr)


if __name__ == "__main__":
    unittest.main()
