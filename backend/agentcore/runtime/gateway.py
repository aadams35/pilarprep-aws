from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from typing import Any, Mapping


class ProjectGateway:
    """Small adapter around the IAM-authenticated AgentCore MCP Gateway."""

    def __init__(self, endpoint: str | None = None, target_name: str | None = None):
        base_endpoint = endpoint or os.environ["GATEWAY_URL"]
        normalized_endpoint = base_endpoint.rstrip("/")
        self.endpoint = normalized_endpoint if normalized_endpoint.endswith("/mcp") else f"{normalized_endpoint}/mcp"
        self.target_name = target_name or os.environ["GATEWAY_TARGET_NAME"]
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self._client: Any = None

    def __enter__(self) -> "ProjectGateway":
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
        from strands.tools.mcp import MCPClient

        factory = lambda: aws_iam_streamablehttp_client(
            endpoint=self.endpoint,
            aws_region=self.region,
            aws_service="bedrock-agentcore",
        )
        self._client = MCPClient(factory)
        self._client.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._client is not None:
            self._client.__exit__(exc_type, exc, traceback)
            self._client = None

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("ProjectGateway must be used as a context manager")
        result = self._client.call_tool_sync(
            tool_use_id=f"pillarprep-{uuid.uuid4().hex}",
            name=f"{self.target_name}___{tool_name}",
            arguments=dict(arguments),
            read_timeout_seconds=timedelta(seconds=45),
        )
        return _tool_result_json(result)


def _tool_result_json(result: Any) -> dict[str, Any]:
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        content = result["content"]
    else:
        content = getattr(result, "content", None)

    text_items: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                item_text = item.get("text")
            else:
                item_text = getattr(item, "text", None)
            if not isinstance(item_text, str) or not item_text.strip():
                continue
            text_items.append(item_text.strip())
            try:
                parsed = json.loads(item_text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    is_error = result.get("isError") if isinstance(result, dict) else getattr(result, "isError", False)
    if is_error:
        detail = " ".join(text_items) or "Gateway tool failed without an error message"
        raise RuntimeError(f"AgentCore Gateway tool error: {detail}")
    if isinstance(result, dict):
        return result
    raise RuntimeError("AgentCore Gateway tool returned an invalid JSON response")