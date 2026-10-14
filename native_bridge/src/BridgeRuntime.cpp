#include "StdAfx.h"

// Native bridge runtime (wire protocol v2, capability_revision 6):
//   * module lifecycle (kPluginModuleInit / kPluginModuleDeinit),
//   * message-only main-thread pump window (WM_TIMER + posted wake-ups),
//   * transport start with port range + exponential back-off + discovery,
//   * socket-thread actions (ping, capabilities, stop, job_status, cancel_job),
//   * main-thread dispatch behind the UI gate with structured errors.

#include "BridgeProtocol.hpp"
#include "CapabilityRegistry.hpp"
#include "DialogWatch.hpp"
#include "Discovery.hpp"
#include "DocumentRegistry.hpp"
#include "DocumentationHandlers.hpp"
#include "JsonParams.hpp"
#include "LegacyHandlers.hpp"
#include "NativeIOHandlers.hpp"
#include "NativeTransport.hpp"
#include "OrganizationHandlers.hpp"
#include "RequestScheduler.hpp"
#include "ScriptEngine.hpp"
#include "UiGate.hpp"
#include "ViewCapture.hpp"
#include "ViewDocumentHandlers.hpp"

#if defined(__has_include)
#if __has_include("BuildInfo.h")
#include "BuildInfo.h"
#endif
#endif

#ifndef VWMCP_BUILD_ID
#define VWMCP_BUILD_ID "unknown-local-build"
#endif
#ifndef VWMCP_GIT_HASH
#define VWMCP_GIT_HASH "unknown"
#endif
#ifndef VWMCP_BUILD_TIME_UTC
#define VWMCP_BUILD_TIME_UTC "unknown"
#endif

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <exception>
#include <mutex>
#include <string>

namespace VectorworksMCP {

void OnPluginLoadStartTransport();
void OnPluginModuleDeinit();
void OnVectorworksMainPluginEvent();

namespace {

constexpr const char* kBridgeVersion = "1.0.0";

std::int64_t SteadyNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// Intentionally leaked: no destructor may run at process exit while other
// threads (already killed by the OS) could hold their mutexes.
RequestScheduler& Scheduler() {
    static RequestScheduler* scheduler = new RequestScheduler();
    return *scheduler;
}

NativeTransport& Transport() {
    static NativeTransport* transport = new NativeTransport();
    return *transport;
}

std::atomic_bool gInitialized{false};
std::atomic_bool gShutdown{false};
std::atomic_bool gPumpActive{false};
std::atomic<std::int64_t> gLastTickMs{0};
std::atomic<std::uint16_t> gBoundPort{0};
std::mutex gStartMutex;
std::int64_t gNextStartAttemptMs = 0;
int gBackoffSeconds = 2;
std::string gLastStartError;
std::string gStartedAt;

const char* VectorworksVersionName() {
#if defined(SDK_VERSION)
    static const std::string name = std::to_string(SDK_VERSION / 100 + 1995);
    return name.c_str();
#else
    return "unknown";
#endif
}

// ---- auth (L1: cached by mtime, constant-time compare) -------------------

std::string DefaultAuthTokenPath() {
    if (const char* configured = std::getenv("VW_MCP_AUTH_TOKEN_FILE")) {
        if (configured[0] != '\0') return configured;
    }
    const std::string root = Discovery::StateDirectory();
    return root.empty() ? root : root + "\\auth-token";
}

std::mutex gTokenMutex;
std::string gTokenPath;
std::int64_t gTokenStamp = -1;
std::string gToken;

std::wstring WidenUtf8(const std::string& value) {
    if (value.empty()) return std::wstring();
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0);
    std::wstring out(static_cast<std::size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), size);
    return out;
}

std::string RequiredAuthToken() {
    if (const char* token = std::getenv("VW_MCP_AUTH_TOKEN")) {
        if (token[0] != '\0') return token;
    }
    const std::string path = DefaultAuthTokenPath();
    if (path.empty()) return std::string();
    const std::wstring widePath = WidenUtf8(path);
    WIN32_FILE_ATTRIBUTE_DATA data{};
    std::int64_t stamp = -2;
    if (GetFileAttributesExW(widePath.c_str(), GetFileExInfoStandard, &data)) {
        stamp = (static_cast<std::int64_t>(data.ftLastWriteTime.dwHighDateTime) << 32) |
            data.ftLastWriteTime.dwLowDateTime;
        stamp ^= static_cast<std::int64_t>(data.nFileSizeLow) << 1;
    }
    std::lock_guard<std::mutex> lock(gTokenMutex);
    if (path == gTokenPath && stamp == gTokenStamp) return gToken;
    std::string token;
    if (stamp != -2) {
        HANDLE file = CreateFileW(widePath.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                  nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file != INVALID_HANDLE_VALUE) {
            char buffer[4096] = {};
            DWORD read = 0;
            if (ReadFile(file, buffer, sizeof(buffer) - 1, &read, nullptr)) {
                token.assign(buffer, read);
            }
            CloseHandle(file);
        }
        const auto newline = token.find_first_of("\r\n");
        if (newline != std::string::npos) token.resize(newline);
        token = TrimCopy(token);
        if (token.size() >= 3 && static_cast<unsigned char>(token[0]) == 0xEF &&
            static_cast<unsigned char>(token[1]) == 0xBB && static_cast<unsigned char>(token[2]) == 0xBF) {
            token = token.substr(3);
        }
    }
    gTokenPath = path;
    gTokenStamp = stamp;
    gToken = token;
    return token;
}

bool ConstantTimeEquals(const std::string& left, const std::string& right) {
    unsigned char diff = static_cast<unsigned char>(left.size() != right.size());
    const std::size_t length = std::max(left.size(), right.size());
    for (std::size_t index = 0; index < length; ++index) {
        const unsigned char a = index < left.size() ? static_cast<unsigned char>(left[index]) : 0u;
        const unsigned char b = index < right.size() ? static_cast<unsigned char>(right[index]) : 0u;
        diff |= static_cast<unsigned char>(a ^ b);
    }
    return diff == 0u;
}

bool AuthAccepted(const Protocol::RequestEnvelope& request) {
    if (TruthyEnvironmentFlag("VW_MCP_INSECURE_NO_AUTH")) return true;
    const std::string required = RequiredAuthToken();
    return !required.empty() && ConstantTimeEquals(request.authToken, required);
}

// ---- main-thread pump window (message-only, L3) ---------------------------

#if defined(_WINDOWS)
constexpr wchar_t kPumpWindowClass[] = L"VectorworksMCPBridgePumpV2";
constexpr UINT_PTR kPumpTimerId = 1;
constexpr UINT kPumpIntervalMs = 50;
constexpr UINT kPumpWakeMessage = WM_APP + 0x4d3;
std::atomic_bool gPumpReady{false};
HWND gPumpWindow = nullptr;
HINSTANCE gPumpInstance = nullptr;
bool gPumpClassRegistered = false;
int gModuleAnchor = 0;

LRESULT CALLBACK PumpWndProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    try {
        if ((message == WM_TIMER && wParam == kPumpTimerId) || message == kPumpWakeMessage) {
            OnVectorworksMainPluginEvent();
            return 0;
        }
        return DefWindowProcW(window, message, wParam, lParam);
    } catch (...) {
        return 0;
    }
}

HINSTANCE ModuleHandle() {
    HMODULE module = nullptr;
    if (GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&gModuleAnchor), &module) && module) {
        return reinterpret_cast<HINSTANCE>(module);
    }
    return GetModuleHandleW(nullptr);
}

bool PinModule() {
    HMODULE module = nullptr;
    return GetModuleHandleExW(
               GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
               reinterpret_cast<LPCWSTR>(&gModuleAnchor), &module) != 0 && module != nullptr;
}

bool StartPump() {
    if (gPumpWindow) {
        gPumpReady.store(true);
        return true;
    }
    gPumpInstance = ModuleHandle();
    WNDCLASSW windowClass = {};
    windowClass.lpfnWndProc = PumpWndProc;
    windowClass.hInstance = gPumpInstance;
    windowClass.lpszClassName = kPumpWindowClass;
    if (RegisterClassW(&windowClass) != 0) {
        gPumpClassRegistered = true;
    } else if (GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
        return false;
    }
    gPumpWindow = CreateWindowExW(0, kPumpWindowClass, L"Vectorworks MCP Bridge Pump", 0, 0, 0, 0, 0,
                                  HWND_MESSAGE, nullptr, gPumpInstance, nullptr);
    if (!gPumpWindow) return false;
    if (SetTimer(gPumpWindow, kPumpTimerId, kPumpIntervalMs, nullptr) == 0) {
        DestroyWindow(gPumpWindow);
        gPumpWindow = nullptr;
        return false;
    }
    gPumpReady.store(true);
    return true;
}

void StopPump() {
    gPumpReady.store(false);
    if (gPumpWindow) {
        KillTimer(gPumpWindow, kPumpTimerId);
        DestroyWindow(gPumpWindow);
        gPumpWindow = nullptr;
    }
    if (gPumpClassRegistered && gPumpInstance) {
        UnregisterClassW(kPumpWindowClass, gPumpInstance);
        gPumpClassRegistered = false;
    }
}

void NotifyPump() {
    HWND window = gPumpWindow;
    if (window && gPumpReady.load()) {
        PostMessageW(window, kPumpWakeMessage, 0, 0);
    }
}

bool PumpReady() {
    return gPumpReady.load();
}
#else
bool PinModule() { return true; }
bool StartPump() { return false; }
void StopPump() {}
void NotifyPump() {}
bool PumpReady() { return false; }
#endif

// ---- transport lifecycle ----------------------------------------------------

DispatchOutcome DispatchFromSocket(const Protocol::RequestEnvelope& request);

NativeTransportOptions TransportOptionsFromEnvironment() {
    NativeTransportOptions options;
    if (const char* host = std::getenv("VW_MCP_HOST")) {
        if (host[0] != '\0') options.host = host;
    }
    if (const char* port = std::getenv("VW_MCP_PORT")) {
        try {
            const auto parsed = std::stoul(port);
            if (parsed > 0u && parsed <= 65535u) {
                options.portFirst = static_cast<std::uint16_t>(parsed);
                options.portLast = options.portFirst;
            }
        } catch (...) {
        }
    }
    return options;
}

void WriteDiscovery() {
    Discovery::InstanceInfo info;
    info.pid = static_cast<std::uint32_t>(GetCurrentProcessId());
    info.port = gBoundPort.load();
    info.protocol = Protocol::kWireProtocolVersion;
    info.capabilityRevision = kCapabilityRevision;
    info.bridgeVersion = kBridgeVersion;
    info.buildId = VWMCP_BUILD_ID;
    info.vectorworksVersion = VectorworksVersionName();
    info.sessionId = Documentation::BridgeSessionId();
    info.startedAt = gStartedAt;
    std::string error;
    if (!Discovery::Write(info, &error)) {
        Discovery::AppendLog("discovery_failed", error);
    }
}

void MaybeStartTransport(bool force = false) {
    if (gShutdown.load()) return;
    std::lock_guard<std::mutex> lock(gStartMutex);
    if (Transport().IsRunning()) return;
    const std::int64_t now = SteadyNowMs();
    if (!force && now < gNextStartAttemptMs) return;
    const auto options = TransportOptionsFromEnvironment();
    try {
        Transport().Start(options, DispatchFromSocket);
        gBoundPort.store(Transport().Port());
        gBackoffSeconds = 2;
        gNextStartAttemptMs = 0;
        gLastStartError.clear();
        WriteDiscovery();
        Discovery::AppendLog("started", options.host + ":" + std::to_string(gBoundPort.load()) +
            " build=" + VWMCP_BUILD_ID);
    } catch (const std::exception& error) {
        const std::string message = error.what();
        // Rate-limited log: only when the error changes or the back-off grows.
        if (message != gLastStartError || gBackoffSeconds < 60) {
            Discovery::AppendLog("start_failed", message + " (retry in " + std::to_string(gBackoffSeconds) + " s)");
        }
        gLastStartError = message;
        gNextStartAttemptMs = now + static_cast<std::int64_t>(gBackoffSeconds) * 1000;
        gBackoffSeconds = std::min(60, gBackoffSeconds * 2);
    } catch (...) {
        gLastStartError = "unknown exception";
        gNextStartAttemptMs = now + static_cast<std::int64_t>(gBackoffSeconds) * 1000;
        gBackoffSeconds = std::min(60, gBackoffSeconds * 2);
    }
}

void FullShutdown(const char* reason) {
    gShutdown.store(true);
    StopPump();
    Scheduler().CancelAll(std::string("native bridge is shutting down: ") + reason);
    try {
        Transport().Stop();
    } catch (...) {
    }
    Discovery::Remove();
    Discovery::AppendLog("shutdown", reason);
    gInitialized.store(false);
}

// ---- socket-thread actions ---------------------------------------------------

struct StaticCapabilityStrings {
    std::string fingerprint;
    std::string implementedActions;
    std::string createObjectTypes;
    std::string objectReadFeatures;
    std::size_t handlerCount = 0;
};

const StaticCapabilityStrings& CapabilityStrings() {
    static const StaticCapabilityStrings strings = [] {
        StaticCapabilityStrings value;
        value.fingerprint = CapabilityFingerprint(true);
        value.implementedActions = ImplementedActionsJson(true);
        value.createObjectTypes = CreateObjectTypesJson(true);
        value.objectReadFeatures = ObjectReadFeaturesJson(true);
        value.handlerCount = ImplementedActionCount(true);
        return value;
    }();
    return strings;
}

std::string PingJson() {
    const bool ready = PumpReady();
    const StaticCapabilityStrings& caps = CapabilityStrings();
    std::string json = R"({"pong":true,"version":"native-sdk-bridge-phase4","bridge_kind":"native_sdk_bridge_phase4","dispatch_mode":"native_sdk","handlers":)";
    json += std::to_string(caps.handlerCount);
    json += ",\"cad_api_safe\":";
    json += ready ? "true" : "false";
    json += ",\"transport_only\":";
    json += ready ? "false" : "true";
    json += R"(,"native_bridge":true,"native_phase":4)";
    json += ",\"protocol\":" + std::to_string(Protocol::kWireProtocolVersion);
    json += ",\"capability_revision\":" + std::to_string(kCapabilityRevision);
    json += ",\"capability_fingerprint\":" + JsonString(caps.fingerprint);
    json += ",\"bridge_version\":" + JsonString(kBridgeVersion);
    json += ",\"build_id\":" + JsonString(VWMCP_BUILD_ID);
    json += ",\"git_hash\":" + JsonString(VWMCP_GIT_HASH);
    json += ",\"pid\":" + std::to_string(GetCurrentProcessId());
    json += ",\"port\":" + std::to_string(gBoundPort.load());
    json += ",\"session_id\":" + JsonString(Documentation::BridgeSessionId());
    json += ",\"bridge_session_id\":" + JsonString(Documentation::BridgeSessionId());
    json += ",\"vectorworks_version\":" + JsonString(VectorworksVersionName());
    json += ",\"started_at\":" + JsonString(gStartedAt);
    json += ",\"ui_state\":" + JsonString(UiStateName(UiGate::Published()));
    json += ",\"ui_state_reason\":" + JsonString(UiGate::PublishedReason());
    json += ",\"handler_running_ms\":" + std::to_string(Scheduler().RunningMilliseconds());
    const std::string running = Scheduler().RunningAction();
    json += ",\"running_action\":";
    json += running.empty() ? std::string("null") : JsonString(running);
    json += ",\"queue_depth\":" + std::to_string(Scheduler().QueueDepth());
    json += ",\"job_count\":" + std::to_string(Scheduler().JobCount());
    const std::int64_t lastTick = gLastTickMs.load();
    const std::int64_t idle = lastTick > 0 ? std::max<std::int64_t>(0, SteadyNowMs() - lastTick) : -1;
    json += ",\"main_context_idle_ms\":" + std::to_string(idle);
    json += ",\"active_document\":" + Docs::CachedActiveDocumentJson();
    json += ",\"open_document_count\":" + std::to_string(Docs::CachedOpenDocumentCount());
    json += ",\"client_count\":" + std::to_string(Transport().ClientCount());
    json += ",\"implemented_actions\":" + caps.implementedActions;
    json += ",\"create_object_types\":" + caps.createObjectTypes;
    json += ",\"object_read_features\":" + caps.objectReadFeatures;
    json += ",\"cad_handlers_implemented\":true";
    json += ",\"main_context_pump\":\"win32_ui_timer\",\"main_context_pump_window\":\"message_only\"";
    json += ",\"main_context_pump_ready\":";
    json += ready ? "true" : "false";
    json += R"(,"dispatch_wakeup":"win32_message","pump_watchdog_ms":50,"pump_budget_ms":8)";
    json += ",\"modal_dialog\":" + DialogWatch::CurrentDialogJson();
    json += ",\"auto_dismiss_dialogs\":";
    json += DialogWatch::AutoDismissEnabled() ? "true" : "false";
    json += ",\"dismissed_dialogs\":" + DialogWatch::RecentDismissedJson();
    json += ",\"max_operations\":" + std::to_string(Legacy::kMaxApplyOperationCount);
    json += ",\"script_enabled\":";
    json += Scripting::ScriptingAllowed() ? "true" : "false";
    json += ",\"script_engine\":" + JsonString(Scripting::ScriptEngineState());
    if (Scripting::ScriptEnginePoisoned()) {
        json += ",\"script_engine_fault\":" + JsonString(Scripting::ScriptFaultCodeHex());
        json += ",\"script_engine_faults\":" + std::to_string(Scripting::ScriptFaultCount());
    }
    json += "}";
    return json;
}

std::string CapabilitiesJson() {
    std::string json = CapabilitiesResultJson(true);
    if (!json.empty() && json.back() == '}') {
        json.pop_back();
        json += ",\"bridge_version\":" + JsonString(kBridgeVersion);
        json += ",\"build_id\":" + JsonString(VWMCP_BUILD_ID);
        json += ",\"vectorworks_version\":" + JsonString(VectorworksVersionName());
        json += ",\"limits\":{\"max_frame_bytes\":" + std::to_string(Protocol::kMaxFrameBytes) +
            ",\"default_timeout_ms\":" + std::to_string(RequestScheduler::kDefaultTimeoutMs) +
            ",\"max_timeout_ms\":" + std::to_string(RequestScheduler::kMaxTimeoutMs) +
            ",\"default_busy_wait_ms\":3000,\"max_jobs\":" + std::to_string(RequestScheduler::kMaxJobs) +
            ",\"job_retention_ms\":" + std::to_string(RequestScheduler::kJobRetentionMs) +
            ",\"max_clients\":16,\"client_idle_seconds\":120,\"max_operations\":" +
            std::to_string(Legacy::kMaxApplyOperationCount) + "}";
        json += "}";
    }
    return json;
}

std::string UiStateText() {
    return UiStateName(UiGate::Published());
}

DispatchOutcome DispatchFromSocket(const Protocol::RequestEnvelope& request) {
    DispatchOutcome outcome;
    try {
        if (!AuthAccepted(request)) {
            outcome.response = Protocol::Fail(
                request.id, "auth_failed", "native bridge authentication failed", false, "not_started");
            return outcome;
        }
        const ActionSpec* spec = FindActionSpec(request.action);
        if (!spec) {
            outcome.response = Protocol::Fail(
                request.id, "unknown_action", "unknown native bridge action: " + request.action, false, "not_started");
            return outcome;
        }
        if (request.action == "ping") {
            outcome.response = Protocol::Ok(request.id, PingJson());
            return outcome;
        }
        if (request.action == "capabilities") {
            outcome.response = Protocol::Ok(request.id, CapabilitiesJson());
            return outcome;
        }
        if (request.action == "stop") {
            // v2 (C3): stop closes only the calling connection.
            outcome.response = Protocol::Ok(request.id, R"({"connection_closed":true,"bridge_running":true})");
            outcome.closeConnection = true;
            return outcome;
        }
        const Params params = ParseParams(request.paramsJson);
        if (request.action == "job_status") {
            outcome.response = Protocol::Ok(
                request.id, Scheduler().JobStatusJson(GetStringParam(params, "job_id"), UiStateText()));
            return outcome;
        }
        if (request.action == "cancel_job") {
            outcome.response = Protocol::Ok(request.id, Scheduler().CancelJobJson(GetStringParam(params, "job_id")));
            return outcome;
        }
        if (request.action == "dismiss_home") {
            // Socket thread: classification and PostMessage are thread-safe and
            // never send a message to the (possibly blocked) main thread.
            std::string json = DialogWatch::HandleDismissHome(GetClampedIntParam(params, "wait_ms", 3000, 0, 15000));
            json.pop_back();
            json += ",\"ui_state\":" + JsonString(UiStateText()) + "}";
            outcome.response = Protocol::Ok(request.id, json);
            return outcome;
        }
        if (spec->context != ExecutionContext::VectorworksMainPluginContext) {
            outcome.response = Protocol::Fail(
                request.id, "unknown_action", "native bridge action has no transport handler: " + request.action);
            return outcome;
        }
        if (gShutdown.load()) {
            outcome.response = Protocol::Fail(request.id, "bridge_stopping", "native bridge is shutting down", true);
            return outcome;
        }
        if (!PumpReady()) {
            outcome.response = Protocol::Fail(
                request.id, "bridge_not_ready", "native bridge main-thread pump is not running", true);
            return outcome;
        }
        SubmitOptions options;
        std::int64_t timeoutMs = request.timeoutMs;
        if (timeoutMs <= 0 && HasParam(params, "timeout_ms")) {
            timeoutMs = GetClampedIntParam(params, "timeout_ms", 25000, 1, 600000);
        }
        options.timeoutMs = timeoutMs > 0 ? timeoutMs : RequestScheduler::kDefaultTimeoutMs;
        options.async = GetLenientBoolParam(params, "async", false);
        options.busyWaitMs = GetClampedIntParam(params, "busy_wait_ms", 3000, 0, 600000);
        options.needsDocument = spec->needsDocument;
        options.mutates = spec->mayWriteDocument;
        options.dialogSeq = DialogWatch::DismissSequence();
        // Watchdog on the waiting socket thread: if the request is blocked by,
        // or its handler opened, the Home screen (or the update notice), close
        // it (PostMessage only; never any other dialog).
        const auto watchdog = [] {
            const UiState published = UiGate::Published();
            if (published == UiState::Modal || published == UiState::Loading ||
                !Scheduler().RunningAction().empty()) {
                DialogWatch::TryAckHandlerMessage("request_watchdog");
                DialogWatch::TryDismissKnown("request_watchdog", true);
            }
        };
        outcome.response = Scheduler().SubmitAndWait(request, options, UiStateText, watchdog);
        return outcome;
    } catch (const Protocol::BridgeError& error) {
        outcome.response = Protocol::FromBridgeError(request.id, error);
    } catch (const std::invalid_argument& error) {
        outcome.response = Protocol::Fail(request.id, "invalid_request", error.what(), false, "not_started");
    } catch (const std::exception& error) {
        outcome.response = Protocol::Fail(request.id, "internal_error", error.what(), true, "not_started");
    } catch (...) {
        outcome.response = Protocol::Fail(request.id, "internal_error", "native bridge socket dispatcher failed", true, "not_started");
    }
    return outcome;
}

// ---- main-thread dispatch -----------------------------------------------------

std::string RouteMain(const std::string& action, const Params& params) {
    if (action == "list_documents") return Docs::HandleListDocuments(params);
    if (action == "activate_document") return Docs::HandleActivateDocument(params);
    if (action == "new_document") return Docs::HandleNewDocument(params);
    if (action == "close_document") return Docs::HandleCloseDocument(params);
    if (action == "open_document") return Docs::HandleOpenDocument(params);
    if (action == "save_document") return Docs::HandleSaveDocument(params);
    if (action == "get_view") return ViewCapture::HandleGetView(params);
    if (action == "set_view") return ViewCapture::HandleSetView(params);
    if (action == "saved_views") return ViewCapture::HandleSavedViews(params);
    if (action == "capture_view" || action == "export_image") {
        Legacy::ValidateBindingIfPresent(params);
        std::string result = action == "capture_view"
            ? ViewCapture::HandleCaptureView(params)
            : ViewCapture::HandleExportImage(params);
        Legacy::ValidateBindingIfPresent(params);
        return result;
    }
    if (action == "layers") return Organization::HandleLayers(params);
    if (action == "stories") return Organization::HandleStories(params);
    if (action == "run_script") return Scripting::HandleRunScript(params);
    if (action == "manage_classes") {
        std::string extended = Organization::HandleClassesExtension(params);
        if (!extended.empty()) return extended;
    }
    if (auto legacy = Legacy::Dispatch(action, params)) {
        return *legacy;
    }
    throw Protocol::BridgeError("unknown_action", "unknown native bridge CAD action: " + action);
}

std::string ViewDocumentErrorJson(const ViewDocument::Error& exc) {
    const bool unknown = exc.State() == ViewDocument::CommitState::Unknown;
    std::string error = "{\"code\":";
    error += JsonString(unknown ? "unknown_commit_state" : ViewDocument::ErrorCodeName(exc.Code()));
    error += ",\"message\":" + JsonString(exc.what());
    error += ",\"requested_path\":" + JsonString(exc.RequestedPath());
    error += ",\"active_path\":" + JsonString(exc.ActivePath());
    error += ",\"commit_state\":" + JsonString(ViewDocument::CommitStateName(exc.State()));
    error += ",\"retryable\":false}";
    return error;
}

Protocol::ResponseEnvelope RunOnMainThread(const ScheduledRequest& entry) {
    const auto& request = entry.request;
    const ActionSpec* spec = FindActionSpec(request.action);
    const bool mutates = spec && spec->mayWriteDocument;
    try {
        const Params params = ParseParams(request.paramsJson);
        Docs::Refresh(true);
        if (spec && spec->needsDocument && !Docs::HasActiveDocument()) {
            throw Protocol::BridgeError(
                "no_active_document", "no Vectorworks document is open; open or create a document first",
                false, "not_started");
        }
        Docs::EnsureExpectedDocument(params);
        return Protocol::Ok(request.id, RouteMain(request.action, params));
    } catch (const Protocol::BridgeError& error) {
        return Protocol::FromBridgeError(request.id, error);
    } catch (const ViewDocument::Error& exc) {
        // Legacy (0.x) form: `error` is a JSON-encoded object string.
        const bool unknown = exc.State() == ViewDocument::CommitState::Unknown;
        auto response = Protocol::Fail(
            request.id,
            unknown ? "unknown_commit_state" : ViewDocument::ErrorCodeName(exc.Code()),
            ViewDocumentErrorJson(exc),
            false,
            unknown ? "unknown" : ViewDocument::CommitStateName(exc.State()));
        if (response.commitState == "accepted") response.commitState = "unknown";
        return response;
    } catch (const NativeIO::Error& exc) {
        return Protocol::Fail(request.id, NativeIO::ErrorCodeName(exc.Code()), exc.what(), false,
                              mutates ? "rolled_back" : "not_started");
    } catch (const std::invalid_argument& exc) {
        return Protocol::Fail(request.id, "invalid_request", exc.what(), false, mutates ? "rolled_back" : "not_started");
    } catch (const std::exception& exc) {
        return Protocol::Fail(request.id, "handler_failed", exc.what(), false, mutates ? "unknown" : "not_started");
    } catch (...) {
        return Protocol::Fail(request.id, "handler_failed", "native bridge CAD handler failed", false,
                              mutates ? "unknown" : "not_started");
    }
}

void AppendTiming(Protocol::ResponseEnvelope& response, double queueWaitMs, double handlerMs) {
    if (!response.success || response.resultJson.size() < 2u ||
        response.resultJson.front() != '{' || response.resultJson.back() != '}') {
        return;
    }
    response.resultJson.pop_back();
    if (response.resultJson.size() > 1u) response.resultJson += ",";
    response.resultJson += "\"timing\":{\"queue_wait_ms\":" + JsonNumber(queueWaitMs) +
        ",\"handler_ms\":" + JsonNumber(handlerMs) +
        ",\"total_native_ms\":" + JsonNumber(queueWaitMs + handlerMs) + "}}";
}

void AppendDismissed(Protocol::ResponseEnvelope& response, std::uint64_t sinceSeq) {
    if (DialogWatch::DismissSequence() <= sinceSeq) return;
    const std::string list = DialogWatch::DismissedSinceJson(sinceSeq);
    const bool home = DialogWatch::HomeDismissedSince(sinceSeq);
    if (response.success) {
        if (response.resultJson.size() < 2u || response.resultJson.front() != '{' || response.resultJson.back() != '}') return;
        response.resultJson.pop_back();
        if (response.resultJson.size() > 1u) response.resultJson += ",";
        response.resultJson += "\"dismissed_dialogs\":" + list;
        if (home) response.resultJson += ",\"home_dismissed\":true";
        response.resultJson += "}";
        return;
    }
    std::string details = response.detailsJson;
    if (details.size() < 2u || details.front() != '{' || details.back() != '}') details = "{}";
    details.pop_back();
    if (details.size() > 1u) details += ",";
    details += "\"dismissed_dialogs\":" + list;
    if (home) details += ",\"home_dismissed\":true";
    response.detailsJson = details + "}";
}

class ScopedFlag {
public:
    explicit ScopedFlag(std::atomic_bool& flag) : flag_(flag) {}
    ~ScopedFlag() { flag_.store(false); }
    ScopedFlag(const ScopedFlag&) = delete;
    ScopedFlag& operator=(const ScopedFlag&) = delete;

private:
    std::atomic_bool& flag_;
};

}  // namespace

void OnVectorworksMainPluginEvent() {
    try {
        gLastTickMs.store(SteadyNowMs());
        if (gShutdown.load() || !gInitialized.load()) return;
        if (gPumpActive.exchange(true)) {
            // Re-entered from a nested message loop while a handler runs:
            // never dequeue here, only publish a non-idle UI state.
            std::string reason;
            const UiState state = UiGate::ComputeWin32State(&reason);
            if (state != UiState::Idle) UiGate::Publish(state, reason);
            // A handler (close_document of the last document, new/open
            // document) may have opened the Home screen synchronously; its
            // nested modal loop dispatches this timer. Close it.
            DialogWatch::TryAckHandlerMessage("during_handler");
            DialogWatch::TryDismissKnown("during_handler", true);
            return;
        }
        ScopedFlag reset(gPumpActive);
        MaybeStartTransport();

        constexpr std::size_t kMaxRequestsPerTick = 8u;
        constexpr std::int64_t kTickBudgetMs = 8;
        const std::int64_t tickStarted = SteadyNowMs();
        std::size_t processed = 0;
        while (true) {
            std::string reason;
            UiState state = UiGate::ComputeWin32State(&reason);
            if (state == UiState::Idle) {
                Docs::Refresh(false);
                if (!Docs::HasActiveDocument()) {
                    state = UiState::NoDocument;
                    reason = "no_document";
                }
            }
            UiGate::Publish(state, reason);
            if ((state == UiState::Modal || state == UiState::Loading) && Scheduler().QueueDepth() > 0u) {
                // A queued request is blocked: dismiss the Home screen /
                // update notice if it is the only dialog (DialogWatch rules).
                DialogWatch::TryDismissKnown("blocked_request", true);
            }
            if (processed >= kMaxRequestsPerTick || SteadyNowMs() - tickStarted > kTickBudgetMs) break;
            // The Home web view tears down asynchronously after it is closed;
            // creating/opening a document in that window crashed CEF (live).
            if (DialogWatch::MsSinceHomeClose() < 1000) break;
            auto entry = Scheduler().TryDequeue(state);
            if (!entry) break;
            const double queueWaitMs = std::chrono::duration<double, std::milli>(
                entry->startedAt - entry->enqueuedAt).count();
            const auto handlerStarted = std::chrono::steady_clock::now();
            Protocol::ResponseEnvelope response;
            DialogWatch::BeginHandlerScope();
            try {
                response = RunOnMainThread(*entry);
            } catch (...) {
                response = Protocol::Fail(entry->request.id, "handler_failed", "native bridge CAD handler failed", false, "unknown");
            }
            DialogWatch::EndHandlerScope();
            const double handlerMs = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - handlerStarted).count();
            AppendDismissed(response, entry->options.dialogSeq);
            AppendTiming(response, queueWaitMs, handlerMs);
            Scheduler().Complete(entry, std::move(response));
            ++processed;
        }
        if (Scheduler().QueueDepth() > 0u) NotifyPump();
    } catch (...) {
        // M4: never let an exception escape into the Vectorworks window proc.
    }
}

void OnPluginLoadStartTransport() {
    try {
        if (gInitialized.load()) {
            MaybeStartTransport(true);
            return;
        }
        gShutdown.store(false);
        if (!PinModule()) {
            Discovery::AppendLog("pin_failed", "the native bridge DLL could not be pinned for the process lifetime");
            return;
        }
        UiGate::InitializeOnMainThread();
        Docs::ResetForModuleInit();
        Handles::ClearAll();
        Legacy::ResetCaches();
        Scheduler().Reset();
        Scheduler().SetNotifier([] { NotifyPump(); });
        Scheduler().SetBusyDetailsProvider([] {
            const std::string dialog = DialogWatch::CurrentDialogJson();
            return dialog == "null" ? std::string() : ",\"modal_dialog\":" + dialog;
        });
        gStartedAt = IsoUtcNow();
        if (!StartPump()) {
            Discovery::AppendLog("pump_failed", "the message-only main-thread pump window could not be created");
            return;
        }
        gInitialized.store(true);
        MaybeStartTransport(true);
    } catch (const std::exception& error) {
        Discovery::AppendLog("init_failed", error.what());
    } catch (...) {
        Discovery::AppendLog("init_failed", "unknown exception");
    }
}

void OnPluginModuleDeinit() {
    try {
        // Vectorworks may release (deinit) a module that registers no
        // interfaces right after loading it; the DLL is pinned, so the bridge
        // keeps running. Only a deinit after the main frame went away is an
        // application shutdown (M7).
        if (!gInitialized.load()) return;
        if (UiGate::MainWindowEverAlive() && !UiGate::MainWindowAlive()) {
            FullShutdown("module deinit during application shutdown");
        } else {
            Discovery::AppendLog("deinit_ignored", "module released while Vectorworks is running; bridge stays up");
        }
    } catch (...) {
    }
}

}  // namespace VectorworksMCP
