from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


AGENTCORE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENTCORE_ROOT))

from runtime.gateway import ProjectGateway  # noqa: E402


class GatewayTests(unittest.TestCase):
    def test_appends_mcp_route_to_cloudformation_gateway_url(self):
        with patch.dict(os.environ, {"GATEWAY_URL": "https://example.gateway.amazonaws.com"}):
            self.assertEqual(ProjectGateway(target_name="project-tools").endpoint, "https://example.gateway.amazonaws.com/mcp")

    def test_does_not_duplicate_existing_mcp_route(self):
        gateway = ProjectGateway(endpoint="https://example.gateway.amazonaws.com/mcp/", target_name="project-tools")
        self.assertEqual(gateway.endpoint, "https://example.gateway.amazonaws.com/mcp")


if __name__ == "__main__":
    unittest.main()