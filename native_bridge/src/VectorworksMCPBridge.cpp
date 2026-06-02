#include "BridgeDispatcher.hpp"
#include "BridgeProtocol.hpp"
#include "CadRequestQueue.hpp"
#include "NativeTransport.hpp"

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <string>

namespace VectorworksMCP {

namespace {

CadRequestQueue gCadQueue;
NativeTransport gTransport;
std::atomic_bool gStopRequested{false};
constexpr auto kCadRequestTimeout = std::chrono::seconds(30);
constexpr bool kCadHandlersImplemented = false;

NativeTransportOptions GetTransportOptionsFromEnvironment() {
    NativeTransportOptions options;
    if (const char* host = std::getenv("VW_MCP_HOST")) {
        if (host[0] != '\0') {
            options.host = host;
        }
    }
    if (const char* port = std::getenv("VW_MCP_PORT")) {
        try {
            const auto parsed = std::stoul(port);
            if (parsed <= 65535u) {
                options.port = static_cast<std::uint16_t>(parsed);
            }
        } catch (...) {
            // Keep the default port when the environment is malformed.
        }
    }
    return options;
}

Protocol::ResponseEnvelope HandlePingOnTransportThread(const Protocol::RequestEnvelope& request) {
    return {
        request.id,
        true,
        R"({"pong":true,"version":"native-scaffold-phase0","bridge_kind":"native_sdk_bridge_scaffold","dispatch_mode":"native_sdk","handlers":2,"cad_api_safe":false,"transport_only":true,"native_bridge":true,"native_phase":0,"implemented_actions":["ping","stop"],"cad_handlers_implemented":false})",
        "",
    };
}

Protocol::ResponseEnvelope DispatchCadRequestOnVectorworksMainContext(const Protocol::RequestEnvelope& request) {
    // Replace this switch with Vectorworks SDK calls after the SDK-backed
    // ObjectExample worktree builds. This function must run only on the
    // Vectorworks main/plugin event context.
    return {
        request.id,
        false,
        "",
        "native bridge CAD handler not implemented yet: " + request.action,
    };
}

}  // namespace

void OnPluginLoadStartTransport() {
    gStopRequested.store(false);
    gCadQueue.ResetCancellation();
    gTransport.Start(GetTransportOptionsFromEnvironment(), DispatchFromSocketWorker);
}

void OnPluginUnloadStopTransport() {
    gStopRequested.store(true);
    gCadQueue.CancelAll("native bridge is unloading");
    gTransport.Stop();
}

void OnVectorworksMainPluginEvent() {
    // SDK hook placeholder: call this from an idle, timer, or event callback
    // that is allowed to use the Vectorworks document API.
    while (auto request = gCadQueue.TryDequeueOnVectorworksMainContext()) {
        gCadQueue.CompleteFromVectorworksMainContext(DispatchCadRequestOnVectorworksMainContext(*request));
    }
}

Protocol::ResponseEnvelope DispatchFromSocketWorker(const Protocol::RequestEnvelope& request) {
    if (request.action == "ping") {
        return HandlePingOnTransportThread(request);
    }
    if (request.action == "stop") {
        gStopRequested.store(true);
        gCadQueue.CancelAll("native bridge stop requested");
        return {request.id, true, R"("Native bridge stop requested")", ""};
    }
    if (RequiresCadMainContext(request.action)) {
        if (!kCadHandlersImplemented) {
            return {request.id, false, "", "native bridge phase 0 CAD handlers are not implemented: " + request.action};
        }
        if (gStopRequested.load()) {
            return {request.id, false, "", "native bridge is stopping"};
        }
        if (auto enqueueFailure = gCadQueue.EnqueueFromSocketThread(request)) {
            return *enqueueFailure;
        }
        return gCadQueue.WaitForResponseOnSocketThread(request.id, kCadRequestTimeout);
    }
    return {request.id, false, "", "unknown native bridge action: " + request.action};
}

bool StopRequested() {
    return gStopRequested.load();
}

std::uint16_t NativeTransportPortForDiagnostics() {
    return gTransport.Port();
}

}  // namespace VectorworksMCP
