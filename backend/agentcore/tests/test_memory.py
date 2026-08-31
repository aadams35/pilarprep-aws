from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import memory


class MemoryTests(unittest.TestCase):
    def test_repeated_batch_jobs_have_fresh_history_and_the_same_scoped_actor(self):
        configs = []
        fake_config = types.ModuleType("bedrock_agentcore.memory.integrations.strands.config")
        fake_sessions = types.ModuleType("bedrock_agentcore.memory.integrations.strands.session_manager")

        def configure(**values):
            configs.append(values)
            return values

        fake_config.AgentCoreMemoryConfig = configure
        fake_sessions.AgentCoreMemorySessionManager = lambda **values: values
        scope = {"tenantId": "tenant-a", "clientId": "client-a", "projectId": "project-a", "sessionId": "browser-a"}
        original = dict(scope)
        with (
            patch.dict(sys.modules, {fake_config.__name__: fake_config, fake_sessions.__name__: fake_sessions}),
            patch.dict(memory.os.environ, {"AGENTCORE_MEMORY_ID": "memory-test"}),
            patch.object(memory, "uuid4", side_effect=[types.SimpleNamespace(hex=str(i)) for i in range(3)]),
        ):
            memory.memory_session(scope)
            memory.memory_session(scope)
            memory.memory_session({**scope, "tenantId": "tenant-b"})

        self.assertEqual(configs[0]["actor_id"], configs[1]["actor_id"])
        self.assertNotEqual(configs[0]["session_id"], configs[1]["session_id"])
        self.assertNotEqual(configs[0]["actor_id"], configs[2]["actor_id"])
        self.assertNotEqual(configs[0]["session_id"], configs[2]["session_id"])
        self.assertEqual(scope, original)

    def test_memory_can_remain_disabled_without_importing_the_sdk(self):
        with patch.dict(memory.os.environ, {"AGENTCORE_MEMORY_ID": ""}):
            with memory.memory_session({}) as session:
                self.assertIsNone(session)
