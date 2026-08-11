#!/usr/bin/env python3
"""OpenCode-compatible HTTP bridge backed by `codex app-server --stdio`."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    TelegramBridgeError,
    ask_questions,
    bridge_url,
    load_telegram_config,
    redact_sensitive_text,
    register_session,
    request_permission,
    send_codex_notification,
)


LOG = logging.getLogger("codex-telegram-bridge")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


class RpcError(RuntimeError):
    pass


def find_codex_binary() -> str:
    configured = os.environ.get("CODEX_BIN")
    if configured and Path(configured).is_file():
        return configured
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    candidates = sorted(
        Path.home().glob(".nvm/versions/node/*/bin/codex"),
        key=lambda path: path.parent.parent.name,
        reverse=True,
    )
    if candidates:
        return str(candidates[0])
    raise RpcError("codex executable not found")


class AppServerClient:
    def __init__(
        self,
        server_request_handler: Callable[[str, dict[str, Any]], dict[str, Any]],
        notification_handler: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.server_request_handler = server_request_handler
        self.notification_handler = notification_handler
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process and self.process.poll() is None else None

    def start(self) -> None:
        with self._start_lock:
            if self._closed:
                raise RpcError("app-server client is closed")
            if self.process and self.process.poll() is None:
                return
            env = os.environ.copy()
            env["CODEX_TELEGRAM_BRIDGE_CHILD"] = "1"
            self.process = subprocess.Popen(
                [find_codex_binary(), "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
            self._reader = threading.Thread(
                target=self._read_loop,
                name="codex-app-server-reader",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._stderr_loop,
                name="codex-app-server-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-telegram-plugin",
                        "title": "Codex Telegram Bridge",
                        "version": "0.1.0",
                    },
                    "capabilities": {},
                },
                timeout=20,
                ensure_started=False,
            )
            self.notify("initialized", {}, ensure_started=False)

    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if not process or process.poll() is not None or process.stdin is None:
            raise RpcError("codex app-server is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            process.stdin.write(encoded + "\n")
            process.stdin.flush()

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60,
        ensure_started: bool = True,
    ) -> Any:
        if ensure_started:
            self.start()
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = responses
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                response = responses.get(timeout=timeout)
            except queue.Empty as exc:
                raise RpcError(f"app-server timeout for {method}") from exc
            if "error" in response:
                safe_error = redact_sensitive_text(response["error"])
                raise RpcError(f"{method} failed: {safe_error}")
            return response.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(
        self,
        method: str,
        params: dict[str, Any],
        *,
        ensure_started: bool = True,
    ) -> None:
        if ensure_started:
            self.start()
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, request_id: Any, result: Any = None, error: Any = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is None:
            payload["result"] = result
        else:
            payload["error"] = error
        self._send(payload)

    def _read_loop(self) -> None:
        process = self.process
        if not process or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("non-JSON app-server stdout ignored")
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    threading.Thread(
                        target=self._handle_server_request,
                        args=(message,),
                        daemon=True,
                    ).start()
                    continue
                if "method" in message:
                    try:
                        params = message.get("params")
                        self.notification_handler(
                            str(message["method"]),
                            params if isinstance(params, dict) else {},
                        )
                    except Exception as exc:
                        LOG.error(
                            "notification handler failed: %s",
                            redact_sensitive_text(exc),
                        )
                    continue
                response_id = message.get("id")
                if isinstance(response_id, int):
                    with self._pending_lock:
                        pending = self._pending.get(response_id)
                    if pending:
                        try:
                            pending.put_nowait(message)
                        except queue.Full:
                            pass
        finally:
            failure = {
                "error": {
                    "code": -32001,
                    "message": "codex app-server exited",
                }
            }
            with self._pending_lock:
                pending_queues = list(self._pending.values())
            for pending in pending_queues:
                try:
                    pending.put_nowait(failure)
                except queue.Full:
                    pass

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        params = message.get("params")
        try:
            result = self.server_request_handler(
                str(message.get("method")),
                params if isinstance(params, dict) else {},
            )
            self.respond(request_id, result=result)
        except Exception as exc:
            LOG.error(
                "server request handler failed for %s: %s",
                message.get("method"),
                redact_sensitive_text(exc),
            )
            try:
                self.respond(
                    request_id,
                    error={"code": -32000, "message": redact_sensitive_text(exc)},
                )
            except RpcError:
                pass

    def _stderr_loop(self) -> None:
        process = self.process
        if not process or process.stderr is None:
            return
        for line in process.stderr:
            stripped = line.rstrip()
            if stripped:
                LOG.info("app-server: %s", redact_sensitive_text(stripped))

    def close(self) -> None:
        self._closed = True
        process = self.process
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def external_open_thread_ids(exclude_pids: set[int]) -> set[str]:
    """Best-effort guard against resuming a thread already open in another Codex."""

    found: set[str] = set()
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return found
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) in exclude_pids:
            continue
        try:
            cmdline = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").lower()
        except OSError:
            continue
        if b"codex" not in cmdline:
            continue
        fd_dir = process_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if "rollout-" not in target:
                continue
            found.update(match.group(0) for match in UUID_RE.finditer(target))
    return found


def _thread_title(thread: dict[str, Any]) -> str:
    title = thread.get("name") or thread.get("preview") or f"Codex {str(thread.get('id', ''))[:8]}"
    normalized = " ".join(str(title).split())
    return normalized[:100] or "Codex"


def _thread_status(thread: dict[str, Any], externally_open: bool = False) -> str:
    if externally_open:
        return "busy"
    raw = thread.get("status")
    kind = raw.get("type") if isinstance(raw, dict) else raw
    if kind == "active":
        return "busy"
    if kind == "systemError":
        return "retry"
    return "idle"


def _message_envelopes(thread: dict[str, Any]) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return envelopes
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                texts: list[str] = []
                for content in item.get("content") or []:
                    if (
                        isinstance(content, dict)
                        and content.get("type") == "text"
                        and isinstance(content.get("text"), str)
                    ):
                        texts.append(content["text"])
                if texts:
                    envelopes.append(
                        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "\n".join(texts)}]}
                    )
            elif item_type == "agentMessage" and isinstance(item.get("text"), str):
                envelopes.append(
                    {
                        "info": {"role": "assistant"},
                        "parts": [{"type": "text", "text": item["text"]}],
                    }
                )
    return envelopes


class CodexBridge:
    def __init__(self) -> None:
        self.config = load_telegram_config()
        self.active_threads: set[str] = set()
        self.last_agent_messages: dict[str, str] = {}
        self.thread_titles: dict[str, str] = {}
        self._state_lock = threading.Lock()
        self.rpc = AppServerClient(self.handle_server_request, self.handle_notification)

    def start(self) -> None:
        self.rpc.start()
        register_session(
            "codex-telegram-bridge",
            title="Codex Telegram Bridge",
            cwd=str(Path.home()),
            status="idle",
            config=self.config,
        )

    def list_threads(self) -> list[dict[str, Any]]:
        result = self.rpc.request("thread/list", {"limit": 100}, timeout=60)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            return []
        return [
            thread
            for thread in result["data"]
            if isinstance(thread, dict) and not thread.get("parentThreadId")
        ]

    def get_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any] | None:
        try:
            result = self.rpc.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": include_turns},
                timeout=60,
            )
        except RpcError:
            return None
        thread = result.get("thread") if isinstance(result, dict) else None
        return thread if isinstance(thread, dict) else None

    def open_thread_ids(self) -> set[str]:
        exclude = {os.getpid()}
        if self.rpc.pid:
            exclude.add(self.rpc.pid)
        return external_open_thread_ids(exclude)

    def session_list(self) -> list[dict[str, Any]]:
        external = self.open_thread_ids()
        records: list[dict[str, Any]] = []
        for thread in self.list_threads():
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                continue
            title = _thread_title(thread)
            self.thread_titles[thread_id] = title
            updated = int(thread.get("updatedAt") or thread.get("createdAt") or time.time())
            records.append(
                {
                    "id": thread_id,
                    "title": title,
                    "parentID": None,
                    "agent": "codex",
                    "time": {"updated": updated * 1000},
                    "_status": _thread_status(thread, thread_id in external),
                }
            )
        return records

    def status_map(self) -> dict[str, dict[str, str]]:
        return {
            record["id"]: {"type": record.pop("_status", "idle")}
            for record in self.session_list()
        }

    def session_get(self, thread_id: str) -> dict[str, Any] | None:
        thread = self.get_thread(thread_id)
        if not thread:
            return None
        title = _thread_title(thread)
        self.thread_titles[thread_id] = title
        return {
            "id": thread_id,
            "directory": str(thread.get("cwd") or Path.home()),
            "title": title,
            "parentID": thread.get("parentThreadId"),
            "agent": "codex",
        }

    def session_messages(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        thread = self.get_thread(thread_id, include_turns=True)
        if not thread:
            return []
        return _message_envelopes(thread)[-limit:]

    def start_work(self, thread_id: str) -> None:
        if thread_id in self.open_thread_ids():
            raise RpcError("다른 Codex 프로세스에서 이미 열린 세션입니다")
        statuses = self.status_map()
        if statuses.get(thread_id, {"type": "idle"})["type"] != "idle":
            raise RpcError("세션이 idle 상태가 아닙니다")
        resumed = self.rpc.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": "workspace-write",
            },
            timeout=120,
        )
        if not isinstance(resumed, dict):
            raise RpcError("thread/resume returned no data")
        self.thread_titles[thread_id] = _thread_title(resumed)
        cwd = str(resumed.get("cwd") or Path.home())
        self.rpc.request(
            "turn/start",
            {
                "threadId": thread_id,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [cwd],
                    "networkAccess": False,
                },
                "input": [
                    {
                        "type": "text",
                        "text": (
                            "사용자가 Telegram에서 이 Codex 세션의 작업 재개를 "
                            "요청했습니다. 기존 목표와 미완료 작업을 확인하고 안전하게 "
                            "계속 진행하세요. 필요한 질문과 승인은 Telegram 연동을 "
                            "사용하세요."
                        ),
                    }
                ],
            },
            timeout=120,
        )

    def handle_server_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(params.get("threadId") or "codex")
        if method == "item/tool/requestUserInput":
            converted: list[dict[str, Any]] = []
            question_ids: list[str] = []
            for index, question in enumerate(params.get("questions") or []):
                if not isinstance(question, dict):
                    continue
                question_id = str(question.get("id") or f"question_{index}")
                question_ids.append(question_id)
                options = []
                for option in question.get("options") or []:
                    if isinstance(option, dict) and isinstance(option.get("label"), str):
                        options.append(
                            {
                                "label": option["label"],
                                "description": str(option.get("description") or ""),
                            }
                        )
                converted.append(
                    {
                        "header": str(question.get("header") or f"질문 {index + 1}"),
                        "question": str(question.get("question") or ""),
                        "options": options,
                        "multiple": False,
                        "custom": True,
                    }
                )
            answers = ask_questions(
                converted,
                session_id=thread_id,
                server_url=bridge_url(),
                session_name=self.thread_titles.get(
                    thread_id,
                    _thread_title({"id": thread_id}),
                ),
                config=self.config,
            )
            return {
                "answers": {
                    question_id: {"answers": answers[index] if index < len(answers) else []}
                    for index, question_id in enumerate(question_ids)
                }
            }

        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            if method == "item/commandExecution/requestApproval":
                permission = "Bash"
                title = str(params.get("reason") or params.get("command") or "명령 실행")
                patterns = [str(params["command"])] if params.get("command") else []
            elif method == "item/fileChange/requestApproval":
                permission = "apply_patch"
                title = str(params.get("reason") or params.get("grantRoot") or "파일 변경")
                patterns = [str(params["grantRoot"])] if params.get("grantRoot") else []
            else:
                permission = "permissions"
                title = str(params.get("reason") or json.dumps(params.get("permissions"), ensure_ascii=False))
                patterns = []
            reply = request_permission(
                session_id=thread_id,
                title=title,
                permission=permission,
                patterns=patterns,
                directory=str(params.get("cwd") or Path.home()),
                config=self.config,
            )
            if method == "item/permissions/requestApproval":
                if reply == "reject":
                    return {"permissions": {}, "scope": "turn"}
                requested = params.get("permissions")
                return {
                    "permissions": requested if isinstance(requested, dict) else {},
                    "scope": "session" if reply == "always" else "turn",
                }
            if reply == "reject":
                return {"decision": "decline"}
            return {"decision": "acceptForSession" if reply == "always" else "accept"}

        raise RpcError(f"unsupported app-server request: {method}")

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId") or "")
        if method == "turn/started":
            turn = params.get("turn")
            if isinstance(turn, dict):
                thread_id = str(params.get("threadId") or turn.get("threadId") or thread_id)
            if thread_id:
                with self._state_lock:
                    self.active_threads.add(thread_id)
            return
        if method == "thread/status/changed":
            status = params.get("status")
            status_type = status.get("type") if isinstance(status, dict) else status
            if thread_id:
                with self._state_lock:
                    if status_type == "active":
                        self.active_threads.add(thread_id)
                    else:
                        self.active_threads.discard(thread_id)
            return
        if method == "item/completed":
            item = params.get("item")
            if (
                thread_id
                and isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and isinstance(item.get("text"), str)
            ):
                with self._state_lock:
                    self.last_agent_messages[thread_id] = item["text"]
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, dict):
                thread_id = str(params.get("threadId") or turn.get("threadId") or thread_id)
            if not thread_id:
                return
            with self._state_lock:
                self.active_threads.discard(thread_id)
                message = self.last_agent_messages.get(thread_id)
            if message:
                threading.Thread(
                    target=self._notify_completion,
                    args=(thread_id, message),
                    daemon=True,
                ).start()

    def _notify_completion(self, thread_id: str, message: str) -> None:
        try:
            thread = self.get_thread(thread_id)
            title = _thread_title(thread or {"id": thread_id})
            send_codex_notification(
                message,
                title=f"Codex 완료 · {title}",
                config=self.config,
            )
        except (TelegramBridgeError, RpcError) as exc:
            LOG.error(
                "completion notification failed: %s",
                redact_sensitive_text(exc),
            )

    def close(self) -> None:
        self.rpc.close()


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], bridge: CodexBridge) -> None:
        super().__init__(address, BridgeRequestHandler)
        self.bridge = bridge


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "CodexTelegramBridge/0.1"

    @property
    def bridge(self) -> CodexBridge:
        server = self.server
        assert isinstance(server, BridgeHTTPServer)
        return server.bridge

    def _json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _trusted_local_request(self) -> bool:
        host_value = self.headers.get("Host", "")
        try:
            host = urllib.parse.urlsplit(f"//{host_value}").hostname
        except ValueError:
            return False
        if host not in {"127.0.0.1", "::1", "localhost"}:
            return False

        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed_origin = urllib.parse.urlsplit(origin)
            except ValueError:
                return False
            if (
                parsed_origin.scheme != "http"
                or parsed_origin.hostname not in {"127.0.0.1", "::1", "localhost"}
            ):
                return False
        return self.headers.get("Sec-Fetch-Site", "").lower() != "cross-site"

    def do_GET(self) -> None:  # noqa: N802
        if not self._trusted_local_request():
            self._json(403, {"error": "untrusted request origin"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if path == "/healthz":
                self._json(
                    200,
                    {
                        "ok": True,
                        "app_server_pid": self.bridge.rpc.pid,
                        "telegram_polling": False,
                    },
                )
                return
            if path == "/session":
                records = self.bridge.session_list()
                for record in records:
                    record.pop("_status", None)
                self._json(200, records)
                return
            if path == "/session/status":
                self._json(200, self.bridge.status_map())
                return
            match = re.fullmatch(r"/session/([^/]+)/message", path)
            if match:
                thread_id = urllib.parse.unquote(match.group(1))
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = min(max(int(query.get("limit", ["10"])[0]), 1), 100)
                except ValueError:
                    limit = 10
                self._json(200, self.bridge.session_messages(thread_id, limit))
                return
            match = re.fullmatch(r"/session/([^/]+)", path)
            if match:
                thread_id = urllib.parse.unquote(match.group(1))
                session = self.bridge.session_get(thread_id)
                self._json(200 if session else 404, session or {"error": "not found"})
                return
            self._json(404, {"error": "not found"})
        except (RpcError, TelegramBridgeError) as exc:
            self._json(503, {"error": redact_sensitive_text(exc)})
        except Exception as exc:
            LOG.error("GET request failed: %s", redact_sensitive_text(exc))
            self._json(500, {"error": redact_sensitive_text(exc)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._trusted_local_request():
            self._json(403, {"error": "untrusted request origin"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        match = re.fullmatch(r"/session/([^/]+)/command", parsed.path)
        if not match:
            self._json(404, {"error": "not found"})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("command") != "start-work":
                self._json(400, {"error": "unsupported command"})
                return
            thread_id = urllib.parse.unquote(match.group(1))
            self.bridge.start_work(thread_id)
            self._json(202, {"ok": True})
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
        except (RpcError, TelegramBridgeError) as exc:
            self._json(409, {"error": redact_sensitive_text(exc)})
        except Exception as exc:
            LOG.error("POST request failed: %s", redact_sensitive_text(exc))
            self._json(500, {"error": redact_sensitive_text(exc)})

    def log_message(self, _fmt: str, *_args: Any) -> None:
        return


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CODEX_TELEGRAM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("CODEX_TELEGRAM_BRIDGE_HOST", DEFAULT_BRIDGE_HOST)
    port = int(os.environ.get("CODEX_TELEGRAM_BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT)))
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Bridge must bind to localhost")
    bridge = CodexBridge()
    bridge.start()
    server = BridgeHTTPServer((host, port), bridge)
    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        if stop_event.is_set():
            return
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    LOG.info("bridge listening on http://%s:%s", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
