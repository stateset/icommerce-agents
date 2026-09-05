"""Small dependency-free Prometheus counters for the reference host."""

from __future__ import annotations

import threading
from collections import Counter, defaultdict


class HostMetrics:
    _DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._policy_rewrites: Counter[str] = Counter()
        self._kernel_commands: Counter[tuple[str, str]] = Counter()
        self._stablecoin_payments: Counter[tuple[str, str]] = Counter()
        self._duration_buckets: Counter[tuple[str, str, float]] = Counter()
        self._duration_count: Counter[tuple[str, str]] = Counter()
        self._duration_sum: defaultdict[tuple[str, str], float] = defaultdict(float)

    def request(self, method: str, route: str, status: int, duration_seconds: float) -> None:
        with self._lock:
            self._requests[(method, route, status)] += 1
            key = (method, route)
            self._duration_count[key] += 1
            self._duration_sum[key] += duration_seconds
            for bucket in self._DURATION_BUCKETS:
                if duration_seconds <= bucket:
                    self._duration_buckets[(method, route, bucket)] += 1

    def policy_rewrite(self, role: str) -> None:
        with self._lock:
            self._policy_rewrites[role] += 1

    def kernel_command(self, command: str, outcome: str) -> None:
        with self._lock:
            self._kernel_commands[(command, outcome)] += 1

    def stablecoin_payment(self, action: str, outcome: str) -> None:
        with self._lock:
            self._stablecoin_payments[(action, outcome)] += 1

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            rewrites = dict(self._policy_rewrites)
            commands = dict(self._kernel_commands)
            stablecoin_payments = dict(self._stablecoin_payments)
            duration_buckets = dict(self._duration_buckets)
            duration_count = dict(self._duration_count)
            duration_sum = dict(self._duration_sum)
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
                "# HELP icommerce_http_request_duration_seconds HTTP request latency.",
                "# TYPE icommerce_http_request_duration_seconds histogram",
            ]
        )
        for (method, route), count in sorted(duration_count.items()):
            for bucket in self._DURATION_BUCKETS:
                bucket_count = duration_buckets.get((method, route, bucket), 0)
                lines.append(
                    "icommerce_http_request_duration_seconds_bucket"
                    f'{{method="{method}",route="{route}",le="{bucket:g}"}} {bucket_count}'
                )
            lines.append(
                "icommerce_http_request_duration_seconds_bucket"
                f'{{method="{method}",route="{route}",le="+Inf"}} {count}'
            )
            lines.append(
                "icommerce_http_request_duration_seconds_sum"
                f'{{method="{method}",route="{route}"}} {duration_sum[(method, route)]:.9f}'
            )
            lines.append(
                "icommerce_http_request_duration_seconds_count"
                f'{{method="{method}",route="{route}"}} {count}'
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
        lines.extend(
            [
                "# HELP icommerce_stablecoin_payments_total Stablecoin payment outcomes.",
                "# TYPE icommerce_stablecoin_payments_total counter",
            ]
        )
        for (action, outcome), count in sorted(stablecoin_payments.items()):
            lines.append(
                f'icommerce_stablecoin_payments_total{{action="{action}",'
                f'outcome="{outcome}"}} {count}'
            )
        return "\n".join(lines) + "\n"


__all__ = ["HostMetrics"]
