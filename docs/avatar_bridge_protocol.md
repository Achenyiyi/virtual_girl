# Avatar Bridge Protocol v1

## Purpose

The avatar stage is a presentation process. The Python companion runtime remains the
authoritative owner of identity, affect, memory, dialogue, and policy. A stage may render
Live2D or VRM, but it must not independently mutate companion state.

This protocol is a stable boundary for a thin stage extension. It is not AIRI's Eventa
protocol and does not claim compatibility with an unfinished AIRI remote-plugin API.

## Transport and security

- WebSocket text frames containing UTF-8 JSON.
- Default local endpoint: `ws://127.0.0.1:6121/ws`.
- Use `wss://` for every non-loopback connection.
- URLs containing embedded credentials, query strings, or fragments are rejected so secrets
  cannot leak through endpoint logs.
- The configured maximum applies to both incoming and outgoing messages (default 1 MiB).
- The bearer token is read from `COMPANION_AVATAR_TOKEN` or another configured environment
  variable. It is sent only in the handshake and must never appear in logs. A missing or empty
  token fails closed before connecting.
- The stage must reject commands until a successful handshake.

Enable the bridge explicitly in YAML:

```yaml
providers:
  avatar:
    enabled: true
    type: websocket_bridge
    url: ws://127.0.0.1:6121/ws
    auth_token_env: COMPANION_AVATAR_TOKEN
    connect_timeout_seconds: 3.0
    request_timeout_seconds: 3.0
    max_message_bytes: 1048576
```

When enabled, an unreachable or unhealthy stage, a failed model validation/load, or an
initial state-sync failure prevents companion startup. Headless mode remains available by
leaving `enabled: false`.

## Envelope

Every client request has this shape:

```json
{
  "protocol": "companion-avatar",
  "version": 1,
  "type": "request",
  "id": "req-1",
  "method": "health",
  "params": {}
}
```

A successful response echoes the request ID:

```json
{
  "protocol": "companion-avatar",
  "version": 1,
  "type": "response",
  "id": "req-1",
  "ok": true,
  "result": {"status": "healthy"}
}
```

An error response sets `ok` to `false` and contains a non-empty `error` string. Requests can
be in flight concurrently, so a stage must correlate responses by ID and must not rely on
response order.

## Required methods

| Method | Request parameters | Successful result |
| --- | --- | --- |
| `handshake` | `supported_versions`, `client`, `auth_token` | `version` negotiated as `1` |
| `health` | none | `status`, currently `healthy` to pass readiness |
| `model.list` | none | `models` array |
| `model.validate` | `model_id` | `errors` string array |
| `model.load` | `model_id` | `loaded` boolean |
| `state.update` | complete `state` object | empty object |
| `expression.trigger` | `expression_id`, `intensity`, `duration_ms` | empty object |
| `gesture.trigger` | `gesture_id`, `intensity` | empty object |
| `proactive.set_level` | `level` from 0 through 4 | empty object |

### Optional release-gate inspection extension

A stage intended for production acceptance must additionally implement the read-only
`stage.inspect` method. Normal runtime operation does not require it, so existing v1 presentation
stages remain compatible; `--accept-avatar` fails until the extension is present.

`stage.inspect` takes no parameters and returns:

| Field | Type | Required meaning |
| --- | --- | --- |
| `renderer` | non-empty string | `live2d` or `vrm`, sourced from the active renderer |
| `model_id` | non-empty string | model currently bound to the renderer |
| `model_loaded` | boolean | renderer model resources completed loading |
| `visible` | boolean | model is attached to a visible stage/view |
| `state_sequence` | non-negative integer | most recent accepted full-state sequence |
| `rendered_state_sequence` | non-negative integer | state sequence consumed by the render loop |
| `frame_sequence` | non-negative integer | monotonically increasing presented-frame counter |
| `expression_sequence` | non-negative integer | most recent expression command applied |
| `gesture_sequence` | non-negative integer | most recent gesture command applied |
| `proactive_sequence` | non-negative integer | most recent proactive-level command applied |
| `expression_id` | non-empty string | expression currently applied by the renderer |
| `valence`, `arousal` | number | affect values currently applied by the renderer |
| `proactive_level` | integer 0-4 | active presentation behavior level |
| `last_gesture_id` | string | most recently accepted one-shot gesture, empty if none |

The inspection values must come from renderer-owned applied state, not merely the WebSocket
request handler. In particular, `rendered_state_sequence` may advance only after the render loop
consumes the matching `state.update`, and `frame_sequence` may advance only after a frame is
presented. Expression, gesture, and proactive sequences may advance only after their corresponding
command is applied to renderer-owned state. The method must not return model paths, user text,
credentials, or image/audio data.

The complete state contains `expression`, `pose`, `eyes`, `valence`, `arousal`, `energy`,
`is_speaking`, and `audio_level`. Nested fields use the names defined by
`companion.providers.avatar.AvatarState`. A stage should treat every state update as a full
snapshot rather than a partial patch.

## Failure and lifecycle behavior

- Connection and request timeouts are bounded.
- A protocol violation or disconnect fails all pending requests.
- A later request reconnects and performs a fresh authenticated handshake.
- Runtime dialogue continues after a post-start rendering failure, while reporting the error;
  presentation failure must not corrupt conversation or memory.
- Shutdown closes the WebSocket and reader task. A shut-down provider cannot reconnect.

## AIRI integration status

As inspected at AIRI commit `a42e3ae0b51000c552d7cd19e6c20fa10918a614`, AIRI provides a
plugin protocol, a WebSocket remote channel, and Live2D expression tools. However,
`setupRemotePluginScope()` is still empty and the expression tools are not registered as a
stable remote API. Therefore AIRI currently needs a thin extension that implements this v1
contract and translates it to the local stage APIs. Pin that extension to a tested AIRI release
until AIRI publishes a stable remote interface.

Research references:

- <https://github.com/moeru-ai/airi/blob/a42e3ae0b51000c552d7cd19e6c20fa10918a614/packages/plugin-protocol/README.md>
- <https://github.com/moeru-ai/airi/blob/a42e3ae0b51000c552d7cd19e6c20fa10918a614/packages/plugin-sdk/src/channels/remote/websocket/index.ts>
- <https://github.com/moeru-ai/airi/blob/a42e3ae0b51000c552d7cd19e6c20fa10918a614/packages/plugin-sdk/src/plugin/remote/index.ts>
- <https://github.com/moeru-ai/airi/blob/a42e3ae0b51000c552d7cd19e6c20fa10918a614/packages/stage-ui-live2d/src/tools/expression-tools.ts>
