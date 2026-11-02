"""Exit codes for the serve daemon, its thin client, and CLI routing.

The daemon-down code (2) is the contract issue #780 asks for: a client whose
daemon has died must signal "service unavailable" rather than return an empty
result, so callers can distinguish "no matches" from "no server".
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DAEMON_DOWN = 2
EXIT_ALREADY_RUNNING = 3
