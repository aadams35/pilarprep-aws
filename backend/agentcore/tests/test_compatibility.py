from __future__ import annotations

import json
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


AGENTCORE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENTCORE_ROOT))

if "boto3" not in sys.modules:
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *args, **kwargs: None
    sys.modules["boto3"] = fake_boto3

if "botocore.config" not in sys.modules:
    fake_botocore = types.ModuleType("botocore")
    fake_botocore_config = types.ModuleType("botocore.config")

    class FakeConfig:
        def __init__(self, **kwargs):
            self.connect_timeout = kwargs.get("connect_timeout")
            self.read_timeout = kwargs.get("read_timeout")
            self.retries = kwargs.get("retries", {})

    fake_botocore_config.Config = FakeConfig
    fake_botocore.config = fake_botocore_config
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.config"] = fake_botocore_config
from common.contracts import validate_router_request  # noqa: E402
from compatibility import handler as app  # noqa: E402


REQUEST = {
    "action": "create_handoff",
    "clientId": "bluemesa-payments",
    "projectId": "bluemesa-payments",
    "sessionId": "session-bluemesa-demo-000001",
    "audienceRole": "PM",
    "focus": "Create the first two-week plan.",
    "meetingNotes": "The customer approved a bounded pilot with named owners.",
    "modelPreference": "nova-pro",
    "confirmWrite": True,
    "idempotencyKey": "handoff-test-000001",
    "approvedBrief": {"technical": ["Approved"]},
    "briefRequest": {"company": "BlueMesa Payments", "context": "Approved"},
}

IDENTITY_ID = "us-east-1:demo-identity"
USER_ID = app.stable_identifier("user", [IDENTITY_ID])
SCOPE = {
    "tenantId": "demo",
    "clientId": "bluemesa-payments",
    "projectId": "bluemesa-payments",
    "userId": USER_ID,
    "sessionId": REQUEST["sessionId"],
}


def iam_event(request=REQUEST):
    return {
        "body": json.dumps(request),
        "requestContext": {
            "authorizer": {
                "iam": {
                    "cognitoIdentity": {
                        "identityId": IDENTITY_ID,
                        "identityPoolId": "us-east-1:demo-pool",
                    }
                }
            }
        },
    }


def worker_event(request=REQUEST, scope=SCOPE):
    return {
        "jobId": "job-123",
        "request": request,
        "scope": scope,
        "runtimeSessionId": "runtime-session-12345678901234567890123456789012",
        "traceId": "trace-12345678901234567890123456789012",
    }



class RouterTests(unittest.TestCase):
    def test_agentcore_client_uses_long_read_timeout(self):
        with patch.object(app.boto3, "client") as client:
            app._client("bedrock-agentcore")

        config = client.call_args.kwargs["config"]
        self.assertEqual(config.connect_timeout, 5)
        self.assertEqual(config.read_timeout, 540)
        self.assertEqual(config.retries["max_attempts"], 0)

    def test_claude_sonnet_46_is_a_supported_model_preference(self):
        request = {**REQUEST, "modelPreference": "claude-sonnet-4.6"}
        self.assertEqual(
            validate_router_request(request)["modelPreference"],
            "claude-sonnet-4.6",
        )

    def test_solutions_architect_is_a_supported_audience_role(self):
        request = {**REQUEST, "audienceRole": "Solutions Architect"}
        self.assertEqual(
            validate_router_request(request)["audienceRole"], "Solutions Architect"
        )

    def test_iam_identity_queues_scoped_worker_job(self):
        dynamodb_calls = []
        worker_calls = []

        class FakeDynamoDB:
            def put_item(self, **kwargs):
                dynamodb_calls.append(kwargs)
                return {}

        class FakeLambda:
            def invoke(self, **kwargs):
                worker_calls.append(kwargs)
                return {"StatusCode": 202}

        def client(service_name):
            return {"dynamodb": FakeDynamoDB(), "lambda": FakeLambda()}[service_name]

        with (
            patch.object(app, "PROJECT_TABLE", "project-state"),
            patch.object(app, "AGENT_WORKER_FUNCTION", "pillarprep-agent-worker"),
            patch.object(app, "_client", side_effect=client),
            patch.object(app, "_invoke_runtime") as invoke_runtime,
        ):
            response = app.handler(iam_event(), None)

        self.assertEqual(response["statusCode"], 202)
        body = json.loads(response["body"])
        self.assertEqual(body["clientId"], "bluemesa-payments")
        self.assertEqual(body["projectId"], "bluemesa-payments")
        self.assertEqual(dynamodb_calls[0]["Item"]["ownerId"]["S"], USER_ID)
        self.assertEqual(
            dynamodb_calls[0]["Item"]["sessionId"]["S"], REQUEST["sessionId"]
        )
        self.assertTrue(
            dynamodb_calls[0]["Item"]["projectId"]["S"].startswith(
                "TENANT#demo|CLIENT#bluemesa-payments|PROJECT#"
            )
        )
        self.assertTrue(
            dynamodb_calls[0]["Item"]["sortKey"]["S"].startswith("AGENTJOB#")
        )
        self.assertGreater(int(dynamodb_calls[0]["Item"]["expiresAt"]["N"]), 0)
        dispatched = json.loads(worker_calls[0]["Payload"].decode("utf-8"))
        self.assertEqual(dispatched["scope"], SCOPE)
        self.assertEqual(dispatched["request"]["action"], "create_handoff")
        self.assertNotIn("scopeToken", dispatched)
        invoke_runtime.assert_not_called()

    def test_demo_identity_cannot_select_another_client(self):
        request = {
            **REQUEST,
            "clientId": "other-customer",
            "projectId": "other-customer",
        }
        response = app.handler(iam_event(request), None)
        self.assertEqual(response["statusCode"], 403)
        self.assertIn("not assigned", json.loads(response["body"])["error"])

    def test_browser_tenant_override_is_rejected(self):
        request = {**REQUEST, "tenantId": "another-tenant"}
        response = app.handler(iam_event(request), None)
        self.assertEqual(response["statusCode"], 403)

    def test_completed_job_poll_requires_same_user_and_session(self):
        completed = {
            "provider": "agentcore",
            "projectAnswer": "Grounded handoff",
            "metadata": {"fallbackUsed": False},
        }
        item = {
            **app._job_key(SCOPE, "job-123"),
            "ownerId": {"S": USER_ID},
            "sessionId": {"S": REQUEST["sessionId"]},
            "status": {"S": "complete"},
            "resultJson": {"S": json.dumps(completed)},
        }

        class FakeDynamoDB:
            def get_item(self, **_kwargs):
                return {"Item": item}

        poll = {
            "operation": "getAgentJob",
            "jobId": "job-123",
            "clientId": REQUEST["clientId"],
            "projectId": REQUEST["projectId"],
            "sessionId": REQUEST["sessionId"],
        }
        with (
            patch.object(app, "PROJECT_TABLE", "project-state"),
            patch.object(app, "_client", return_value=FakeDynamoDB()),
        ):
            response = app.handler(iam_event(poll), None)
            wrong_session = app.handler(
                iam_event({**poll, "sessionId": "session-other-000001"}), None
            )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"]), completed)
        self.assertEqual(wrong_session["statusCode"], 404)

    def test_poll_cannot_cross_client_boundary(self):
        poll = {
            "operation": "getAgentJob",
            "jobId": "job-123",
            "clientId": "apex-mutual",
            "projectId": "apex-mutual",
            "sessionId": REQUEST["sessionId"],
        }
        response = app.handler(iam_event(poll), None)
        self.assertEqual(response["statusCode"], 403)

    def test_worker_completes_runtime_result_in_job_store(self):
        updates = []
        generated = {
            "provider": "agentcore",
            "projectAnswer": "Grounded handoff",
            "metadata": {"memoryUsed": True, "gatewayUsed": True},
        }

        def update(scope, job_id, status, **kwargs):
            updates.append((scope, job_id, status, kwargs))

        with (
            patch.object(app, "_update_agent_job", side_effect=update),
            patch.object(app, "_run_agent_request", return_value=generated),
        ):
            response = app.worker_handler(worker_event(), None)

        self.assertEqual(response, {"jobId": "job-123", "status": "complete"})
        self.assertEqual([entry[2] for entry in updates], ["running", "complete"])
        self.assertEqual(updates[1][3]["result"], generated)

    def test_runtime_failure_uses_existing_lambda_fallback(self):
        fallback = {
            "provider": "bedrock",
            "metadata": {"fallbackUsed": True},
            "projectAnswer": "Fallback",
        }
        with (
            patch.object(app, "_scope_secret", return_value="s" * 48),
            patch.object(app, "_invoke_runtime", side_effect=RuntimeError("down")),
            patch.object(app, "_invoke_fallback", return_value=fallback) as invoke_fallback,
        ):
            result = app._run_agent_request(
                REQUEST,
                SCOPE,
                worker_event()["runtimeSessionId"],
                worker_event()["traceId"],
            )

        self.assertTrue(result["metadata"]["fallbackUsed"])
        invoke_fallback.assert_called_once_with(REQUEST, "RuntimeError")

    def test_catchup_runtime_failure_does_not_invoke_write_capable_fallback(self):
        request = {
            **REQUEST,
            "action": "generate_catchup",
            "confirmWrite": False,
        }
        with (
            patch.object(app, "_scope_secret", return_value="s" * 48),
            patch.object(app, "_invoke_runtime", side_effect=RuntimeError("down")),
            patch.object(app, "_invoke_fallback") as invoke_fallback,
        ):
            with self.assertRaisesRegex(RuntimeError, "remain unchanged"):
                app._run_agent_request(
                    request,
                    SCOPE,
                    worker_event()["runtimeSessionId"],
                    worker_event()["traceId"],
                )
        invoke_fallback.assert_not_called()

    def test_scope_secret_failure_uses_existing_lambda_fallback(self):
        fallback = {
            "provider": "bedrock",
            "metadata": {"fallbackUsed": True},
            "projectAnswer": "Existing Lambda completed the handoff.",
        }
        with (
            patch.object(app, "_scope_secret", side_effect=RuntimeError("secret unavailable")),
            patch.object(app, "_invoke_fallback", return_value=fallback) as invoke_fallback,
        ):
            result = app._run_agent_request(
                REQUEST,
                SCOPE,
                worker_event()["runtimeSessionId"],
                worker_event()["traceId"],
            )

        self.assertTrue(result["metadata"]["fallbackUsed"])
        invoke_fallback.assert_called_once_with(REQUEST, "RuntimeError")

    def test_worker_failure_records_failed_status(self):
        updates = []

        def update(_scope, _job_id, status, **kwargs):
            updates.append((status, kwargs))

        with (
            patch.object(app, "_update_agent_job", side_effect=update),
            patch.object(app, "_run_agent_request", side_effect=RuntimeError("unavailable")),
        ):
            response = app.worker_handler(worker_event(), None)

        self.assertEqual(response["status"], "failed")
        self.assertEqual([entry[0] for entry in updates], ["running", "failed"])
        self.assertEqual(updates[1][1]["error"], "RuntimeError")

    def test_worker_rejects_request_scope_mismatch(self):
        mismatched = worker_event(
            scope={**SCOPE, "projectId": "another-project"}
        )
        with self.assertRaises(app.AuthorizationError):
            app.worker_handler(mismatched, None)

    def test_fallback_lambda_contract(self):
        lambda_payload = {
            "statusCode": 200,
            "body": json.dumps(
                {"provider": "bedrock", "metadata": {}, "projectAnswer": "ok"}
            ),
        }

        class FakeLambda:
            def invoke(self, **_kwargs):
                return {"Payload": BytesIO(json.dumps(lambda_payload).encode("utf-8"))}

        with (
            patch.object(
                app,
                "FALLBACK_FUNCTION_ARN",
                "arn:aws:lambda:us-east-1:123:function:brief",
            ),
            patch.object(app, "_client", return_value=FakeLambda()),
        ):
            result = app._invoke_fallback(REQUEST, "RuntimeError")
        self.assertEqual(result["metadata"]["agentMode"], "lambda-fallback")
        self.assertTrue(result["metadata"]["fallbackUsed"])


if __name__ == "__main__":
    unittest.main()
