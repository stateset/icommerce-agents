"""Builds the one ``AsyncAnthropic`` client both the host and ``evals/run.py`` use.

An identity-linked API key requires every request to carry an ``anthropic-workspace-id``
header naming the workspace the request acts in; an unlinked key must not carry it. This
module reads ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_WORKSPACE_ID`` from the environment
(loading ``.env`` first, since both live there for local runs) and returns a client
configured accordingly -- it never logs or returns either value.

Importable by both ``host/app.py`` and ``evals/run.py`` without either importing the
other.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

load_dotenv()


def build_anthropic_client() -> AsyncAnthropic | None:
    """Return a configured client, or ``None`` when no API key is set.

    With ``ANTHROPIC_WORKSPACE_ID`` set, the returned client sends it as the
    ``anthropic-workspace-id`` header on every request, as an identity-linked key
    requires. Without it, the client is unconfigured and behaves exactly as before.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    from anthropic import AsyncAnthropic

    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if workspace_id:
        return AsyncAnthropic(default_headers={"anthropic-workspace-id": workspace_id})
    return AsyncAnthropic()
