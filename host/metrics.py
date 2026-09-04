"""Small dependency-free Prometheus counters for the reference host."""

from __future__ import annotations

import threading
from collections import Counter


class HostMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._policy_rewrites: Counter[str] = Counter()
        self._kernel_commands: Counter[tuple[str, str]] = Counter()

    def request(self, method: str, route: str, status: int) -> None:
        with self._lock:
            self._requests[(method, route, status)] += 1

    def policy_rewrite(self, role: str) -> None:
        with self._lock:
            self._policy_rewrites[role] += 1

    def kernel_command(self, command: str, outcome: str) -> None:
        with self._lock:
            self._kernel_commands[(command, outcome)] += 1

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            rewrites = dict(self._policy_rewrites)
            commands = dict(self._kernel_commands)
        lines = [
            "# HELP icommerce_http_requests_total Completed HTTP requests.",
            "# TYPE icommerce_http_requests_total counter",
        ]
        for (method, route, status), count in sorted(requests.items()):
            lines.append(
                f'icommerce_http_requests_total{{method="{method}",route="{route}",'
                f'status="{status}"}} {count}'
            )
        lines.extend(
            [
                "# HELP icommerce_response_policy_rewrites_total Model responses rewritten.",
                "# TYPE icommerce_response_policy_rewrites_total counter",
            ]
        )
        for role, count in sorted(rewrites.items()):
            lines.append(f'icommerce_response_policy_rewrites_total{{role="{role}"}} {count}')
        lines.extend(
            [
                "# HELP icommerce_kernel_commands_total Governed kernel command outcomes.",
                "# TYPE icommerce_kernel_commands_total counter",
            ]
        )
        for (command, outcome), count in sorted(commands.items()):
            lines.append(
                f'icommerce_kernel_commands_total{{command="{command}",'
                f'outcome="{outcome}"}} {count}'
            )
        return "\n".join(lines) + "\n"


__all__ = ["HostMetrics"]
