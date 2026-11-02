"""Serve daemon subpackage: resident search/index service for #780.

Split so each transport (Unix/TCP socket, HTTP) and the thin client can reuse
one :class:`ServeService` without importing one another. This module re-exports
the stable entry points; transport adapters and the daemon loop arrive in later
commits of the same effort.
"""

from __future__ import annotations

from .exit_codes import EXIT_ALREADY_RUNNING, EXIT_DAEMON_DOWN, EXIT_ERROR, EXIT_OK
from .pidfile import DaemonAlreadyRunningError, acquire, default_socket_and_pid, is_pid_alive, read_pid
from .protocol import RpcError, ServeProtocolError, parse_message, request_line, response_line
from .service import ServeService

__all__ = [
    "EXIT_ALREADY_RUNNING",
    "EXIT_DAEMON_DOWN",
    "EXIT_ERROR",
    "EXIT_OK",
    "DaemonAlreadyRunningError",
    "RpcError",
    "ServeProtocolError",
    "ServeService",
    "acquire",
    "default_socket_and_pid",
    "is_pid_alive",
    "parse_message",
    "read_pid",
    "request_line",
    "response_line",
]
