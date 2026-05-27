# Native Source Placeholder

This folder is reserved for the Vectorworks SDK plug-in source.

Do not add fake build files that appear to compile without the Vectorworks SDK.
The next implementation step after prerequisites are installed is to create the
plug-in from the official Vectorworks 2024 SDK sample/template, then wire it to
the protocol in `..\PROTOCOL.md`.

Minimum source shape once the SDK is available:

- A Vectorworks SDK plug-in entry point.
- A local TCP server or transport object.
- A thread-safe request queue from socket worker to Vectorworks event context.
- Handler implementations that mirror `vw_listener.py`.
- A stop/unload path that releases port `9877`.
