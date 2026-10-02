#pragma once

// In-process scripting (contract 1.10): run_script executes Python (`vs`
// pre-imported) or VectorScript on the Vectorworks main thread through
// IPythonScriptEngine / IVectorScriptEngine. Never a modal listener.

#include "JsonParams.hpp"

#include <string>

namespace VectorworksMCP::Scripting {

struct ScriptOutcome {
    bool ok = false;
    std::string resultJson = "null";
    std::string stdoutText;
    std::string stderrText;
    bool stdoutTruncated = false;
    bool stderrTruncated = false;
    std::string error;
    bool rolledBack = false;
    bool rollbackVerified = false;
    bool transaction = false;
    double durationMs = 0.0;
};

bool ScriptingAllowed();

// Crash containment (PROTOCOL.md "Script engine crash containment"): every
// IPythonScriptEngine / IVectorScriptEngine call runs under an SEH guard. A
// hardware exception inside the engine (e.g. an access violation in a vs.*
// call with wrong-typed arguments) is caught, reported as `script_crashed`
// (commit_state "unknown") and poisons the scripting engine for the rest of
// the process: later run_script requests and Python-backed handlers fail fast
// with `script_engine_unavailable` without calling into Python again.
bool ScriptEnginePoisoned();
// "ok" | "poisoned" (ping `script_engine`).
const char* ScriptEngineState();
// Number of contained faults so far (0 = never crashed).
unsigned ScriptFaultCount();
// Hex exception code of the first contained fault ("" when none).
std::string ScriptFaultCodeHex();
// Closes the open undo event after a contained fault WITHOUT replaying it
// (UndoAndRemove would traverse objects the faulting call may have left
// half-modified). Itself SEH-guarded; returns false if that faulted too.
bool EndUndoEventAfterFault();

// run_script action handler.
std::string HandleRunScript(const Params& params);

// Internal helper used by other handlers (saved views, layers, classes):
// runs a short Python snippet that assigns `result`, without an undo wrapper
// (callers manage undo). Throws BridgeError on failure; returns result JSON.
std::string RunInternalPython(const std::string& code);

// Encodes text as a Python expression that evaluates to the same str
// (base64 transport, safe for any content).
std::string PythonStringLiteral(const std::string& text);

}  // namespace VectorworksMCP::Scripting
