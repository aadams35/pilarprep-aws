from __future__ import annotations

import sys
import unittest
from pathlib import Path


AGENTCORE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENTCORE_ROOT))

from common.security import (  # noqa: E402
    ScopeTokenError,
    assert_event_scope,
    sign_scope_token,
    verify_scope_token,
)


SECRET = "s" * 48
SCOPE = {
    "tenantId": "tenant-a",
    "clientId": "bluemesa-payments",
    "projectId": "migration-wave-one",
    "userId": "user-123",
    "sessionId": "session-123",
}


class ScopeTokenTests(unittest.TestCase):
    def test_round_trip_and_expiry(self):
        token = sign_scope_token(SECRET, SCOPE, ttl_seconds=60, now=1000)
        self.assertEqual(verify_scope_token(token, SECRET, now=1030), SCOPE)

        with self.assertRaisesRegex(ScopeTokenError, "expired"):
            verify_scope_token(token, SECRET, now=1100, clock_skew_seconds=0)

    def test_signature_tampering_is_rejected(self):
        token = sign_scope_token(SECRET, SCOPE, now=1000)
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        with self.assertRaises(ScopeTokenError):
            verify_scope_token(tampered, SECRET, now=1001)

    def test_cross_client_event_is_rejected(self):
        with self.assertRaisesRegex(ScopeTokenError, "clientId"):
            assert_event_scope(
                {
                    "tenantId": "tenant-a",
                    "clientId": "another-client",
                    "projectId": "migration-wave-one",
                },
                SCOPE,
            )


if __name__ == "__main__":
    unittest.main()
