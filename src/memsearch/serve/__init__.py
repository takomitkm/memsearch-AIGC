"""Serve daemon subpackage: resident search/index service for #780.

Split so each transport (Unix/TCP socket, HTTP) and the thin client can reuse
one :class:`ServeService` without importing one another. This module re-exports
the stable entry points: the daemon loop (:meth:`run_daemon`), the transports
(:class:`SocketServer`, :class:`HttpServer`), and the thin client
(:class:`ThinClient`) the CLI's automatic routing rides on.
"""

from __future__ import annotations

from .client import ENV_NO_DAEMON, ENV_SOCKET, ENV_TCP_PORT, DaemonDownError, ThinClient, daemon_client_if_alive
from .daemon import run_daemon
from .exit_codes import EXIT_ALREADY_RUNNING, EXIT_DAEMON_DOWN, EXIT_ERROR, EXIT_OK
from .http_server import HttpServer
from .pidfile import DaemonAlreadyRunningError, acquire, default_socket_and_pid, is_pid_alive, read_pid
from .protocol import RpcError, ServeProtocolError, parse_message, request_line, response_line
from .service import ServeService
from .socket_server import DEFAULT_TCP_HOST, DEFAULT_TCP_PORT, SocketServer

__all__ = [
    "DEFAULT_TCP_HOST",
    "DEFAULT_TCP_PORT",
    "ENV_NO_DAEMON",
    "ENV_SOCKET",
    "ENV_TCP_PORT",
    "EXIT_ALREADY_RUNNING",
    "EXIT_DAEMON_DOWN",
    "EXIT_ERROR",
    "EXIT_OK",
    "DaemonAlreadyRunningError",
    "DaemonDownError",
    "HttpServer",
    "RpcError",
    "ServeProtocolError",
    "ServeService",
    "SocketServer",
    "ThinClient",
    "acquire",
    "daemon_client_if_alive",
    "default_socket_and_pid",
    "is_pid_alive",
    "parse_message",
    "read_pid",
    "request_line",
    "response_line",
    "run_daemon",
]
