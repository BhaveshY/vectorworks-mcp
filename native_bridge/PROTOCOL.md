# Native Bridge Protocol

The native bridge should speak the same local TCP protocol as `vw_listener.py`
so `server.py` and Claude Code do not need a new tool surface.

## Transport

- Address: `127.0.0.1:9877` by default.
- One TCP connection may carry multiple requests.
- Each frame is a 4-byte big-endian unsigned length followed by UTF-8 JSON.
- The default maximum frame size is 16 MiB, matching `VW_MCP_MAX_FRAME_BYTES`.

## Request

```json
{
  "id": "client-generated-id",
  "action": "get_layers",
  "params": {}
}
```

Rules:

- `id` is echoed in the response.
- `action` matches the existing handler names in `vw_listener.py`.
- `params` is an object. Missing params are treated as `{}`.

## Response

Success:

```json
{
  "id": "client-generated-id",
  "success": true,
  "result": {
    "layers": []
  }
}
```

Failure:

```json
{
  "id": "client-generated-id",
  "success": false,
  "error": "human-readable error"
}
```

The smoke harness treats the response envelope strictly:

- `success` must be boolean `true` or boolean `false`; string or numeric
  lookalikes fail.
- A successful response must include `result`.
- A failed response must include a non-empty string `error`.

## Threading Contract

The socket worker may decode frames and enqueue requests. It must not call
Vectorworks document APIs directly.

CAD requests must be executed on the Vectorworks SDK main/plugin event context.
The worker thread should wait on a response future or completion event, then
write the response frame back to the socket.

`ping` may be answered directly by the transport layer because it does not touch
the active document.

## Handler Parity

The native bridge should initially implement these handlers first:

- `ping`
- `stop`
- `get_document_info`
- `get_layers`
- `get_objects`
- `selection`
- `create_object`
- `batch_create_objects`

After those are stable, port the remaining handlers from `vw_listener.py` in
small groups with smoke tests.

## Phase-0 Smoke Schema

Phase 0 proves that a native SDK transport shell is loaded and can shut down
cleanly. It does not prove CAD handler safety.

- `ping`: object with `pong: true`, non-empty string `version`,
  non-empty string `bridge_kind`, non-empty string `dispatch_mode`, non-boolean
  integer `handlers` greater than or equal to the phase-0 handler count, and
  boolean `cad_api_safe` / `transport_only` fields that agree with each other.
  A scaffold bridge may report `cad_api_safe: false` and `transport_only: true`
  while `bridge_kind` still begins with `native_sdk_bridge` and
  `dispatch_mode` is `native_sdk`. It should also report `native_phase: 0` and
  `implemented_actions` containing `ping` and `stop`.
- `stop`: when requested with `--stop`, the bridge must acknowledge `stop` and
  release the listening port.

## Phase-1 Smoke Schemas

The smoke harness validates more than transport success. Phase-1 native bridge
responses must satisfy these minimum shapes:

- `ping`: object with `pong: true`, non-empty string `version`,
  non-empty string `bridge_kind`, non-empty string `dispatch_mode`, positive
  non-boolean integer `handlers` greater than or equal to the phase-1 handler
  count, `cad_api_safe: true`, `transport_only: false`, and `native_bridge:
  true` unless the harness is explicitly run with `--allow-non-native`. Native
  mode must report `dispatch_mode: "native_sdk"` and a `bridge_kind` beginning
  with `native_sdk_bridge`; known Python diagnostic modes are rejected. It must
  also report `native_phase >= 1` and `implemented_actions` containing `ping`,
  `stop`, `get_document_info`, `get_layers`, `get_objects`, `selection`,
  `create_object`, and `batch_create_objects`. Windows SDK builds must also report
  `main_context_pump: "win32_ui_timer"` and
  `main_context_pump_ready: true`; otherwise CAD requests are not considered
  safe even when the handler list is complete.
- `get_document_info`: object with non-empty string `filename`, string
  `filepath` when present, `layers` as a list of strings, non-negative integer
  `layer_count` matching `layers.length`, and non-negative integer
  `total_objects`.
- `get_layers`: list of objects. Each layer must have a non-empty string
  `name`; `visible`, when present, must be boolean.
- `get_objects`: list of objects. Each object must have a non-empty string
  `handle` and `type`; optional `type_id` must be a non-negative integer;
  optional `name` must be a string; optional `bounds` must be `null` or contain
  `top_left` and `bottom_right` as two-number lists.
- `selection` with `action=get`: list of selected-object records using the same
  object shape as `get_objects`. An empty list is valid.
- `create_object`: object with non-empty string `type` and `handle`. If the
  active Vectorworks session has no writable layer, the native bridge
  creates/selects `Vectorworks MCP Layer` before drawing.
- `batch_create_objects`: object with `atomic: true`, `created_count`, and a
  `created` list containing one `{index, type, handle}` object per requested
  primitive. The handler must create all requested primitives in one native undo
  event and roll back created objects on ordinary handler errors before
  returning failure. It uses the same no-layer bootstrap as `create_object`.

The harness also cross-checks the first successful phase-1 read snapshots:

- `get_document_info.layers` must match names from `get_layers`.
- `get_document_info.layer_count` must match the number of layer rows.
- `get_document_info.total_objects` must be greater than or equal to the number
  of objects returned by the bounded `get_objects` call.
- `get_objects` must honor requested `limit`, `object_type`, and `layer`
  filters when those params are present.
