"""Settings for the two MCP servers, read from the environment once and validated.

Each server binds a single process-level principal: the shopping server one customer,
the merchant server one operator. Identity is never a tool argument, so it has to come
from here. The variable names are the ones ``docs/mcp.md`` documents.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "acme.db"


@dataclass(frozen=True)
class McpSettings:
    role: Literal["shopping", "merchant"]
    host: str
    port: int
    session_id: str
    principal: str
    db_path: str
    memory_file: Path
    unsafe_env_var: str

    @classmethod
    def shopping(cls, env: Mapping[str, str] | None = None) -> McpSettings:
        e = os.environ if env is None else env
        return cls(
            role="shopping",
            host=e.get("STOREFRONT_MCP_HOST", "127.0.0.1"),
            port=_port(e.get("STOREFRONT_MCP_PORT", "8300"), "STOREFRONT_MCP_PORT"),
            session_id=e.get("STOREFRONT_MCP_SESSION_ID", "mcp-shopping"),
            principal=e.get("ACME_CUSTOMER", "rowan@example.invalid"),
            db_path=e.get("STOREFRONT_MCP_DB", str(DEFAULT_DB_PATH)),
            memory_file=Path(
                e.get("STOREFRONT_MCP_MEMORY_FILE", str(REPO_ROOT / ".storefront_mcp_memory.json"))
            ),
            unsafe_env_var="STOREFRONT_MCP_UNSAFE_ALLOW_NO_AUTH",
        ).validated()

    @classmethod
    def merchant(cls, env: Mapping[str, str] | None = None) -> McpSettings:
        e = os.environ if env is None else env
        return cls(
            role="merchant",
            host=e.get("MERCHANT_MCP_HOST", "127.0.0.1"),
            port=_port(e.get("MERCHANT_MCP_PORT", "8301"), "MERCHANT_MCP_PORT"),
            session_id=e.get("MERCHANT_MCP_SESSION_ID", "mcp-merchant"),
            principal=e.get("ACME_OPERATOR", "user:acme-operator"),
            db_path=e.get("MERCHANT_MCP_DB", str(DEFAULT_DB_PATH)),
            memory_file=Path(
                e.get("MERCHANT_MCP_MEMORY_FILE", str(REPO_ROOT / ".merchant_mcp_memory.json"))
            ),
            unsafe_env_var="MERCHANT_MCP_UNSAFE_ALLOW_NO_AUTH",
        ).validated()

    def validated(self) -> McpSettings:
        if not self.host.strip():
            raise ValueError(f"{self.role} MCP host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"{self.role} MCP port must be between 1 and 65535")
        if not self.session_id or self.session_id != self.session_id.strip():
            raise ValueError(f"{self.role} MCP session id must be a non-empty token")
        if not self.principal.strip():
            raise ValueError(f"{self.role} MCP principal must not be empty")
        if not self.db_path.strip() or self.db_path == ":memory:":
            raise ValueError(f"{self.role} MCP database must be a file path")
        return self

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"


def _port(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer port, got {value!r}") from error
