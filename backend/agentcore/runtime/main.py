from __future__ import annotations

from bedrock_agentcore import BedrockAgentCoreApp

from runtime.service import AgentContextLimitError, handle_request


app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context):
    try:
        return handle_request(payload)
    except AgentContextLimitError as error:
        return {
            "errorCode": "AGENT_CONTEXT_TOO_LARGE",
            "retryable": False,
            "error": str(error),
        }


if __name__ == "__main__":
    app.run()
