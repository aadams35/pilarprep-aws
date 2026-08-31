from __future__ import annotations

import os
from contextlib import nullcontext
from typing import Any, Mapping
from uuid import uuid4

from common.identifiers import stable_identifier


def memory_session(scope: Mapping[str, str]):
    memory_id = os.getenv("AGENTCORE_MEMORY_ID", "")
    if not memory_id:
        return nullcontext(None)

    from bedrock_agentcore.memory.integrations.strands.config import (
        AgentCoreMemoryConfig,
    )
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )

    actor_id = stable_identifier(
        "project-actor",
        [scope["tenantId"], scope["clientId"], scope["projectId"]],
        length=40,
    )
    session_id = stable_identifier(
        "project-session",
        # Batch jobs reload authoritative project state, not previous model conversations.
        # Retain a scoped audit trail without replaying failed attempts or old packets.
        [scope["tenantId"], scope["clientId"], scope["projectId"], scope["sessionId"], uuid4().hex],
        length=40,
    )
    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        actor_id=actor_id,
        session_id=session_id,
        batch_size=1,
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )
