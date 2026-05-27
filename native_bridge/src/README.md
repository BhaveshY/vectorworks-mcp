# Native Source Placeholder

This folder is reserved for the Vectorworks SDK plug-in source.

Do not add fake build files that appear to compile without the Vectorworks SDK.
After prerequisites are installed, run `..\..\scripts\prepare-native-bridge-source.ps1`
to create an ignored SDK-backed worktree from the official Vectorworks example,
then run `..\..\scripts\build-native-bridge.ps1` to prove the unmodified example
builds before wiring it to the protocol in `..\PROTOCOL.md`.

Minimum source shape once the SDK is available:

- A Vectorworks SDK plug-in entry point.
- A local TCP server or transport object.
- A thread-safe request queue from socket worker to Vectorworks event context.
- Handler implementations that mirror `vw_listener.py`.
- A stop/unload path that releases port `9877`.
