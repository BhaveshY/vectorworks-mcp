# Mock Native Bridge

`mock_bridge.py` is a no-SDK contract harness. It speaks the same
length-prefixed JSON protocol as the planned native SDK bridge and returns a
native-style `ping` payload.

This mock does not use Vectorworks APIs. Its purpose is to prove that `server.py`
and agent preflight logic will work unchanged when the real native bridge
replaces the Python dialog listener.
