"""Local HTTP integration checks. No real API key or paid requests are used."""

import base64
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import json
import socket
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

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

    def test_agent_stdin_rejects_terminal_without_hanging_or_echoing_key(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdin.readline", side_effect=AssertionError("Must not read terminal")), patch.object(client.getpass, "getpass", side_effect=AssertionError("Must not use console getpass")):
            with self.assertRaises(client.ClientError) as context:
                self.run_client("configure", "--key-stdin")
        self.assertEqual(context.exception.details["error_code"], "stdin_pipe_required")
        self.assertFalse(self.config.exists())

    def test_configure_checks_write_permission_before_reading_key(self):
        with patch.object(client.tempfile, "mkstemp", side_effect=PermissionError(errno.EACCES, "private-path")), patch("sys.stdin.isatty", return_value=False), patch("sys.stdin.readline", side_effect=AssertionError("Must check permissions first")):
            code, value = self.cli("configure", "--key-stdin")
        self.assertEqual(code, 1)
        self.assertEqual(value["error_code"], "configuration_write_denied")
        self.assertEqual(value["config_path"], str(self.config.resolve()))
        self.assertEqual(value["os_errno"], errno.EACCES)
        self.assertNotIn("private-path", json.dumps(value))
        self.assertFalse(self.config.exists())

    def test_check_config_does_not_request_key_or_change_saved_configuration(self):
        code, value = self.cli("check-config")
        self.assertEqual(code, 0)
        self.assertTrue(value["configuration_writable"])
        self.assertFalse(self.config.exists())
        with patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")):
            self.run_client("configure", "--key-stdin")
        previous = self.config.read_bytes()
        with patch("sys.stdin.readline", side_effect=AssertionError("Must not request key")):
            code, value = self.cli("check-config")
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_bytes(), previous)
        self.assertNotIn(FAKE_KEY, json.dumps(value))
        self.assertEqual(self.server.requests, [])

    def test_pipe_input_works_in_real_child_process_without_console_prompt(self):
        environment = dict(client.os.environ, LOCALAPPDATA=str(self.root / "user-data"), XDG_CONFIG_HOME=str(self.root / "user-data"))
        command = [sys.executable, "-B", str(Path(client.__file__).resolve())]
        saved = subprocess.run(command + ["configure", "--key-stdin"], input=FAKE_KEY + "\n", text=True, capture_output=True, timeout=15, env=environment)
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        self.assertEqual(saved.stderr, "")
        self.assertNotIn(FAKE_KEY, saved.stdout)
        value = json.loads(saved.stdout)
        self.assertTrue(value["configured"])
        self.assertEqual(json.loads(Path(value["config_path"]).read_text())["api_key"], FAKE_KEY)
        status = subprocess.run(command + ["status"], text=True, capture_output=True, timeout=15, env=environment)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(json.loads(status.stdout)["api_key_configured"])
        self.assertNotIn(FAKE_KEY, status.stdout + status.stderr)

    def test_failed_replace_keeps_previous_configuration_and_cleans_temporary_key(self):
        with patch("sys.stdin", io.StringIO(FAKE_KEY + "\n")):
            self.run_client("configure", "--key-stdin")
        previous = self.config.read_bytes()
        with patch("sys.stdin", io.StringIO("replacement-test-key\n")), patch.object(client.os, "replace", side_effect=PermissionError(errno.EACCES, "private-write-context")):
            code, value = self.cli("configure", "--key-stdin")
        self.assertEqual(code, 1)
        self.assertEqual(value["error_code"], "configuration_write_denied")
        self.assertEqual(self.config.read_bytes(), previous)
        self.assertEqual(list(self.config.parent.glob(".config-*")), [])
        self.assertNotIn("replacement-test-key", json.dumps(value))
        self.assertNotIn("private-write-context", json.dumps(value))

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

    def test_aspect_ratio_and_resolution_become_valid_size(self):
        self.authenticate()
        for ratio, resolution, expected in (("16:9", None, "2048x1152"), ("9:16", "4k", "2160x3840"), ("4:3", "1k", "1024x768"), ("1:1", "4k", "2880x2880")):
            self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
            args = ["--aspect-ratio", ratio] + (["--resolution", resolution] if resolution else [])
            result = self.run_client(*self.request_args("generate", *args))
            self.assertEqual(json.loads(self.server.requests[-1]["body"])["size"], expected)
            self.assertEqual(result["requested_size"], expected)

    def test_exact_size_is_normalized_and_invalid_dimensions_are_rejected_locally(self):
        self.authenticate()
        image = self.root / "reference.png"
        image.write_bytes(PNG)
        self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        self.run_client(*self.request_args("edit", "--image", str(image), "--size", "1024 * 1536"))
        self.assertIn(b'name="size"\r\n\r\n1024x1536', self.server.requests[0]["body"])
        for extra in (("--size", "wide"), ("--aspect-ratio", "4:1"), ("--size", "1024x1024", "--aspect-ratio", "1:1")):
            with self.assertRaises(SystemExit), patch("sys.stderr"):
                client.parser().parse_args(self.request_args("generate", *extra))
        with self.assertRaises(client.ClientError):
            self.run_client(*self.request_args("generate", "--resolution", "4k"))
        self.assertEqual(len(self.server.requests), 1)

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
        with patch("sys.argv", ["image_client.py", *self.request_args()]), redirect_stdout(output), redirect_stderr(error):
            result = client.main()
        self.assertEqual(result, 1)
        value = json.loads(output.getvalue())
        self.assertEqual(value["status"], "failed")
        self.assertIn("API key required", value["error"])
        self.assertEqual(json.loads(Path(value["result_file"]).read_text()), value)
        self.assertEqual(self.server.requests, [])

    def cli(self, *arguments):
        output, error = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["image_client.py", *arguments]), redirect_stdout(output), redirect_stderr(error):
            code = client.main()
        self.assertNotIn("Traceback", error.getvalue())
        return code, json.loads(output.getvalue())

    def test_cli_http_failure_is_visible_persisted_redacted_and_recoverable_without_key(self):
        self.authenticate()
        self.respond({"error": {"message": "Provider unavailable " + FAKE_KEY}}, status=503)
        code, value = self.cli(*self.request_args())
        self.assertEqual(code, 1)
        self.assertEqual(value["status"], "failed")
        self.assertIn("HTTP 503", value["error"])
        self.assertNotIn(FAKE_KEY, json.dumps(value))
        self.assertFalse(value["automatic_retry"])
        self.assertEqual(json.loads(Path(value["result_file"]).read_text()), value)
        client.os.environ.clear()
        recovered_code, recovered = self.cli("result", value["result_file"])
        self.assertEqual(recovered_code, 1)
        self.assertEqual(recovered, value)
        self.assertEqual(len(self.server.requests), 1)

    def test_cli_success_can_be_recovered_without_generating_again(self):
        self.authenticate()
        self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        code, value = self.cli(*self.request_args())
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "completed")
        client.os.environ.clear()
        self.assertEqual(self.cli("result", value["result_file"]), (0, value))
        self.assertEqual(Path(value["files"][0]).read_bytes(), PNG)
        self.assertEqual(len(self.server.requests), 1)

    def test_receipt_exists_before_network_timeout_and_does_not_retry(self):
        self.authenticate()

        def timeout(*args, **kwargs):
            receipts = list(self.outputs.glob("result-*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(json.loads(receipts[0].read_text())["status"], "running")
            raise socket.timeout()

        with patch.object(client.urllib.request.OpenerDirector, "open", side_effect=timeout) as opened:
            code, value = self.cli(*self.request_args())
        self.assertEqual(code, 1)
        self.assertIn("may still be running", value["error"])
        self.assertEqual(value["error_code"], "network_timeout")
        self.assertEqual(value["submission_status"], "unknown")
        self.assertEqual(value["timeout_seconds"], 600)
        self.assertIn(value["proxy"]["mode"], {"proxy", "direct"})
        self.assertEqual(json.loads(Path(value["result_file"]).read_text()), value)
        opened.assert_called_once()
        self.assertEqual(opened.call_args.kwargs["timeout"], 600)

    def test_generation_edit_and_task_timeouts_are_independent_of_diagnostic_default(self):
        root = client.parser()
        cases = (
            (["generate", "--prompt", "Test"], 600),
            (["diagnose"], 15),
            (["edit", "--image", "reference.png", "--prompt", "Test"], 600),
            (["task", "imgtask_test"], 600),
            (["generate", "--prompt", "Test", "--timeout", "900"], 900),
            (["diagnose", "--timeout", "30"], 30),
            (["generate", "--prompt", "Test"], 600),
        )
        for arguments, timeout in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(root.parse_args(arguments).timeout, timeout)

    def test_explicit_generation_timeout_reaches_network_and_receipt(self):
        self.authenticate()
        with patch.object(client.urllib.request.OpenerDirector, "open", side_effect=socket.timeout()) as opened:
            code, value = self.cli(*self.request_args("generate", "--timeout", "900"))
        self.assertEqual(code, 1)
        self.assertEqual(value["timeout_seconds"], 900)
        self.assertEqual(opened.call_args.kwargs["timeout"], 900)

    def test_environment_proxy_is_used_for_post_and_recorded_without_credentials(self):
        self.authenticate()
        self.respond({"error": {"message": "Test gateway unavailable"}}, status=503)
        with patch.dict(client.os.environ, {"http_proxy": self.url}), patch.object(client.urllib.request, "proxy_bypass", return_value=False):
            code, value = self.cli(*self.request_args("generate", "--base-url", "http://localhost:9"))
        self.assertEqual(code, 1)
        self.assertEqual(value["http_status"], 503)
        self.assertEqual(value["proxy"]["source"], "environment")
        self.assertEqual(value["proxy"]["mode"], "proxy")
        self.assertEqual(value["proxy"]["endpoint"]["host"], "127.0.0.1")
        self.assertEqual(value["proxy"]["endpoint"]["port"], self.server.server_port)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["path"], "http://localhost:9/v1/images/generations")
        self.assertEqual(value["timeout_seconds"], 600)

    def test_proxy_route_omits_password_user_path_and_query_and_respects_bypass(self):
        proxies = {"https": "http://proxy-user:proxy-password@localhost:7890/private-path?secret-token=hidden"}
        with patch.object(client.urllib.request, "proxy_bypass", return_value=False):
            route = client.proxy_route("https://example.com/v1", proxies)
        self.assertEqual(route["mode"], "proxy")
        self.assertEqual(route["endpoint"], {"scheme": "http", "host": "localhost", "port": 7890})
        for secret in ("proxy-user", "proxy-password", "private-path", "secret-token"):
            self.assertNotIn(secret, json.dumps(route))
        with patch.object(client.urllib.request, "proxy_bypass", return_value=True):
            route = client.proxy_route("https://example.com/v1", proxies)
        self.assertEqual(route["mode"], "direct")
        self.assertTrue(route["bypassed"])

    def test_system_proxy_mapping_is_used_when_no_environment_proxy_is_present(self):
        self.authenticate()
        self.respond({"error": {"message": "Test gateway unavailable"}}, status=503)
        with patch.object(client.urllib.request, "getproxies", return_value={"http": self.url}), patch.object(client.urllib.request, "getproxies_environment", return_value={}), patch.object(client.urllib.request, "proxy_bypass", return_value=False):
            code, value = self.cli(*self.request_args("generate", "--base-url", "http://localhost:9"))
        self.assertEqual(code, 1)
        self.assertEqual(value["http_status"], 503)
        self.assertEqual(value["proxy"]["source"], "windows_registry" if client.os.name == "nt" else "system")
        self.assertEqual(value["proxy"]["mode"], "proxy")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["path"], "http://localhost:9/v1/images/generations")

    def test_all_proxy_alone_is_not_misreported_as_an_https_proxy(self):
        route = client.proxy_route("https://example.com/v1", {"all": "socks5://localhost:1080"})
        self.assertEqual(route["mode"], "direct")
        self.assertTrue(route["all_proxy_only"])

    def test_network_failures_report_specific_codes_without_raw_exception_secrets(self):
        self.authenticate()
        cases = (
            (socket.gaierror(-2, "secret-dns-context"), "dns_resolution_failed", "not_sent"),
            (ssl.SSLCertVerificationError(1, "secret-certificate-context"), "tls_certificate_verification_failed", "not_sent"),
            (PermissionError(errno.EACCES, "secret-sandbox-context"), "network_access_denied", "unknown"),
            (ConnectionRefusedError(errno.ECONNREFUSED, "secret-proxy-context"), "connection_refused", "not_sent"),
            (ConnectionResetError(errno.ECONNRESET, "secret-server-context"), "connection_reset", "unknown"),
            (OSError(errno.ENETUNREACH, "secret-routing-context"), "network_unreachable", "unknown"),
            (ssl.SSLError(1, "secret-tls-context"), "tls_error", "unknown"),
            (OSError("secret-other-context"), "network_error", "unknown"),
        )
        for reason, expected, submission in cases:
            with self.subTest(error_code=expected), patch.object(client.urllib.request.OpenerDirector, "open", side_effect=urllib.error.URLError(reason)) as opened:
                code, value = self.cli(*self.request_args())
                self.assertEqual(code, 1)
                self.assertEqual(value["error_code"], expected)
                self.assertEqual(value["submission_status"], submission)
                self.assertEqual(value["exception_type"], type(reason).__name__)
                self.assertNotIn("secret-", json.dumps(value))
                self.assertEqual(json.loads(Path(value["result_file"]).read_text()), value)
                opened.assert_called_once()

    def test_windows_socket_access_denial_is_preserved(self):
        self.authenticate()
        reason = OSError("private-error-context")
        reason.winerror = 10013
        with patch.object(client.urllib.request.OpenerDirector, "open", side_effect=urllib.error.URLError(reason)):
            code, value = self.cli(*self.request_args())
        self.assertEqual(code, 1)
        self.assertEqual(value["error_code"], "network_access_denied")
        self.assertEqual(value["winerror"], 10013)
        self.assertNotIn("private-error-context", json.dumps(value))

    def test_diagnose_gets_models_without_key_or_generation_even_with_saved_credentials(self):
        self.authenticate()
        self.respond({"error": {"message": "Unauthorized"}}, status=401)
        code, value = self.cli("diagnose", "--base-url", self.url)
        self.assertEqual(code, 0)
        self.assertTrue(value["transport_reachable"])
        self.assertEqual(value["http_status"], 401)
        self.assertFalse(value["credentials_sent"])
        self.assertFalse(value["generation_submitted"])
        self.assertEqual(value["probe_method"], "GET")
        self.assertNotIn("result_file", value)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["path"], "/v1/models")
        self.assertEqual(self.server.requests[0]["method"], "GET")
        self.assertEqual(self.server.requests[0]["body"], b"")
        self.assertNotIn("Authorization", self.server.requests[0]["headers"])
        self.assertFalse(self.outputs.exists())

    def test_diagnose_works_without_key_and_reports_proxy_and_certificate_flags_without_secrets(self):
        with patch.dict(client.os.environ, {"HTTPS_PROXY": "http://proxy-user:proxy-secret@proxy.example:8080/private-token", "SSL_CERT_FILE": str(self.root / "missing-ca.pem")}), patch.object(client.urllib.request, "getproxies", return_value={"https": "http://proxy-user:proxy-secret@proxy.example:8080/private-token"}), patch.object(client.urllib.request.OpenerDirector, "open", side_effect=urllib.error.URLError(socket.gaierror(-2, "private-dns-error"))) as opened:
            code, value = self.cli("diagnose", "--base-url", self.url)
        self.assertEqual(code, 1)
        self.assertFalse(value["transport_reachable"])
        self.assertEqual(value["error_code"], "dns_resolution_failed")
        self.assertEqual(value["submission_status"], "not_applicable")
        self.assertTrue(value["network_environment"]["https_proxy_configured"])
        self.assertTrue(value["network_environment"]["ssl_cert_file_configured"])
        self.assertFalse(value["network_environment"]["ssl_cert_file_exists"])
        self.assertNotIn("proxy-secret", json.dumps(value))
        self.assertNotIn("private-token", json.dumps(value))
        self.assertNotIn("private-dns-error", json.dumps(value))
        opened.assert_called_once()
        self.assertEqual(opened.call_args.kwargs["timeout"], 15)
        self.assertEqual(self.server.requests, [])

    def test_failed_download_keeps_network_details_and_completed_files(self):
        self.authenticate()
        self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}, {"url": self.url + "/image.png"}]})
        original = client.request

        def fail_download(url, *args, **kwargs):
            if url.endswith("/image.png"):
                raise client.network_failure(ConnectionResetError(errno.ECONNRESET, "private-download-error"), False)
            return original(url, *args, **kwargs)

        with patch.object(client, "request", side_effect=fail_download):
            code, value = self.cli(*self.request_args())
        self.assertEqual(code, 1)
        self.assertEqual(value["error_code"], "connection_reset")
        self.assertEqual(value["submission_status"], "not_applicable")
        self.assertEqual(len(value["files"]), 1)
        self.assertEqual(Path(value["files"][0]).read_bytes(), PNG)
        self.assertEqual(len(self.server.requests), 1)

    def test_bad_server_json_and_malformed_image_return_visible_failures(self):
        self.authenticate()
        for response in (b"<html>bad gateway</html>", [], {"data": []}, {"data": [{"url": 42}]}):
            with self.subTest(response=response):
                if isinstance(response, list):
                    response = json.dumps(response).encode()
                self.respond(response)
                code, value = self.cli(*self.request_args())
                self.assertEqual(code, 1)
                self.assertEqual(value["status"], "failed")
                self.assertTrue(value["error"])
                self.assertFalse(value["automatic_retry"])
                self.assertEqual(json.loads(Path(value["result_file"]).read_text()), value)
        self.assertEqual(len(self.server.requests), 4)

    def test_partial_success_preserves_saved_files_when_later_image_is_invalid(self):
        self.authenticate()
        self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}, {"b64_json": "invalid"}]})
        code, value = self.cli(*self.request_args())
        self.assertEqual(code, 1)
        self.assertEqual(len(value["files"]), 1)
        self.assertEqual(Path(value["files"][0]).read_bytes(), PNG)
        self.assertEqual(len(self.server.requests), 1)

    def test_failed_async_task_keeps_task_id_and_reports_error(self):
        self.authenticate()
        self.respond({"status": "failed", "error": {"message": "Model unavailable " + FAKE_KEY}})
        code, value = self.cli(*self.request_args("task", "imgtask_test"))
        self.assertEqual(code, 1)
        self.assertEqual(value["task_id"], "imgtask_test")
        self.assertIn("Model unavailable", value["error"])
        self.assertNotIn(FAKE_KEY, json.dumps(value))
        self.assertEqual([request["method"] for request in self.server.requests], ["GET"])

    def test_async_submission_terminal_failure_is_not_reported_as_success(self):
        self.authenticate()
        self.respond({"task_id": "imgtask_test", "status": "failed"}, status=202)
        code, value = self.cli(*self.request_args("generate", "--async"))
        self.assertEqual(code, 1)
        self.assertEqual(value["task_id"], "imgtask_test")
        self.assertTrue(value["error"])
        self.assertEqual(len(self.server.requests), 1)

    def test_failure_to_save_final_receipt_keeps_successful_image_in_stdout(self):
        self.authenticate()
        self.respond({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        original = client.write_receipt
        calls = []

        def save_once(path, value):
            calls.append(path)
            if len(calls) > 1:
                raise PermissionError()
            original(path, value)

        with patch.object(client, "write_receipt", side_effect=save_once):
            code, value = self.cli(*self.request_args())
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "completed")
        self.assertIn("receipt_error", value)
        self.assertEqual(Path(value["files"][0]).read_bytes(), PNG)
        self.assertEqual(len(self.server.requests), 1)

    def test_unknown_async_status_fails_instead_of_polling_forever(self):
        self.authenticate()
        self.respond({"status": "unknown"})
        code, value = self.cli(*self.request_args("task", "imgtask_test"))
        self.assertEqual(code, 1)
        self.assertIn("unknown task status", value["error"])

    def test_interruption_and_unexpected_exception_still_return_an_error(self):
        for exception, expected in ((KeyboardInterrupt(), 130), (TypeError("secret-debug-context"), 1)):
            with self.subTest(exception=type(exception).__name__), patch.object(client, "run", side_effect=exception):
                code, value = self.cli(*self.request_args())
                self.assertEqual(code, expected)
                self.assertTrue(value["error"])
                self.assertNotIn("secret-debug-context", json.dumps(value))
                self.assertEqual(json.loads(Path(value["result_file"]).read_text()), value)

    def test_cannot_create_receipt_stops_before_network_and_still_reports_failure(self):
        self.authenticate()
        with patch.object(client, "write_receipt", side_effect=PermissionError("sensitive path")):
            code, value = self.cli(*self.request_args())
        self.assertEqual(code, 1)
        self.assertEqual(value["status"], "failed")
        self.assertIn("receipt_error", value)
        self.assertNotIn("sensitive path", json.dumps(value))
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
