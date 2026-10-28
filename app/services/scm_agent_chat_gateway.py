"""Stateless, read-only OpenClaw gateway for the first real SCM agent UI.

The API validates the authenticated SCM actor and the request shape.  This
module deliberately has no Flask imports: it only receives a validated config
and plain message data, then returns a small, sanitized provider result.
"""

import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from uuid import UUID
from urllib.parse import urlsplit

import requests

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows UAT only
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX only
    msvcrt = None


MAX_QUERY_CHARS = 4000
MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_BYTES = 16000
MAX_SERIALIZED_BYTES = 24000
MAX_OUTPUT_BYTES = 128 * 1024
MAX_ANSWER_CHARS = 32000
DEADLINE_SECONDS = 150.0
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 147.0
MODEL = "openclaw/scm-personal"
LIMITATION = "Las fuentes citadas forman parte de la respuesta del agente; esta interfaz no expone trazas de herramientas."

_IN_FLIGHT = threading.BoundedSemaphore(1)
_LEASE_PATH = (
    Path("/tmp/scm-agent-chat.inflight.lock")
    if os.name != "nt"
    else Path(tempfile.gettempdir()) / "scm-agent-chat.inflight.lock"
)
_SAFE_MODEL = re.compile(r"[A-Za-z0-9_./:-]{1,160}\Z")


class AgentGatewayError(RuntimeError):
    """A provider failure safe to expose through the HTTP adapter."""

    def __init__(self, code, message, *, status_code=503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _error(code, message, *, status_code=503):
    raise AgentGatewayError(code, message, status_code=status_code)


def _plain_text(value, *, field):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_MESSAGE_CHARS:
        _error("AGENT_INVALID_REQUEST", f"El campo {field} debe ser texto de 1 a 4000 caracteres.", status_code=400)
    if "\x00" in value:
        _error("AGENT_INVALID_REQUEST", f"El campo {field} debe ser texto plano.", status_code=400)
    return value


def validate_messages(query, history):
    """Validate the bounded user/assistant pair history and return a copy."""
    query = _plain_text(query, field="query")
    if not isinstance(history, list) or len(history) > MAX_HISTORY_MESSAGES or len(history) % 2:
        _error("AGENT_INVALID_REQUEST", "history debe contener hasta 12 mensajes en pares completos.", status_code=400)
    clean_history = []
    expected = "user"
    for item in history:
        if not isinstance(item, dict) or set(item) != {"role", "content"} or item.get("role") != expected:
            _error("AGENT_INVALID_REQUEST", "history solo admite pares alternados user/assistant.", status_code=400)
        clean_history.append({"role": expected, "content": _plain_text(item["content"], field="history.content")})
        expected = "assistant" if expected == "user" else "user"
    if len(json.dumps(clean_history, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_HISTORY_BYTES:
        _error("AGENT_PAYLOAD_LIMIT", "history supera el tamaño permitido.", status_code=413)
    return query, clean_history


def _read_token(config):
    token = config.get("SCM_OPENCLAW_TOKEN")
    token_file = config.get("SCM_OPENCLAW_TOKEN_FILE")
    if token_file:
        try:
            with Path(str(token_file)).open("r", encoding="utf-8") as secret:
                token = secret.read(4097).strip()
        except (OSError, UnicodeError):
            return None
    if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(char.isspace() for char in token):
        return None
    return token


def _destination(config):
    base = str(config.get("SCM_OPENCLAW_URL") or "http://127.0.0.1:18789").rstrip("/")
    parsed = urlsplit(base)
    private_host = config.get("SCM_OPENCLAW_PRIVATE_HOST")
    allowed = {"127.0.0.1", "localhost", "::1"}
    if private_host:
        allowed.add(str(private_host))
    host = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or host not in allowed
        or (parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"})
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    ca_file = config.get("SCM_OPENCLAW_CA_FILE")
    if ca_file and not Path(str(ca_file)).is_file():
        return None
    return base, (str(ca_file) if ca_file else True)


def _validated_config(config):
    if (
        not isinstance(config, dict)
        or str(config.get("SCM_AGENT_CHAT_ENABLED") or "").lower() != "true"
        or config.get("SCM_ASSISTANT_PROVIDER") != "openclaw"
    ):
        return None
    if str(config.get("SCM_OPENCLAW_POLICY_CONFIRMED") or "").lower() != "true":
        return None
    token = _read_token(config)
    destination = _destination(config)
    backend_model = config.get("SCM_OPENCLAW_BACKEND_MODEL")
    if backend_model and (
        str(config.get("SCM_OPENCLAW_MODEL_VERIFIED") or "").lower() != "true"
        or not _SAFE_MODEL.fullmatch(str(backend_model))
    ):
        return None
    if token is None or destination is None:
        return None
    return token, destination, (str(backend_model) if backend_model else None)


def readiness(config):
    """Return the public status shape without touching the provider."""
    if _validated_config(config) is None:
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "message": "El proveedor OpenClaw no está disponible para consultas de solo lectura.",
            "provider": "openclaw",
            "read_only": True,
            "attachments_supported": False,
        }
    return {
        "available": True,
        "status": "READY",
        "message": "Proveedor OpenClaw disponible para consultas de solo lectura.",
        "provider": "openclaw",
        "read_only": True,
        "attachments_supported": False,
    }


def _open_process_lease():
    flags = os.O_RDWR | os.O_CREAT
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    handle = None
    try:
        handle = os.fdopen(os.open(str(_LEASE_PATH), flags | no_follow, 0o600), "a+b")
        try:
            os.fchmod(handle.fileno(), 0o600)
        except AttributeError:
            try:
                os.chmod(str(_LEASE_PATH), 0o600)
            except OSError:
                pass
        except OSError:
            pass
        if msvcrt is not None and fcntl is None:
            if os.path.getsize(str(_LEASE_PATH)) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
        return handle
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return None


def _acquire_process_lease():
    """Take a nonblocking cross-worker lease without writing request metadata."""
    handle = _open_process_lease()
    if handle is None:
        return None
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - an unsupported platform must fail closed
            handle.close()
            return None
        return handle
    except (BlockingIOError, OSError):
        try:
            handle.close()
        except OSError:
            pass
        return None


def _release_process_lease(handle):
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except (OSError, ValueError):
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def _own_ip():
    try:
        value = socket.gethostbyname(socket.gethostname())
        ipaddress.ip_address(value)
        return value
    except (OSError, ValueError):
        return None


def _provider_error(result):
    status = result.get("status") if isinstance(result, dict) else None
    if status == "TIMEOUT":
        return AgentGatewayError("AGENT_TIMEOUT", "El proveedor no respondió dentro del tiempo permitido.", status_code=504)
    return AgentGatewayError("AGENT_UNAVAILABLE", "El proveedor no está disponible en este momento.", status_code=503)


def _set_response_read_timeout(response, seconds):
    """Best-effort tightening of urllib3's socket timeout for the next read."""
    raw = getattr(response, "raw", None)
    candidates = [
        getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None),
        getattr(getattr(raw, "_connection", None), "sock", None),
    ]
    for candidate in candidates:
        sock = getattr(candidate, "_sock", candidate)
        setter = getattr(sock, "settimeout", None)
        if setter is not None:
            try:
                setter(max(0.001, seconds))
                return
            except (OSError, AttributeError):
                continue


def _complete(payload, config):
    validated = _validated_config(config)
    if validated is None:
        return {"status": "UNAVAILABLE", "usage": None}
    token, (base, verify), backend_model = validated
    own_ip = _own_ip()
    if own_ip is None:
        return {"status": "UNAVAILABLE", "usage": None}
    headers = {
        "Authorization": "Bearer " + token,
        "X-Forwarded-For": own_ip,
        "X-Forwarded-Proto": "https",
    }
    if backend_model:
        headers["x-openclaw-model"] = backend_model
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_SERIALIZED_BYTES:
        return {"status": "INPUT_LIMIT", "usage": None}
    started = time.monotonic()
    try:
        with requests.Session() as transport:
            transport.trust_env = False
            with transport.post(
                base + "/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                allow_redirects=False,
                stream=True,
                verify=verify,
            ) as response:
                if response.status_code != 200:
                    return {"status": "UNAVAILABLE", "usage": None}
                chunks, size = [], 0
                chunks_iterator = iter(response.iter_content(4096))
                while True:
                    remaining = DEADLINE_SECONDS - (time.monotonic() - started)
                    if remaining <= 0:
                        return {"status": "TIMEOUT", "usage": None}
                    _set_response_read_timeout(response, min(READ_TIMEOUT_SECONDS, remaining))
                    try:
                        chunk = next(chunks_iterator)
                    except StopIteration:
                        break
                    size += len(chunk)
                    if size > MAX_OUTPUT_BYTES:
                        return {"status": "OUTPUT_LIMIT", "usage": None}
                    chunks.append(chunk)
                if time.monotonic() - started > DEADLINE_SECONDS:
                    return {"status": "TIMEOUT", "usage": None}
                result = json.loads(b"".join(chunks))
        choice = result["choices"][0]
        message = choice.get("message") if isinstance(choice, dict) else None
        answer = message.get("content") if isinstance(message, dict) else None
        if (
            choice.get("finish_reason") != "stop"
            or not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("tool_calls")
            or not isinstance(answer, str)
            or not 1 <= len(answer) <= MAX_ANSWER_CHARS
            or "\x00" in answer
        ):
            return {"status": "INVALID_RESPONSE", "usage": None}
        raw_usage = result.get("usage") if isinstance(result, dict) else None
        usage = {
            key: raw_usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(raw_usage, dict)
            and isinstance(raw_usage.get(key), int)
            and not isinstance(raw_usage.get(key), bool)
            and raw_usage[key] >= 0
        }
        return {"status": "answered", "answer": answer, "usage": usage or None}
    except (requests.Timeout, TimeoutError):
        return {"status": "TIMEOUT", "usage": None}
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, OSError):
        return {"status": "UNAVAILABLE", "usage": None}


def complete_agent_chat(query, history, client_request_id, config):
    """Complete one bounded stateless chat request through OpenClaw."""
    query, history = validate_messages(query, history)
    if str(config.get("SCM_AGENT_CHAT_ENABLED") or "").lower() != "true":
        _error("AGENT_DISABLED", "El chat del agente no está habilitado.", status_code=503)
    try:
        UUID(str(client_request_id))
    except (ValueError, AttributeError, TypeError):
        _error("AGENT_INVALID_REQUEST", "client_request_id debe ser UUID.", status_code=400)
    messages = list(history) + [{"role": "user", "content": query}]
    payload = {
        "model": MODEL,
        "stream": False,
        "max_completion_tokens": 4000,
        "messages": messages,
    }
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_SERIALIZED_BYTES:
        _error("AGENT_PAYLOAD_LIMIT", "La solicitud supera el tamaño permitido.", status_code=413)
    if not _IN_FLIGHT.acquire(blocking=False):
        _error("AGENT_BUSY", "El agente está atendiendo otra consulta; vuelve a intentar.", status_code=503)
    try:
        lease = _acquire_process_lease()
    except Exception:
        _IN_FLIGHT.release()
        _error("AGENT_UNAVAILABLE", "El agente no está disponible en este momento.", status_code=503)
    if lease is None:
        _IN_FLIGHT.release()
        _error("AGENT_BUSY", "El agente está atendiendo otra consulta; vuelve a intentar.", status_code=503)
    try:
        try:
            result = _complete(payload, config)
        except Exception:
            result = {"status": "UNAVAILABLE", "usage": None}
    finally:
        _release_process_lease(lease)
        _IN_FLIGHT.release()
    if result.get("status") != "answered":
        raise _provider_error(result)
    return result

