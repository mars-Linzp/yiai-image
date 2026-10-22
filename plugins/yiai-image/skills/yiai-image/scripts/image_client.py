#!/usr/bin/env python3
"""YiAI-image: a dependency-free client for the AigcRouter Images API."""

import argparse
import base64
import binascii
from datetime import datetime, timezone
import errno
import getpass
import http.client
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


DEFAULT_BASE_URL = "https://api.aigcrouter.org"
DEFAULT_MODEL = "gpt-image-2.5-sunburst"
MAX_RESPONSE = 128 * 1024 * 1024
MAX_IMAGE = 32 * 1024 * 1024
RESOLUTION_EDGES = {"1k": 1024, "2k": 2048, "4k": 3840}
MAX_OUTPUT_PIXELS = 3840 * 2160
MAX_ASPECT_RATIO = 3


class ClientError(Exception):
    def __init__(self, message, files=None, details=None):
        super().__init__(message)
        self.files = files or []
        self.details = details or {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def config_path():
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    # Preserve configured credentials across skill renames.
    return root / "aigcrouter-image" / "config.json"


def read_config():
    path = config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ClientError("Cannot read local configuration; run configure again.") from None
    if not isinstance(value, dict):
        raise ClientError("Invalid local configuration; run configure again.")
    return value


def normalize_base_url(value):
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or not parsed.netloc:
        raise ClientError("Base URL must use HTTPS (HTTP is allowed for localhost testing).")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClientError("Base URL must not contain credentials, a query, or a fragment.")
    return value if value.endswith("/v1") else value + "/v1"


def settings(args):
    saved = read_config()
    key = os.environ.get("AIGCROUTER_API_KEY") or saved.get("api_key", "")
    base = args.base_url or os.environ.get("AIGCROUTER_BASE_URL") or saved.get("base_url") or DEFAULT_BASE_URL
    model = args.model or os.environ.get("AIGCROUTER_IMAGE_MODEL") or saved.get("model") or DEFAULT_MODEL
    return {"api_key": str(key).strip(), "base_url": normalize_base_url(base), "model": model}


def configure(args):
    saved = read_config()
    if args.keep_key:
        key = str(saved.get("api_key", "")).strip()
        if not key:
            raise ClientError("No saved API key. Ask the user for their key in the conversation and save it with --key-stdin.")
    else:
        if not args.key_stdin and not sys.stdin.isatty():
            raise ClientError("Run configure in an interactive terminal for hidden key input, or use --key-stdin.")
        if args.key_stdin and not sys.stdin.isatty():
            key = sys.stdin.readline().strip()
        else:
            key = getpass.getpass("YiAI-image user API key: ").strip()
    if not key or any(char.isspace() for char in key):
        raise ClientError("A non-empty API key without whitespace is required.")
    base = args.base_url if args.base_url is not None else saved.get("base_url") or DEFAULT_BASE_URL
    normalize_base_url(base)
    model = (args.model if args.model is not None else saved.get("model") or DEFAULT_MODEL).strip()
    if not model:
        raise ClientError("The image model must not be empty.")
    value = {"api_key": key, "base_url": base, "model": model}
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"configured": True, "config_path": str(path.resolve()), "base_url": base, "model": value["model"]}


def redact(message, key):
    message = str(message)
    if key:
        message = message.replace(key, "[REDACTED]")
    return message[:1000]


def network_failure(exc, submitted):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    number = getattr(reason, "errno", None)
    windows = getattr(reason, "winerror", None)
    before_send = False
    if isinstance(reason, socket.gaierror):
        code, message = "dns_resolution_failed", "Cannot resolve the API or proxy hostname. Check DNS, Base URL and proxy settings."
        before_send = True
    elif isinstance(reason, ssl.SSLCertVerificationError):
        code, message = "tls_certificate_verification_failed", "TLS certificate verification failed. Check the system clock, trusted certificates and HTTPS proxy certificates; keep TLS verification enabled."
        before_send = True
    elif isinstance(reason, ssl.SSLError):
        code, message = "tls_error", "The TLS connection failed. Check HTTPS proxy and TLS configuration."
    elif isinstance(reason, PermissionError) or number in {errno.EACCES, errno.EPERM} or windows in {5, 10013}:
        code, message = "network_access_denied", "Network access was denied by the OS, firewall or execution sandbox. Check the host's network permissions."
    elif isinstance(reason, ConnectionRefusedError) or number == errno.ECONNREFUSED or windows == 10061:
        code, message = "connection_refused", "The API or proxy refused the connection. Check whether the configured proxy is running and the endpoint is correct."
        before_send = True
    elif isinstance(reason, (TimeoutError, socket.timeout)) or number == errno.ETIMEDOUT or windows == 10060:
        code, message = "network_timeout", "Network request timed out."
    elif isinstance(reason, ConnectionResetError) or number == errno.ECONNRESET or windows == 10054:
        code, message = "connection_reset", "The API, proxy or firewall reset the connection."
    elif number in {errno.ENETUNREACH, errno.EHOSTUNREACH} or windows in {10051, 10065}:
        code, message = "network_unreachable", "The API or proxy network is unreachable. Check routing, VPN and proxy settings."
    else:
        code, message = "network_error", "The network connection failed; inspect the error type, OS error code and network diagnostic."
    details = {"error_code": code, "exception_type": type(reason).__name__, "submission_status": "not_sent" if submitted and before_send else "unknown" if submitted else "not_applicable"}
    if isinstance(number, int):
        details["os_errno"] = number
    if isinstance(windows, int):
        details["winerror"] = windows
    if submitted:
        message += " The image request was not sent." if before_send else " A submitted generation may still be running; do not automatically resubmit."
    return ClientError(message, details=details)


def request(url, key="", body=None, content_type="application/json", timeout=600, limit=MAX_RESPONSE):
    headers = {"Accept": "application/json" if key else "image/*", "User-Agent": "aigcrouter-image-skill/1"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            content = response.read(limit + 1)
            if len(content) > limit:
                raise ClientError("Response exceeds the download limit; do not resubmit generation.")
            return content, response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        # Do not emit raw HTTP bodies: providers can include request headers in them.
        detail = ""
        try:
            error = json.loads(exc.read(16384)).get("error", {})
            detail = error.get("message", "") if isinstance(error, dict) else str(error)
        except (ValueError, AttributeError):
            pass
        raise ClientError(redact("HTTP {}: {}".format(exc.code, detail or exc.reason), key), details={"error_code": "http_error", "http_status": exc.code}) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
        raise network_failure(exc, body is not None) from None


def diagnose(args, config):
    # No key, prompt or POST: this probe cannot submit an image job.
    parsed = urllib.parse.urlsplit(config["base_url"])
    proxies = urllib.request.getproxies()
    environment = {"https_proxy_configured": bool(proxies.get("https")), "http_proxy_configured": bool(proxies.get("http")), "all_proxy_configured": bool(proxies.get("all")), "proxy_bypassed": urllib.request.proxy_bypass(parsed.hostname), "ssl_cert_file_configured": bool(os.environ.get("SSL_CERT_FILE")), "ssl_cert_dir_configured": bool(os.environ.get("SSL_CERT_DIR"))}
    if os.environ.get("SSL_CERT_FILE"):
        try:
            environment["ssl_cert_file_exists"] = Path(os.environ["SSL_CERT_FILE"]).is_file()
        except OSError:
            environment["ssl_cert_file_exists"] = False
    result = {"status": "completed", "base_url": config["base_url"], "probe_method": "GET", "probe_path": "/models", "credentials_sent": False, "generation_submitted": False, "network_environment": environment}
    start = time.monotonic()
    try:
        request(config["base_url"] + "/models", timeout=args.timeout, limit=1024 * 1024)
        result["transport_reachable"] = True
    except ClientError as exc:
        status = exc.details.get("http_status")
        if status is not None and status != 407:
            result.update(transport_reachable=True, http_status=status)
        else:
            result.update(status="failed", transport_reachable=False, error=str(exc), **exc.details)
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    result["note"] = "This unauthenticated probe checks network connectivity only; it does not verify the API key, image model or generation quota."
    return result


def json_request(url, key, payload=None, timeout=600, content_type="application/json", allow_task_error=False):
    body = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
    raw, _ = request(url, key, body, content_type, timeout)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ClientError("Server returned invalid JSON; do not resubmit generation.") from None
    if not isinstance(value, dict):
        raise ClientError("Server returned an unexpected response; do not resubmit generation.")
    if value.get("error") and not allow_task_error:
        error = value["error"]
        detail = error.get("message", "Image request failed") if isinstance(error, dict) else str(error)
        raise ClientError(redact(detail, key))
    return value


def image_suffix(content, content_type=""):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return ".gif"
    raise ClientError("The returned content is not a supported image; do not resubmit generation.")


def image_bytes(item, timeout):
    try:
        if item.get("b64_json"):
            content = base64.b64decode(item["b64_json"], validate=True)
        else:
            url = item.get("url", "")
            if not isinstance(url, str):
                raise ClientError("The API returned an invalid image URL; do not resubmit generation.")
            if url.startswith("data:image/") and ";base64," in url:
                content = base64.b64decode(url.split(",", 1)[1], validate=True)
            else:
                parsed = urllib.parse.urlsplit(url)
                local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
                    raise ClientError("The API did not return a valid HTTPS image URL.")
                if parsed.username or parsed.password:
                    raise ClientError("Image URLs must not contain credentials.")
                # Image hosts never receive the API credential, even on the same host.
                content, _ = request(url, timeout=timeout, limit=MAX_IMAGE)
    except (ValueError, binascii.Error, TypeError):
        raise ClientError("Invalid image data; do not resubmit generation.") from None
    if not content or len(content) > MAX_IMAGE:
        raise ClientError("Image is empty or exceeds 32 MiB; do not resubmit generation.")
    return content


def save_images(value, output_dir, timeout):
    if not isinstance(value, dict):
        raise ClientError("Unexpected image result; do not resubmit generation automatically.")
    data = value.get("data")
    if not isinstance(data, list) or not data:
        raise ClientError("No images in the response; do not resubmit generation automatically.")
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for item in data:
        try:
            if not isinstance(item, dict):
                raise ClientError("Invalid image entry in the response.")
            content = image_bytes(item, timeout)
            path = destination / ("image-" + uuid.uuid4().hex + image_suffix(content))
            with path.open("xb") as stream:
                stream.write(content)
        except ClientError as exc:
            raise ClientError(str(exc) + " Do not regenerate.", files=paths, details=exc.details) from None
        except OSError:
            raise ClientError("Cannot save the generated image. Check output paths and permissions; do not regenerate.", files=paths) from None
        paths.append(str(path))
    return paths


def multipart(payload, images, mask):
    boundary = "aigcrouter-" + uuid.uuid4().hex
    parts = []
    for name, value in payload.items():
        parts.append(("--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(boundary, name, value)).encode("utf-8"))
    files = [("image[]", path) for path in images]
    if mask:
        files.append(("mask", mask))
    for index, (field, filename) in enumerate(files):
        path = Path(filename)
        if path.stat().st_size > MAX_IMAGE:
            raise ClientError("Input images must not exceed 32 MiB each.")
        content = path.read_bytes()
        suffix = image_suffix(content)
        mime = mimetypes.guess_type("image" + suffix)[0] or "application/octet-stream"
        header = "--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"input-{}{}\"\r\nContent-Type: {}\r\n\r\n".format(boundary, field, index, suffix, mime)
        parts.extend([header.encode("ascii"), content, b"\r\n"])
    parts.append(("--" + boundary + "--\r\n").encode("ascii"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def image_size(value):
    text = value.strip().lower()
    if text == "auto":
        return text
    match = re.fullmatch(r"([1-9][0-9]{0,4})\s*[x*]\s*([1-9][0-9]{0,4})", text)
    if not match:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT, for example 1536x1024, or auto")
    return "{}x{}".format(int(match.group(1)), int(match.group(2)))


def aspect_ratio(value):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*[:/]\s*([0-9]+(?:\.[0-9]+)?)\s*", value)
    if not match or float(match.group(1)) <= 0 or float(match.group(2)) <= 0:
        raise argparse.ArgumentTypeError("use WIDTH:HEIGHT, for example 16:9")
    width, height = float(match.group(1)), float(match.group(2))
    if max(width, height) / min(width, height) > MAX_ASPECT_RATIO:
        raise argparse.ArgumentTypeError("aspect ratio must not exceed 3:1")
    return width, height


def size_from_aspect_ratio(ratio, resolution):
    width, height = ratio
    scale = RESOLUTION_EDGES[resolution] / max(width, height)
    pixels = width * scale * height * scale
    if pixels > MAX_OUTPUT_PIXELS:
        scale *= (MAX_OUTPUT_PIXELS / pixels) ** 0.5
    output_width = max(16, int(width * scale / 16) * 16)
    output_height = max(16, int(height * scale / 16) * 16)
    return "{}x{}".format(output_width, output_height)


def parser():
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--base-url")
    shared.add_argument("--model")
    shared.add_argument("--timeout", type=positive_int, default=600)
    shared.add_argument("--output-dir", default="output/aigcrouter-images")
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status", parents=[shared])
    commands.add_parser("diagnose", parents=[shared], help="Check network connectivity without a key or generating an image").set_defaults(timeout=15)
    result = commands.add_parser("result", help="Read a saved execution receipt without sending an API request")
    result.add_argument("path")
    setup = commands.add_parser("configure", parents=[shared])
    key_mode = setup.add_mutually_exclusive_group()
    key_mode.add_argument("--key-stdin", action="store_true", help="Read the API key from stdin instead of a hidden prompt")
    key_mode.add_argument("--keep-key", action="store_true", help="Keep the saved API key and update preferences without reading stdin")
    for name in ("generate", "edit"):
        command = commands.add_parser(name, parents=[shared])
        prompt = command.add_mutually_exclusive_group(required=True)
        prompt.add_argument("--prompt")
        prompt.add_argument("--prompt-file")
        dimensions = command.add_mutually_exclusive_group()
        dimensions.add_argument("--size", type=image_size, help="Exact output size such as 1536x1024, or auto")
        dimensions.add_argument("--aspect-ratio", type=aspect_ratio, help="Output aspect ratio such as 16:9, 9:16, or 4:3")
        command.add_argument("--resolution", type=str.lower, choices=sorted(RESOLUTION_EDGES), help="Size tier used with --aspect-ratio; defaults to 2k")
        command.add_argument("--quality")
        command.add_argument("--n", type=positive_int, default=1)
        command.add_argument("--async", action="store_true", dest="asynchronous")
        if name == "edit":
            command.add_argument("--image", action="append", required=True)
            command.add_argument("--mask")
    task = commands.add_parser("task", parents=[shared])
    task.add_argument("task_id")
    return root


def run(args):
    if args.command == "result":
        value = json.loads(Path(args.path).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("command") not in {"generate", "edit", "task"} or not value.get("result_file"):
            raise ClientError("Not a YiAI-image execution receipt.")
        return value
    if args.command == "configure":
        return configure(args)
    config = settings(args)
    key, base, model = config["api_key"], config["base_url"], config["model"]
    if args.command == "diagnose":
        return diagnose(args, config)
    if args.command == "status":
        saved = read_config()
        sources = {}
        for name, option, environment in (
            ("api_key", None, "AIGCROUTER_API_KEY"),
            ("base_url", args.base_url, "AIGCROUTER_BASE_URL"),
            ("model", args.model, "AIGCROUTER_IMAGE_MODEL"),
        ):
            sources[name] = "command_line" if option else "environment" if os.environ.get(environment) else "saved" if saved.get(name) else "default"
        if not key:
            sources["api_key"] = "missing"
        return {"api_key_configured": bool(key), "saved_api_key_configured": bool(str(saved.get("api_key", "")).strip()), "base_url": base, "model": model, "configuration_sources": sources, "config_path": str(config_path().resolve())}
    if not key:
        raise ClientError("API key required. Ask the user for their AigcRouter user API key in the conversation, save it internally, and resume the pending image request.")
    if args.command == "task":
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.task_id):
            raise ClientError("Invalid task ID.")
        task = json_request(base + "/images/tasks/" + args.task_id, key, timeout=args.timeout, allow_task_error=True)
        result = {"task_id": args.task_id, "status": task.get("status")}
        if task.get("error") or task.get("status") in {"failed", "cancelled", "canceled", "expired"}:
            raise ClientError(redact("Image task failed: " + str(task.get("error", "unknown error")), key))
        if task.get("status") not in {"processing", "pending", "queued", "running", "completed"}:
            raise ClientError("Server returned an unknown task status; query the same task instead of generating again.")
        if task.get("status") == "completed":
            payload = task.get("result") or {"data": [{"url": task.get("image_url", "")}]}
            result["files"] = save_images(payload, args.output_dir, args.timeout)
        return result
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    if not prompt.strip():
        raise ClientError("The image prompt must not be empty.")
    if args.resolution and not args.aspect_ratio:
        raise ClientError("--resolution requires --aspect-ratio; use --size for an exact size.")
    size = args.size or (size_from_aspect_ratio(args.aspect_ratio, args.resolution or "2k") if args.aspect_ratio else None)
    payload = {"model": model, "prompt": prompt, "n": args.n}
    if size:
        payload["size"] = size
    if args.quality:
        payload["quality"] = args.quality
    content_type = "application/json"
    endpoint = "/images/generations" if args.command == "generate" else "/images/edits"
    if args.command == "edit":
        payload, content_type = multipart(payload, args.image, args.mask)
    if args.asynchronous:
        endpoint += "/async"
    value = json_request(base + endpoint, key, payload, args.timeout, content_type)
    if args.asynchronous:
        task_id = value.get("task_id") or value.get("id")
        if value.get("status") in {"failed", "cancelled", "canceled", "expired"}:
            return {"task_id": task_id, "status": "failed", "error": "Image task submission returned status: " + value["status"], "automatic_retry": False}
        if not task_id:
            raise ClientError("No task ID returned; do not resubmit generation automatically.")
        result = {"task_id": task_id, "status": value.get("status", "processing"), "model": model}
    else:
        result = {"status": "completed", "model": model, "files": save_images(value, args.output_dir, args.timeout)}
    if size:
        result["requested_size"] = size
    return result


def write_receipt(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".result-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    args = parser().parse_args()
    receipt = None
    metadata = {"command": args.command}
    exit_code = 0
    try:
        if args.command in {"generate", "edit", "task"}:
            receipt = Path(args.output_dir).resolve() / ("result-" + uuid.uuid4().hex + ".json")
            metadata.update(result_file=str(receipt), started_at=datetime.now(timezone.utc).isoformat())
            if args.command == "task":
                metadata["task_id"] = args.task_id
            # Persist before the POST so a disconnected conversation can find this run.
            write_receipt(receipt, {**metadata, "status": "running", "automatic_retry": False})
            print(json.dumps({**metadata, "status": "running"}), file=sys.stderr, flush=True)
        value = {**metadata, **run(args)}
        if value.get("status") in {"failed", "cancelled"}:
            exit_code = 1
    except (KeyboardInterrupt, EOFError):
        value = {**metadata, "status": "cancelled", "error": "Cancelled. A submitted generation may still be running; do not automatically resubmit.", "automatic_retry": False}
        exit_code = 130
    except Exception as exc:
        # Providers, filesystem exceptions and tracebacks may contain credentials.
        if isinstance(exc, ClientError):
            message = str(exc)
        elif isinstance(exc, (OSError, ValueError)):
            message = "Local configuration or file operation failed. Check paths and permissions."
        else:
            message = "Unexpected client error ({}). Do not automatically resubmit generation.".format(type(exc).__name__)
        value = {**metadata, "status": "failed", "error": message, "files": getattr(exc, "files", []), "automatic_retry": False}
        if isinstance(exc, ClientError):
            value.update(exc.details)
        exit_code = 1
    if receipt is not None:
        value["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            write_receipt(receipt, value)
        except OSError:
            value["receipt_error"] = "Could not save the final execution receipt; use this command output. Do not regenerate."
    # Failures are results too: keep one terminal JSON record on stdout on all paths.
    print(json.dumps(value, ensure_ascii=True), flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
