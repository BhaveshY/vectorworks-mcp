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

After those are stable, port the remaining handlers from `vw_listener.py` in
small groups with smoke tests.
