#include "StdAfx.h"

#include "ScriptEngine.hpp"

#include "BridgeProtocol.hpp"

#include "Interfaces/VectorWorks/Scripting/IPythonScriptEngine.h"
#include "Interfaces/VectorWorks/Scripting/IVectorScriptEngine.h"

#include <malloc.h>  // _resetstkoflw

#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <system_error>

namespace VectorworksMCP::Scripting {

namespace {

namespace fs = std::filesystem;

constexpr std::size_t kMaxResultBytes = 8u * 1024u * 1024u;
constexpr short kSuppressVectorScriptErrorsVariable = 9876;  // see SDK ImageComparisonTesting.cpp
std::atomic<std::uint64_t> gScriptCounter{0};

// ---- crash containment --------------------------------------------------------
// The SEH guards below live in small functions with only POD locals: MSVC
// forbids __try in a function whose frame holds C++ objects that need
// unwinding (C2712), and /EHsc does not run destructors for frames unwound by
// an SEH handler anyway. Only hardware faults are handled; C++ exceptions
// (0xE06D7363) and everything else continue the search unchanged.
std::atomic<bool> gEnginePoisoned{false};
std::atomic<unsigned> gFaultCount{0};
std::atomic<unsigned long> gFirstFaultCode{0};

int ScriptFaultFilter(unsigned long code) {
    switch (code) {
        case EXCEPTION_ACCESS_VIOLATION:
        case EXCEPTION_ARRAY_BOUNDS_EXCEEDED:
        case EXCEPTION_DATATYPE_MISALIGNMENT:
        case EXCEPTION_FLT_DIVIDE_BY_ZERO:
        case EXCEPTION_FLT_INVALID_OPERATION:
        case EXCEPTION_ILLEGAL_INSTRUCTION:
        case EXCEPTION_IN_PAGE_ERROR:
        case EXCEPTION_INT_DIVIDE_BY_ZERO:
        case EXCEPTION_PRIV_INSTRUCTION:
        case EXCEPTION_STACK_OVERFLOW:
            return EXCEPTION_EXECUTE_HANDLER;
        default:
            return EXCEPTION_CONTINUE_SEARCH;
    }
}

// Each guard returns 0, or the exception code of a contained fault.
unsigned long GuardedPythonExecute(
    VectorWorks::Scripting::IPythonScriptEngine* engine, const TXString* script,
    VectorWorks::Scripting::IPythonLogger* logger, VCOMError* error) {
    __try {
        *error = engine->ExecuteScript(*script, logger);
        return 0;
    } __except (ScriptFaultFilter(GetExceptionCode())) {
        const unsigned long code = GetExceptionCode();
        if (code == EXCEPTION_STACK_OVERFLOW) _resetstkoflw();
        return code ? code : 1u;
    }
}

unsigned long GuardedVectorScriptCompile(
    VectorWorks::Scripting::IVectorScriptEngine* engine, const TXString* script, bool* compiled, Sint32* line,
    TXString* errorText, VCOMError* error) {
    __try {
        *error = engine->CompileScript(*script, false, *compiled, line, errorText);
        return 0;
    } __except (ScriptFaultFilter(GetExceptionCode())) {
        const unsigned long code = GetExceptionCode();
        if (code == EXCEPTION_STACK_OVERFLOW) _resetstkoflw();
        return code ? code : 1u;
    }
}

unsigned long GuardedVectorScriptExecute(
    VectorWorks::Scripting::IVectorScriptEngine* engine, const TXString* script, VCOMError* error) {
    __try {
        *error = engine->ExecuteScript(*script);
        return 0;
    } __except (ScriptFaultFilter(GetExceptionCode())) {
        const unsigned long code = GetExceptionCode();
        if (code == EXCEPTION_STACK_OVERFLOW) _resetstkoflw();
        return code ? code : 1u;
    }
}

int GuardedEndUndoEvent() {
    __try {
        return gSDK->EndUndoEvent() ? 1 : 0;
    } __except (ScriptFaultFilter(GetExceptionCode())) {
        return -1;
    }
}

std::string HexCode(unsigned long code) {
    char buffer[16] = {};
    std::snprintf(buffer, sizeof(buffer), "0x%08lX", code);
    return buffer;
}

const char* FaultName(unsigned long code) {
    switch (code) {
        case EXCEPTION_ACCESS_VIOLATION: return "access violation";
        case EXCEPTION_STACK_OVERFLOW: return "stack overflow";
        case EXCEPTION_INT_DIVIDE_BY_ZERO: return "integer divide by zero";
        case EXCEPTION_ILLEGAL_INSTRUCTION: return "illegal instruction";
        case EXCEPTION_IN_PAGE_ERROR: return "in-page error";
        default: return "hardware exception";
    }
}

constexpr const char* kRestartNextStep = "save your work and restart Vectorworks";

[[noreturn]] void ThrowCrashed(unsigned long code, const char* engineName) {
    unsigned long expected = 0;
    gFirstFaultCode.compare_exchange_strong(expected, code);
    gEnginePoisoned.store(true);
    ++gFaultCount;
    throw Protocol::BridgeError(
        "script_crashed",
        std::string("the script crashed inside Vectorworks (") + FaultName(code) + " " + HexCode(code) + " in the " +
            engineName + " engine, typically a vs.* call with wrong-typed arguments). The fault was contained, but "
            "scripting is disabled for the rest of this Vectorworks session and the document state is unknown; " +
            kRestartNextStep,
        false, "unknown",
        "{\"exception_code\":" + JsonString(HexCode(code)) + ",\"script_engine\":\"poisoned\",\"next_step\":" +
            JsonString(kRestartNextStep) + "}");
}

void ThrowIfPoisoned() {
    if (!gEnginePoisoned.load()) return;
    const std::string code = HexCode(gFirstFaultCode.load());
    throw Protocol::BridgeError(
        "script_engine_unavailable",
        "the Vectorworks scripting engine is disabled after an earlier script crash (" + code + "); " +
            kRestartNextStep,
        false, "not_started",
        "{\"exception_code\":" + JsonString(code) + ",\"script_engine\":\"poisoned\",\"next_step\":" +
            JsonString(kRestartNextStep) + "}");
}

const char* kBase64Alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string Base64(const std::string& input) {
    std::string out;
    out.reserve(((input.size() + 2u) / 3u) * 4u);
    std::size_t index = 0;
    while (index + 2u < input.size()) {
        const std::uint32_t chunk = (static_cast<unsigned char>(input[index]) << 16) |
            (static_cast<unsigned char>(input[index + 1u]) << 8) |
            static_cast<unsigned char>(input[index + 2u]);
        out.push_back(kBase64Alphabet[(chunk >> 18) & 63u]);
        out.push_back(kBase64Alphabet[(chunk >> 12) & 63u]);
        out.push_back(kBase64Alphabet[(chunk >> 6) & 63u]);
        out.push_back(kBase64Alphabet[chunk & 63u]);
        index += 3u;
    }
    const std::size_t rest = input.size() - index;
    if (rest == 1u) {
        const std::uint32_t chunk = static_cast<unsigned char>(input[index]) << 16;
        out.push_back(kBase64Alphabet[(chunk >> 18) & 63u]);
        out.push_back(kBase64Alphabet[(chunk >> 12) & 63u]);
        out += "==";
    } else if (rest == 2u) {
        const std::uint32_t chunk = (static_cast<unsigned char>(input[index]) << 16) |
            (static_cast<unsigned char>(input[index + 1u]) << 8);
        out.push_back(kBase64Alphabet[(chunk >> 18) & 63u]);
        out.push_back(kBase64Alphabet[(chunk >> 12) & 63u]);
        out.push_back(kBase64Alphabet[(chunk >> 6) & 63u]);
        out += "=";
    }
    return out;
}

std::string Narrow(const std::wstring& value) {
    if (value.empty()) return std::string();
    const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), size, nullptr, nullptr);
    return out;
}

fs::path ScriptDirectory() {
    wchar_t buffer[MAX_PATH + 2] = {};
    const DWORD length = GetTempPathW(MAX_PATH + 1, buffer);
    fs::path base = length ? fs::path(std::wstring(buffer, length)) : fs::temp_directory_path();
    fs::path dir = base / L"vectorworks-mcp" / L"scripts";
    std::error_code error;
    fs::create_directories(dir, error);
    return dir;
}

std::string SerializeJson(const JsonValue& value) {
    switch (value.type) {
        case JsonValue::Type::Null: return "null";
        case JsonValue::Type::Bool: return value.boolValue ? "true" : "false";
        case JsonValue::Type::Number: return JsonNumber(value.numberValue);
        case JsonValue::Type::String: return JsonString(value.stringValue);
        case JsonValue::Type::Array: {
            std::string json = "[";
            for (std::size_t index = 0; index < value.items.size(); ++index) {
                if (index) json += ",";
                json += SerializeJson(value.items[index]);
            }
            return json + "]";
        }
        case JsonValue::Type::Object: {
            std::string json = "{";
            for (std::size_t index = 0; index < value.members.size(); ++index) {
                if (index) json += ",";
                json += JsonString(value.members[index].first) + ":" + SerializeJson(value.members[index].second);
            }
            return json + "}";
        }
    }
    return "null";
}

const char* kHarness = R"PY(import sys, io, json, traceback, base64
import vs
_mcp_code = base64.b64decode("@CODE@").decode("utf-8")
_mcp_path = base64.b64decode("@PATH@").decode("utf-8")
_mcp_limit = 262144
_mcp_out = io.StringIO()
_mcp_err = io.StringIO()
_mcp_saved = (sys.stdout, sys.stderr)
_mcp_report = {"ok": True, "result": None}
_mcp_ns = {"vs": vs, "__name__": "__vw_mcp__", "__builtins__": __builtins__, "result": None}
sys.stdout, sys.stderr = _mcp_out, _mcp_err
try:
    exec(compile(_mcp_code, "<vw_mcp_script>", "exec"), _mcp_ns)
except BaseException as _mcp_exc:
    _mcp_report["ok"] = False
    _mcp_report["error"] = "".join(traceback.format_exception(type(_mcp_exc), _mcp_exc, _mcp_exc.__traceback__))[-65536:]
finally:
    sys.stdout, sys.stderr = _mcp_saved
_mcp_value = _mcp_ns.get("result")
try:
    _mcp_report["result"] = json.loads(json.dumps(_mcp_value, default=str))
except BaseException:
    try:
        _mcp_report["result"] = repr(_mcp_value)
    except BaseException:
        _mcp_report["result"] = None
_mcp_text = _mcp_out.getvalue()
_mcp_report["stdout"] = _mcp_text[:_mcp_limit]
_mcp_report["stdout_truncated"] = len(_mcp_text) > _mcp_limit
_mcp_text = _mcp_err.getvalue()
_mcp_report["stderr"] = _mcp_text[:_mcp_limit]
_mcp_report["stderr_truncated"] = len(_mcp_text) > _mcp_limit
try:
    with open(_mcp_path, "w", encoding="utf-8") as _mcp_file:
        json.dump(_mcp_report, _mcp_file)
except BaseException:
    pass
)PY";

std::string ReplaceAll(std::string text, const std::string& token, const std::string& value) {
    std::size_t position = 0;
    while ((position = text.find(token, position)) != std::string::npos) {
        text.replace(position, token.size(), value);
        position += value.size();
    }
    return text;
}

class ScopedVectorScriptErrorSuppression {
public:
    ScopedVectorScriptErrorSuppression() {
        Boolean suppress = true;
        active_ = gSDK && gSDK->SetProgramVariable(kSuppressVectorScriptErrorsVariable, &suppress);
    }
    ~ScopedVectorScriptErrorSuppression() {
        if (active_) {
            Boolean suppress = false;
            gSDK->SetProgramVariable(kSuppressVectorScriptErrorsVariable, &suppress);
        }
    }
    ScopedVectorScriptErrorSuppression(const ScopedVectorScriptErrorSuppression&) = delete;
    ScopedVectorScriptErrorSuppression& operator=(const ScopedVectorScriptErrorSuppression&) = delete;

private:
    bool active_ = false;
};

// Cheap structural fingerprint used to verify a rollback: layer count plus
// top-level object count per layer (no UUID reads, no deep traversal).
std::string StructureSignature() {
    std::ostringstream out;
    std::size_t layers = 0;
    gSDK->ForEachLayerN([&](MCObjectHandle layer) {
        if (!layer) return;
        ++layers;
        std::size_t count = 0;
        for (MCObjectHandle object = gSDK->FirstMemberObj(layer);
             object && gSDK->GetObjectTypeN(object) != kTermNode;
             object = gSDK->NextObject(object)) {
            ++count;
        }
        out << count << ',';
    });
    return std::to_string(layers) + ":" + out.str();
}

class PythonLogger final : public VectorWorks::Scripting::IPythonLogger {
public:
    void VCOM_CALLTYPE LogStdOut(const char* message) override {
        if (message && out_.size() < 262144u) out_ += message;
    }
    void VCOM_CALLTYPE LogStdErr(const char* message) override {
        if (message && err_.size() < 262144u) err_ += message;
    }
    std::string out_;
    std::string err_;
};

ScriptOutcome ExecutePython(const std::string& code) {
    ThrowIfPoisoned();
    ScriptOutcome outcome;
    VectorWorks::Scripting::IPythonScriptEnginePtr python(VectorWorks::Scripting::IID_PythonScriptEngine);
    if (!python) {
        throw Protocol::BridgeError("interface_unavailable", "IPythonScriptEngine is unavailable in this Vectorworks runtime");
    }
    const fs::path resultPath = ScriptDirectory() /
        (L"run-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(++gScriptCounter) + L".json");
    std::error_code ignored;
    fs::remove(resultPath, ignored);
    std::string script = kHarness;
    script = ReplaceAll(script, "@CODE@", Base64(code));
    script = ReplaceAll(script, "@PATH@", Base64(Narrow(resultPath.wstring())));

    PythonLogger logger;
    VCOMError error = kVCOMError_NoError;
    const TXString scriptText(script.c_str());
    unsigned long fault = 0;
    {
        ScopedVectorScriptErrorSuppression suppress;
        fault = GuardedPythonExecute(python.operator->(), &scriptText, &logger, &error);
    }
    if (fault) {
        // Do not touch Python again (interpreter/thread state is unknown after
        // the fault). The harness never wrote its report; drop any partial file.
        fs::remove(resultPath, ignored);
        ThrowCrashed(fault, "Python");
    }
    std::ifstream input(resultPath, std::ios::binary);
    std::string report((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    input.close();
    fs::remove(resultPath, ignored);
    if (report.empty()) {
        outcome.ok = false;
        outcome.error = "the Python engine did not produce a result (VCOM error " + std::to_string(error) + ")";
        if (!logger.err_.empty()) outcome.error += ": " + logger.err_;
        outcome.stdoutText = logger.out_;
        outcome.stderrText = logger.err_;
        return outcome;
    }
    if (report.size() > kMaxResultBytes + 2u * 262144u + 65536u) {
        outcome.ok = false;
        outcome.error = "script result exceeded the 8 MiB limit";
        return outcome;
    }
    JsonValue parsed;
    try {
        parsed = ParseJsonValue(report);
    } catch (const std::exception& parseError) {
        outcome.ok = false;
        outcome.error = std::string("could not parse the script report: ") + parseError.what();
        return outcome;
    }
    const auto boolField = [&](const char* key) {
        const JsonValue* value = parsed.Find(key);
        return value && value->IsBool() && value->boolValue;
    };
    const auto stringField = [&](const char* key) {
        const JsonValue* value = parsed.Find(key);
        return value && value->IsString() ? value->stringValue : std::string();
    };
    outcome.ok = boolField("ok");
    if (const JsonValue* result = parsed.Find("result")) {
        outcome.resultJson = SerializeJson(*result);
        if (outcome.resultJson.size() > kMaxResultBytes) {
            outcome.ok = false;
            outcome.resultJson = "null";
            outcome.error = "script result exceeded the 8 MiB limit";
        }
    }
    outcome.stdoutText = stringField("stdout") + logger.out_;
    outcome.stderrText = stringField("stderr") + logger.err_;
    outcome.stdoutTruncated = boolField("stdout_truncated");
    outcome.stderrTruncated = boolField("stderr_truncated");
    if (!outcome.ok && outcome.error.empty()) outcome.error = stringField("error");
    return outcome;
}

ScriptOutcome ExecuteVectorScript(const std::string& code) {
    ThrowIfPoisoned();
    ScriptOutcome outcome;
    VectorWorks::Scripting::IVectorScriptEnginePtr engine(VectorWorks::Scripting::IID_VectorScriptEngine);
    if (!engine) {
        throw Protocol::BridgeError("interface_unavailable", "IVectorScriptEngine is unavailable in this Vectorworks runtime");
    }
    const TXString script(code.c_str());
    bool compiled = false;
    Sint32 line = 0;
    TXString errorText;
    ScopedVectorScriptErrorSuppression suppress;
    VCOMError compileError = kVCOMError_NoError;
    if (const unsigned long fault = GuardedVectorScriptCompile(engine.operator->(), &script, &compiled, &line, &errorText, &compileError)) {
        ThrowCrashed(fault, "VectorScript");
    }
    if (VCOM_FAILED(compileError) || !compiled) {
        outcome.ok = false;
        outcome.error = "VectorScript compile error at line " + std::to_string(line) + ": " + errorText.GetStdString();
        return outcome;
    }
    VCOMError runError = kVCOMError_NoError;
    if (const unsigned long fault = GuardedVectorScriptExecute(engine.operator->(), &script, &runError)) {
        ThrowCrashed(fault, "VectorScript");
    }
    outcome.ok = VCOM_SUCCEEDED(runError);
    if (!outcome.ok) {
        outcome.error = "VectorScript execution failed (VCOM error " + std::to_string(runError) + ")";
    }
    return outcome;
}

}  // namespace

bool ScriptingAllowed() {
    return !FalsyEnvironmentFlag("VW_MCP_ALLOW_SCRIPT");
}

bool ScriptEnginePoisoned() {
    return gEnginePoisoned.load();
}

const char* ScriptEngineState() {
    return gEnginePoisoned.load() ? "poisoned" : "ok";
}

unsigned ScriptFaultCount() {
    return gFaultCount.load();
}

std::string ScriptFaultCodeHex() {
    const unsigned long code = gFirstFaultCode.load();
    return code ? HexCode(code) : std::string();
}

bool EndUndoEventAfterFault() {
    return GuardedEndUndoEvent() == 1;
}

std::string PythonStringLiteral(const std::string& text) {
    return "__import__('base64').b64decode('" + Base64(text) + "').decode('utf-8')";
}

std::string RunInternalPython(const std::string& code) {
    const ScriptOutcome outcome = ExecutePython(code);
    if (!outcome.ok) {
        throw Protocol::BridgeError(
            "script_failed",
            "internal Vectorworks script failed: " + outcome.error.substr(0, 2000),
            false,
            "unknown");
    }
    return outcome.resultJson;
}

std::string HandleRunScript(const Params& params) {
    if (!ScriptingAllowed()) {
        throw Protocol::BridgeError(
            "script_disabled", "run_script is disabled (VW_MCP_ALLOW_SCRIPT=0 in the Vectorworks process)",
            false, "not_started");
    }
    const std::string language = ToLower(GetStringParam(params, "language", "python"));
    const std::string code = GetStringParam(params, "code");
    if (code.empty()) {
        throw Protocol::BridgeError("invalid_request", "code is required");
    }
    if (code.size() > 4u * 1024u * 1024u) {
        throw Protocol::BridgeError("invalid_request", "code exceeds the 4 MiB limit");
    }
    const bool isPython = language == "python" || language == "py";
    const bool isVectorScript = language == "vectorscript" || language == "vs" || language == "pascal";
    if (!isPython && !isVectorScript) {
        throw Protocol::BridgeError("invalid_request", "language must be python or vectorscript");
    }
    const bool transaction = GetLenientBoolParam(params, "transaction", true);
    std::string undoLabel = GetStringParam(params, "undo_label", "Vectorworks MCP script");
    if (undoLabel.empty()) undoLabel = "Vectorworks MCP script";
    if (undoLabel.size() > 120u) undoLabel.resize(120u);

    // Fail fast before opening an undo event when an earlier script crashed.
    ThrowIfPoisoned();
    const auto started = std::chrono::steady_clock::now();
    std::string signatureBefore;
    if (transaction) {
        signatureBefore = StructureSignature();
        gSDK->SupportUndoAndRemove();
        gSDK->SetUndoMethod(kUndoSwapObjects);
        gSDK->NameUndoEvent(TXString(undoLabel.c_str()));
    }
    const unsigned faultsBefore = ScriptFaultCount();
    ScriptOutcome outcome;
    try {
        outcome = isPython ? ExecutePython(code) : ExecuteVectorScript(code);
    } catch (const Protocol::BridgeError&) {
        if (transaction) {
            if (ScriptFaultCount() != faultsBefore) {
                // Contained crash: close the event instead of replaying it (see
                // PROTOCOL.md); no StructureSignature/DrawScreen either.
                EndUndoEventAfterFault();
            } else {
                gSDK->UndoAndRemove();
            }
        }
        throw;
    } catch (const std::exception& error) {
        outcome.ok = false;
        outcome.error = error.what();
    } catch (...) {
        outcome.ok = false;
        outcome.error = "script engine raised an unknown exception";
    }
    outcome.transaction = transaction;
    if (transaction) {
        if (!outcome.ok) {
            gSDK->UndoAndRemove();
            outcome.rolledBack = true;
            outcome.rollbackVerified = StructureSignature() == signatureBefore;
        } else {
            gSDK->EndUndoEvent();
        }
    }
    gSDK->DrawScreen();
    outcome.durationMs = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();

    std::string json = "{\"ok\":";
    json += outcome.ok ? "true" : "false";
    json += ",\"language\":" + JsonString(isPython ? "python" : "vectorscript");
    json += ",\"result\":" + outcome.resultJson;
    json += ",\"stdout\":" + JsonString(outcome.stdoutText);
    json += ",\"stderr\":" + JsonString(outcome.stderrText);
    json += ",\"stdout_truncated\":";
    json += outcome.stdoutTruncated ? "true" : "false";
    json += ",\"stderr_truncated\":";
    json += outcome.stderrTruncated ? "true" : "false";
    if (!outcome.ok) {
        json += ",\"error\":" + JsonString(outcome.error);
    }
    json += ",\"transaction\":";
    json += transaction ? "true" : "false";
    json += ",\"rolled_back\":";
    json += outcome.rolledBack ? "true" : "false";
    if (outcome.rolledBack) {
        json += ",\"rollback_verified\":";
        json += outcome.rollbackVerified ? "true" : "false";
    }
    json += ",\"undo_label\":" + JsonString(undoLabel);
    json += ",\"duration_ms\":" + JsonNumber(outcome.durationMs);
    json += "}";
    return json;
}

}  // namespace VectorworksMCP::Scripting
