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
