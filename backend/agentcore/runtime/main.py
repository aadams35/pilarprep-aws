from __future__ import annotations

from bedrock_agentcore import BedrockAgentCoreApp

from runtime.service import handle_request


app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context):
    return handle_request(payload)


if __name__ == "__main__":
    app.run()
