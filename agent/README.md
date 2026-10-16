# fast-channels-agent

The FAST Channels scheduling agent, an Amazon Bedrock AgentCore Runtime application (package
`fast_channels.agent`). It is a Strands agent that acts only through the AgentCore Gateway tools and talks to the
console over the AG-UI protocol. It does not depend on `fast_channels.core`; replace it with any agent that speaks
the same protocol to the same Gateway.

## How a run works

1. The Runtime (ProtocolType `AGUI`, Cognito JWT authorizer) checks the bearer token and forwards the
   allow-listed `Authorization` header with the AG-UI `RunAgentInput` to `POST /invocations`.
2. `identity.Caller` reads the token's claims: `actor_id` is `sub`, or `client_id` for machine tokens. Identity is
   never taken from the payload.
3. `gateway.gateway_tools` opens one MCP connection to `GATEWAY_URL` with the caller's token. Cedar authorizes each
   call as that operator. Tool definitions are cached per principal for 5 minutes. An empty `tools/list` is
   never cached: it is asked again after 1 s, and a second empty answer ends the run with `GATEWAY_UNAVAILABLE`
   (right after a deployment the Gateway can list nothing for a few minutes). Only `<target>___<tool>`
   tools are exposed to the model, and `correlation_id` is hidden from their schemas.
4. `scheduler.Scheduler` rebuilds the Strands agent from the AgentCore Memory session: history, active skills
   and pending approvals. The process holds no conversation state. The agent gets the system prompt
   (`prompts/system.md`), the domain skills (`skills/*/SKILL.md`, through the Strands `AgentSkills` plugin) and
   the hooks:
   - `ToolGuard` (BeforeToolCall) sets `correlation_id` to the session id on every Gateway call. It also raises an
     approval interrupt for each tool in `CONFIRM_TOOLS`, and cancels a declined call with a `DECLINED` error
     envelope.
   - `SharedStateTracker` (AfterToolCall) turns `schedule___plan`, `schedule___get_schedule`, `schedule___publish`
     and `schedule___remove_programs` results into the AG-UI shared state. A publish or removal result holds only
     the programs it changed, so it is folded into the guide already shown instead of replacing it.
5. `events.EventTranslator` streams standard AG-UI events back to the console.

## AG-UI contract (CONTRACTS.md §G)

| Direction | Shape |
|---|---|
| Request | `RunAgentInput`. Only the newest user message is read, plus `context` and `state`. `threadId` should equal the runtime session id (at least 33 characters). |
| Resume | `forwardedProps.approval = {"interrupt_id", "approved", "note"?}`, or standard AG-UI `resume: [{"interruptId", "status": "resolved" or "cancelled", "payload": {"approved", "note"}}]`. A plain new message while an approval is pending declines it, and the message becomes the reason the model reads. |
| Mode | Optional `forwardedProps.mode`: `"chat"` or `"plan"`. It selects the ModelSpec effort. Machine callers (Autopilot) default to `plan`. |
| Events | `RUN_STARTED` → `TEXT_MESSAGE_START/CONTENT/END`, `TOOL_CALL_START/ARGS/END`, `TOOL_CALL_RESULT`, then `STATE_SNAPSHOT` (`{"channel", "plan", "schedule", "schedule_scope"}`) after schedule tools. A paused run sends `CUSTOM approval_required` (`{"interrupt_id", "tool", "arguments", "reason", "tool_call_id"}`). The run ends with `RUN_FINISHED` (`outcome.type = "interrupt"` when paused; `usage` holds token counts) or with `RUN_ERROR {code, message}`. |
| Tool results | AG-UI `TOOL_CALL_RESULT` has no status field, so a failed call always carries the tools' error envelope `{"ok": false, "error": {"code", "message", "retryable", "hint"}}`. Tools return it themselves. A declined approval is code `DECLINED`. Failures that never reached a tool, such as a Cedar denial or a Gateway, MCP or Strands error, are wrapped as `TOOL_ERROR`. |
| Shared state | `schedule` is EPG-shaped (`channel`, `generated_at`, `ladder_version`, `programs`). `schedule_scope` is `"channel"` when it is the channel's guide from now on (a `get_schedule` result, with later publishes and removals folded in), or `"published"` when the conversation published without reading the guide first, so it holds only the programs published here. |
| Error codes | `UNAUTHENTICATED`, `BAD_REQUEST`, `APPROVAL_NOT_FOUND`, `GATEWAY_UNAVAILABLE`, `THROTTLED`, `INTERNAL_ERROR`. Runtime platform errors (invalid token: 401/403) also arrive as AG-UI `RUN_ERROR` events (`ACCESS_DENIED`, `UNAUTHORIZED`) on a `text/event-stream` response. |

## Environment (set by infra)

| Variable | Meaning |
|---|---|
| `AWS_REGION` | Set by the Runtime. Used for Bedrock, SSM and Memory. |
| `GATEWAY_URL` | AgentCore Gateway MCP endpoint (`https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`). |
| `MEMORY_ID` | AgentCore Memory id. Without it, sessions are kept on local disk (development only). |
| `MEMORY_PREFERENCE_NAMESPACE` | Optional. USER_PREFERENCE namespace template, for example `/users/{actorId}/preferences/`. When set, recalled preferences are added to the prompt. |
| `CONFIRM_TOOLS` | Comma-separated qualified tool names that need operator approval (`registry.confirm_tools()`). |
| `MODEL_SPEC_PARAMETER` | SSM String parameter holding the ModelSpec JSON `{"model_id", "effort": {"chat", "plan"}, "max_tokens"}`. Read with a 60 s cache. |
| `DEFAULT_MODEL_ID` | Code default model. Use the region's geo profile, for example `us.anthropic.claude-haiku-5-5`. |
| `ALLOWED_MODELS` | Comma-separated model ids. A spec naming any other model is ignored. The runtime role must be able to invoke each of them. |
| `CAPTURE_PROMPTS` | Optional debug switch, off by default. `true` keeps prompts, tool results and model replies in the runtime's logs and traces (see Logging below). |

Runtime role: `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the allowed profiles,
`ssm:GetParameter` on the spec parameter, and Memory read and write. The Gateway needs no IAM grant, because the
operator's JWT is forwarded.

## Code zip (PYTHON_3_14, arm64)

This is the build that was verified in the live spike: it took 0.3 s with a warm uv cache, produced 100 MB
unzipped and 40 MB zipped, and was deployed with CDK `AgentRuntimeArtifact.from_code_asset`.

```bash
out=build/agent && mkdir -p "$out"
uv export --package fast-channels-agent --frozen --no-dev --no-editable --no-hashes --output-file "$out.txt"
uv pip install --no-sources --no-installer-metadata --no-compile-bytecode \
  --python-platform aarch64-manylinux2014 --python 3.14 --target "$out" --requirement "$out.txt"
cp agent/main.py "$out/main.py"        # zip-root entry point
```

By default uv reuses its cached wheel of a local package until that package's `pyproject.toml` changes, so a
source-only edit would ship a stale zip (this was observed). This package declares `tool.uv.cache-keys` on
`src/**/*` to prevent that. Passing `--refresh-package fast-channels-agent` also works.

```python
agentcore.AgentRuntimeArtifact.from_code_asset(
    path="build/agent",
    runtime=agentcore.AgentCoreRuntime.PYTHON_3_14,
    entrypoint=["opentelemetry-instrument", "main.py"],
)
# protocol_configuration=ProtocolType.AGUI, request_header_configuration allow-lists "Authorization",
# lifecycle idle 10 min / max 2 h
```

## Logging and traces

Logging uses standard `logging`. Under `opentelemetry-instrument`, ADOT exports every record, including `extra=`
fields such as `correlation_id`, `actor`, `mode` and `model_id`, to the runtime log group's `otel-rt-logs` stream.
It also prints trace-correlated lines to stdout. The agent's own records carry only this metadata.

ADOT also turns on GenAI content capture by default. The botocore Bedrock instrumentation would then log the
system prompt, every operator message, tool result and model reply to `otel-rt-logs`, and Strands would put the
same content on its spans. Anyone who can read the log group would read every operator's conversation, although
Memory keeps conversations apart per actor. So at startup `telemetry.configure_content_capture` sets
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` and adds the Strands token
`gen_ai_unredacted_attributes=` to `OTEL_SEMCONV_STABILITY_OPT_IN`. What remains:

- The Bedrock log events keep tool call ids and tool names.
- The spans keep their structure, tool names, token counts, `session.id` (the correlation id) and `actor.id`, with
  content shown as `[REDACTED]`.

`scripts/trace.py` still joins a run's tool calls by correlation id. To debug a conversation, or for evaluations
that need the content, set `CAPTURE_PROMPTS=true` on the Runtime and redeploy. Then turn it off again: the log
group keeps what was captured for its whole retention.

## Model conformance

```bash
uv run scripts/eval_model.py --model-id us.anthropic.claude-haiku-5-5 --runs 3
```

This runs the standard (3 h) and hard (6 h, genre alternation, prime-time title) scheduling scenarios through
the real agent. The tools are a local fake MCP server by default, or a live Gateway with `--gateway-url`
and `--token-file`. The fake server advertises the real Gateway definitions of its tools
(`evals/gateway_tools.json`, generated from the tool registry by `functions/tests/test_agent_contract.py`) and
answers in the registry's output shapes, so the model reads what it reads in production. A model should pass every
run before it enters `ALLOWED_MODELS`.

Measured on 2026-10-16 with 3 runs per scenario:

| Model | Passed | Cost per run |
|---|---|---|
| Haiku 5.5 (`us.` profile) | 6/6, twice | about $0.005 |
| Sonnet 5.5 | 6/6 | about $0.05 |
| Nova 2 Lite | 0/6 | n/a |

Sonnet 5.5 and Nova 2 Lite were measured before the fake server advertised the real Gateway definitions. With the
real definitions, Haiku once answered "Shall I publish it?" in text instead of calling `schedule___publish`; the
approval note in every CONFIRM tool description and the system prompt now say that the call itself asks the
operator.
