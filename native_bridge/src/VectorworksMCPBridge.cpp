#include "BridgeDispatcher.hpp"
#include "BridgeProtocol.hpp"
#include "CadRequestQueue.hpp"

#include <string>

namespace VectorworksMCP {

namespace {

CadRequestQueue gCadQueue;
bool gStopRequested = false;

Protocol::ResponseEnvelope HandlePingOnTransportThread(const Protocol::RequestEnvelope& request) {
    return {
        request.id,
        true,
        R"({"pong":true,"version":"native-scaffold","bridge_kind":"native_sdk_bridge_scaffold","dispatch_mode":"native_sdk","handlers":7,"cad_api_safe":true,"transport_only":false,"native_bridge":true})",
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
    // SDK hook placeholder: start the local socket worker here.
    // The worker may parse frames and answer ping, but any action listed in
    // kPhaseOneActions must be enqueued with gCadQueue instead of touching CAD.
    gStopRequested = false;
}

void OnPluginUnloadStopTransport() {
    // SDK hook placeholder: stop the socket worker and release port 9877.
    gStopRequested = true;
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
        gStopRequested = true;
        return {request.id, true, R"("Native bridge stop requested")", ""};
    }
    if (RequiresCadMainContext(request.action)) {
        gCadQueue.EnqueueFromSocketThread(request);
        return gCadQueue.WaitForResponseOnSocketThread(request.id);
    }
    return {request.id, false, "", "unknown native bridge action: " + request.action};
}

bool StopRequested() {
    return gStopRequested;
}

}  // namespace VectorworksMCP
