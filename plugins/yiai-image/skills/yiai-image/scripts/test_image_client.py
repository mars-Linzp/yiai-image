"""Local HTTP integration checks. No real API key or paid requests are used."""

import base64
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import image_client as client


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
FAKE_KEY = "test-only-not-a-real-api-key"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.handle_request()

    def do_GET(self):
        self.handle_request()

    def handle_request(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length)})
        status, headers, body = self.server.responses.popleft()
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class ImageClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:{}".format(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aigcrouter-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "settings" / "config.json"
        self.outputs = self.root / "images"
        self.server.responses = deque()
        self.server.requests = []
        self.config_patch = patch.object(client, "config_path", return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.environment = patch.dict(client.os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_client(self, *arguments):
        return client.run(client.parser().parse_args(arguments))

    def request_args(self, command="generate", *extra):
        args = [command, "--base-url", self.url, "--output-dir", str(self.outputs)]
        if command in {"generate", "edit"}:
            args += ["--prompt", "A red bicycle"]
        return args + list(extra)

    def authenticate(self):
        client.os.environ["AIGCROUTER_API_KEY"] = FAKE_KEY

    def respond(self, value, status=200, headers=None):
        self.server.responses.append((status, headers or {"Content-Type": "application/json"}, value))

    def test_first_use_has_defaults_and_never_sends_a_request_without_key(self):
        status = self.run_client("status")
        self.assertFalse(status["api_key_configured"])
        self.assertEqual(status["base_url"], "https://api.aigcrouter.org/v1")
        self.assertEqual(status["model"], "gpt-image-2.5-sunburst")
        with self.assertRaisesRegex(client.ClientError, "API key required"):
            self.run_client(*self.request_args())
        self.assertEqual(self.server.requests, [])
        self.assertFalse(self.config.exists())

    def test_configure_persists_key_without_printing_it_and_env_can_override(self):
        with patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")):
            result = self.run_client("configure", "--key-stdin")
        self.assertNotIn(FAKE_KEY, json.dumps(result))
        self.assertNotIn(FAKE_KEY, json.dumps(self.run_client("status")))
        self.assertEqual(json.loads(self.config.read_text())["api_key"], FAKE_KEY)
        args = client.parser().parse_args(["status"])
        self.assertEqual(client.settings(args)["api_key"], FAKE_KEY)
        client.os.environ["AIGCROUTER_API_KEY"] = "environment-only-key"
        self.assertEqual(client.settings(args)["api_key"], "environment-only-key")

    def test_base_url_normalizes_version_without_duplication(self):
        self.assertEqual(client.normalize_base_url("https://api.aigcrouter.org/v1/"), "https://api.aigcrouter.org/v1")
        for value in ["http://example.com", "https://user:secret@example.com", "https://example.com?key=secret"]:
            with self.assertRaises(client.ClientError):
                client.normalize_base_url(value)

    def test_keep_key_updates_preferences_without_prompting_and_generation_uses_them(self):
        with patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")):
            self.run_client("configure", "--key-stdin")
        with patch("sys.stdin.readline", side_effect=AssertionError("Must not read stdin")), patch.object(client.getpass, "getpass", side_effect=AssertionError("Must not prompt")):
            result = self.run_client("configure", "--keep-key", "--base-url", self.url, "--model", "chosen-model")
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved, {"api_key": FAKE_KEY, "base_url": self.url, "model": "chosen-model"})
        self.assertNotIn(FAKE_KEY, json.dumps(result))
        self.assertEqual(self.server.requests, [])
        self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        self.run_client("generate", "--prompt", "Test", "--output-dir", str(self.outputs))
        self.assertEqual(self.server.requests[0]["headers"]["Authorization"], "Bearer " + FAKE_KEY)
        self.assertEqual(json.loads(self.server.requests[0]["body"])["model"], "chosen-model")

    def test_keep_key_requires_a_saved_key_even_if_environment_is_configured(self):
        self.authenticate()
        with self.assertRaisesRegex(client.ClientError, "No saved API key"):
            self.run_client("configure", "--keep-key", "--model", "chosen-model")
        self.assertFalse(self.config.exists())
        status = self.run_client("status")
        self.assertTrue(status["api_key_configured"])
        self.assertFalse(status["saved_api_key_configured"])
        self.assertEqual(status["configuration_sources"]["api_key"], "environment")

    def test_replacing_key_preserves_preferences_and_keep_key_preserves_unspecified_fields(self):
        with patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")):
            self.run_client("configure", "--key-stdin", "--base-url", self.url, "--model", "first-model")
        self.run_client("configure", "--keep-key", "--model", "second-model")
        self.assertEqual(json.loads(self.config.read_text())["base_url"], self.url)
        with patch("sys.stdin", io.StringIO("replacement-test-key\n")):
            self.run_client("configure", "--key-stdin")
        self.assertEqual(json.loads(self.config.read_text()), {"api_key": "replacement-test-key", "base_url": self.url, "model": "second-model"})

    def test_invalid_preference_update_preserves_existing_configuration(self):
        with patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")):
            self.run_client("configure", "--key-stdin")
        previous = self.config.read_bytes()
        for option, value in (("--base-url", "http://example.com"), ("--base-url", ""), ("--model", "   ")):
            with self.subTest(option=option, value=value), self.assertRaises(client.ClientError):
                self.run_client("configure", "--keep-key", option, value)
            self.assertEqual(self.config.read_bytes(), previous)
        self.assertEqual(self.server.requests, [])

    def test_status_reports_effective_sources_without_exposing_keys(self):
        with patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")):
            self.run_client("configure", "--key-stdin")
        status = self.run_client("status")
        self.assertTrue(status["saved_api_key_configured"])
        self.assertEqual(status["configuration_sources"], {"api_key": "saved", "base_url": "saved", "model": "saved"})
        client.os.environ.update({"AIGCROUTER_API_KEY": "environment-test-key", "AIGCROUTER_BASE_URL": self.url, "AIGCROUTER_IMAGE_MODEL": "environment-model"})
        status = self.run_client("status")
        self.assertEqual(set(status["configuration_sources"].values()), {"environment"})
        self.assertEqual(status["base_url"], self.url + "/v1")
        self.assertEqual(status["model"], "environment-model")
        self.assertNotIn(FAKE_KEY, json.dumps(status))
        self.assertNotIn("environment-test-key", json.dumps(status))
        explicit = self.run_client("status", "--model", "explicit-model")
        self.assertEqual(explicit["model"], "explicit-model")
        self.assertEqual(explicit["configuration_sources"]["model"], "command_line")

    def test_agent_stdin_in_terminal_uses_hidden_input(self):
        output = io.StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch.object(client.getpass, "getpass", return_value=FAKE_KEY) as hidden_input, redirect_stdout(output):
            result = self.run_client("configure", "--key-stdin")
        hidden_input.assert_called_once()
        self.assertEqual(json.loads(self.config.read_text())["api_key"], FAKE_KEY)
        self.assertNotIn(FAKE_KEY, json.dumps(result) + output.getvalue())
        self.assertTrue(self.run_client("status")["api_key_configured"])

    def test_generate_saves_base64_and_preserves_default_model_and_auth(self):
        self.authenticate()
        self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        result = self.run_client(*self.request_args())
        self.assertEqual(Path(result["files"][0]).read_bytes(), PNG)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/images/generations")
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + FAKE_KEY)
        self.assertEqual(json.loads(request["body"]), {"model": "gpt-image-2.5-sunburst", "prompt": "A red bicycle", "n": 1})
        self.assertNotIn("b64_json", json.dumps(result))
        self.assertTrue(Path(result["files"][0]).is_absolute())

    def test_download_does_not_forward_bearer_key(self):
        self.authenticate()
        self.respond({"data": [{"url": self.url + "/image.png"}]})
        self.respond(PNG, headers={"Content-Type": "image/png"})
        result = self.run_client(*self.request_args())
        self.assertEqual(Path(result["files"][0]).read_bytes(), PNG)
        self.assertNotIn("Authorization", self.server.requests[1]["headers"])

    def test_data_url_and_explicit_model_override(self):
        self.authenticate()
        self.respond({"data": [{"url": "data:image/png;base64," + base64.b64encode(PNG).decode()}]})
        self.run_client(*self.request_args("generate", "--model", "user-requested-model", "--size", "1536x1024"))
        payload = json.loads(self.server.requests[0]["body"])
        self.assertEqual(payload["model"], "user-requested-model")
        self.assertEqual(payload["size"], "1536x1024")

    def test_async_submission_and_polling_never_repeats_post(self):
        self.authenticate()
        self.respond({"task_id": "imgtask_test", "status": "processing"}, status=202)
        submitted = self.run_client(*self.request_args("generate", "--async"))
        self.assertEqual(submitted["task_id"], "imgtask_test")
        self.respond({"task_id": "imgtask_test", "status": "processing"})
        running = self.run_client(*self.request_args("task", "imgtask_test"))
        self.assertEqual(running["status"], "processing")
        self.assertFalse(self.outputs.exists())
        self.respond({"status": "completed", "result": {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}})
        completed = self.run_client(*self.request_args("task", "imgtask_test"))
        self.assertEqual(Path(completed["files"][0]).read_bytes(), PNG)
        self.assertEqual([request["method"] for request in self.server.requests], ["POST", "GET", "GET"])
        self.assertEqual(self.server.requests[0]["path"], "/v1/images/generations/async")

    def test_edit_uploads_local_file_and_mask_as_multipart(self):
        self.authenticate()
        image = self.root / "reference.png"
        image.write_bytes(PNG)
        self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        self.run_client(*self.request_args("edit", "--image", str(image), "--mask", str(image)))
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/images/edits")
        self.assertIn("multipart/form-data", request["headers"]["Content-Type"])
        self.assertIn(b'name="image[]"', request["body"])
        self.assertIn(b'name="mask"', request["body"])
        self.assertEqual(request["body"].count(PNG), 2)

    def test_failed_post_is_redacted_and_not_retried(self):
        self.authenticate()
        self.respond({"error": {"message": "Invalid key " + FAKE_KEY}}, status=401)
        with self.assertRaises(client.ClientError) as context:
            self.run_client(*self.request_args())
        self.assertIn("HTTP 401", str(context.exception))
        self.assertNotIn(FAKE_KEY, str(context.exception))
        self.assertEqual(len(self.server.requests), 1)

    def test_authenticated_redirect_is_not_followed(self):
        self.authenticate()
        self.respond(b"", status=307, headers={"Location": self.url + "/other-host"})
        with self.assertRaisesRegex(client.ClientError, "HTTP 307"):
            self.run_client(*self.request_args())
        self.assertEqual(len(self.server.requests), 1)

    def test_invalid_download_is_not_saved_as_an_image(self):
        self.authenticate()
        self.respond({"data": [{"url": self.url + "/bad.png"}]})
        self.respond(b"<html>error</html>", headers={"Content-Type": "text/html"})
        with self.assertRaisesRegex(client.ClientError, "not a supported image"):
            self.run_client(*self.request_args())
        self.assertEqual(list(self.outputs.iterdir()), [])
        self.assertEqual(len(self.server.requests), 2)

    def test_cli_missing_key_returns_compact_json_error(self):
        output, error = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["image_client.py", "generate", "--prompt", "Test"]), redirect_stdout(output), redirect_stderr(error):
            result = client.main()
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("API key required", json.loads(error.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
