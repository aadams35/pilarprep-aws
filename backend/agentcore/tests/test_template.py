from __future__ import annotations

import unittest
from pathlib import Path


TEMPLATE = (Path(__file__).resolve().parents[3] / "infrastructure" / "agentcore.yaml").read_text(
    encoding="utf-8"
)


class TemplateSecurityTests(unittest.TestCase):
    def test_router_and_worker_can_use_the_configured_data_key(self):
        router_role = TEMPLATE.split("  AgentRouterRole:", 1)[1].split(
            "  AgentWorkerRole:", 1
        )[0]
        worker_role = TEMPLATE.split("  AgentWorkerRole:", 1)[1].split(
            "  AgentRouterFunction:", 1
        )[0]

        for role in (router_role, worker_role):
            self.assertIn("- HasDataKmsKey", role)
            self.assertIn("- kms:Decrypt", role)
            self.assertIn("- kms:Encrypt", role)
            self.assertIn("- kms:GenerateDataKey", role)
            self.assertIn("- kms:DescribeKey", role)
            self.assertIn("Resource: !Ref DataKmsKeyArn", role)


    def test_tools_can_read_only_supported_scoped_brief_pointer_shapes(self):
        tools_role = TEMPLATE.split("  AgentToolsRole:", 1)[1].split(
            "  AgentToolsFunction:", 1
        )[0]

        self.assertIn(
            "/tenants/*/clients/*/projects/*/brief/approved/v*/packet.json",
            tools_role,
        )
        self.assertIn(
            "/tenants/*/clients/*/projects/*/brief/latest.json",
            tools_role,
        )

    def test_agent_worker_allows_long_running_agentcore_generation(self):
        worker = TEMPLATE.split("  AgentWorkerFunction:", 1)[1].split(
            "  AgentToolsLogGroup:", 1
        )[0]

        self.assertIn("Timeout: 600", worker)
        self.assertIn('AGENT_RUNTIME_READ_TIMEOUT_SECONDS: "540"', worker)

if __name__ == "__main__":
    unittest.main()
