#include "BridgeDispatcher.hpp"
#include "BridgeProtocol.hpp"
#include "CadRequestQueue.hpp"

#include <chrono>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>

namespace VectorworksMCP {
void OnPluginLoadStartTransport();
void OnPluginUnloadStopTransport();
Protocol::ResponseEnvelope DispatchFromSocketWorker(const Protocol::RequestEnvelope& request);
bool StopRequested();
}  // namespace VectorworksMCP

using VectorworksMCP::CadRequestQueue;
using VectorworksMCP::FindActionSpec;
using VectorworksMCP::RequiresCadMainContext;
using VectorworksMCP::StopRequested;
using VectorworksMCP::DispatchFromSocketWorker;
using VectorworksMCP::OnPluginLoadStartTransport;
using VectorworksMCP::OnPluginUnloadStopTransport;
using VectorworksMCP::Protocol::DecodeFrameHeader;
using VectorworksMCP::Protocol::EncodeFrameHeader;
using VectorworksMCP::Protocol::ParseRequestEnvelope;
using VectorworksMCP::Protocol::RequestEnvelope;
using VectorworksMCP::Protocol::ResponseEnvelope;
using VectorworksMCP::Protocol::SerializeResponseEnvelope;

void Require(bool condition, const char* message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

void RequireContains(const std::string& value, const std::string& expected, const char* message) {
    if (value.find(expected) == std::string::npos) {
        throw std::runtime_error(message);
    }
}

template <typename Fn>
void RequireThrows(Fn fn, const char* message) {
    try {
        fn();
    } catch (const std::exception&) {
        return;
    }
    throw std::runtime_error(message);
}

void TestProtocol() {
    const auto header = EncodeFrameHeader(513);
    Require(header[0] == 0 && header[1] == 0 && header[2] == 2 && header[3] == 1, "frame header encoded incorrectly");
    Require(DecodeFrameHeader(header) == 513, "frame header decoded incorrectly");
    RequireThrows([] { EncodeFrameHeader(0); }, "zero frame size should fail");

    const auto ping = ParseRequestEnvelope(R"({"id":"abc","action":"ping"})");
    Require(ping.id == "abc", "request id parse failed");
    Require(ping.action == "ping", "request action parse failed");
    Require(ping.paramsJson == "{}", "missing params should default to object");

    const auto objects = ParseRequestEnvelope(R"({"params":{"limit":10,"layer":"A"},"action":"get_objects","id":"req-1"})");
    Require(objects.id == "req-1", "out-of-order id parse failed");
    Require(objects.action == "get_objects", "out-of-order action parse failed");
    Require(objects.paramsJson == R"({"limit":10,"layer":"A"})", "params object capture failed");

    const auto escaped = ParseRequestEnvelope(R"({"id":"a\u0031","action":"p\ting","params":{}})");
    Require(escaped.id == "a1", "unicode escape parse failed");
    Require(escaped.action == std::string("p\ting"), "escaped tab parse failed");

    RequireThrows([] { ParseRequestEnvelope(R"([])"); }, "non-object request should fail");
    RequireThrows([] { ParseRequestEnvelope(R"({"action":"ping"})"); }, "missing id should fail");
    RequireThrows([] { ParseRequestEnvelope(R"({"id":"abc"})"); }, "missing action should fail");
    RequireThrows([] { ParseRequestEnvelope(R"({"id":"abc","action":"ping","params":[]})"); }, "array params should fail");
    RequireThrows([] { ParseRequestEnvelope(R"({"id":"abc","id":"dup","action":"ping"})"); }, "duplicate id should fail");

    const auto success = SerializeResponseEnvelope(ResponseEnvelope{"abc", true, R"({"pong":true})", ""});
    Require(success == R"({"id":"abc","success":true,"result":{"pong":true}})", "success response serialization failed");

    const auto failure = SerializeResponseEnvelope(ResponseEnvelope{"abc", false, "", "bad \"wall\""});
    Require(failure == R"({"id":"abc","success":false,"error":"bad \"wall\""})", "failure response serialization failed");

    RequireThrows([] { SerializeResponseEnvelope(ResponseEnvelope{"", true, R"({"ok":true})", ""}); }, "empty response id should fail");
    RequireThrows([] { SerializeResponseEnvelope(ResponseEnvelope{"abc", true, "", ""}); }, "success without result should fail");
    RequireThrows([] { SerializeResponseEnvelope(ResponseEnvelope{"abc", true, R"({"ok":true} trailing)", ""}); }, "invalid success result should fail");
    RequireThrows([] { SerializeResponseEnvelope(ResponseEnvelope{"abc", false, "", ""}); }, "failure without error should fail");
}

void TestDispatcherMetadata() {
    const auto* ping = FindActionSpec("ping");
    Require(ping != nullptr, "ping action spec missing");
    Require(!RequiresCadMainContext("ping"), "ping should not require CAD main context");
    Require(RequiresCadMainContext("get_layers"), "get_layers should require CAD main context");
    Require(FindActionSpec("missing") == nullptr, "missing action should not have a spec");
}

void TestQueue() {
    CadRequestQueue queue(1);
    const RequestEnvelope first{"r1", "get_layers", "{}"};
    const RequestEnvelope second{"r2", "get_objects", R"({"limit":10})"};

    Require(!queue.EnqueueFromSocketThread(first).has_value(), "first queue enqueue should succeed");
    const auto duplicate = queue.EnqueueFromSocketThread(first);
    Require(duplicate.has_value(), "duplicate request should be rejected");
    RequireContains(duplicate->error, "duplicate native bridge request id", "duplicate rejection message drifted");

    const auto full = queue.EnqueueFromSocketThread(second);
    Require(full.has_value(), "full queue should reject second request");
    RequireContains(full->error, "native bridge CAD request queue is full", "full queue rejection message drifted");
    Require(queue.PendingCountForDiagnostics() == 1, "pending count should be one");
    Require(queue.InFlightCountForDiagnostics() == 1, "in-flight count should be one");

    const auto dequeued = queue.TryDequeueOnVectorworksMainContext();
    Require(dequeued.has_value() && dequeued->id == "r1", "main context dequeue failed");
    Require(queue.CompleteFromVectorworksMainContext(ResponseEnvelope{"r1", true, R"({"layers":[]})", ""}), "completion should succeed");
    const auto response = queue.WaitForResponseOnSocketThread("r1", std::chrono::milliseconds(1));
    Require(response.success, "completed queue response should be successful");
    Require(queue.InFlightCountForDiagnostics() == 0, "completed request should be removed");
    Require(!queue.CompleteFromVectorworksMainContext(ResponseEnvelope{"missing", true, R"({})", ""}), "unknown completion should fail");

    Require(!queue.EnqueueFromSocketThread(second).has_value(), "second enqueue after drain should succeed");
    queue.CancelAll("native bridge test cancellation");
    const auto cancelled = queue.WaitForResponseOnSocketThread("r2", std::chrono::milliseconds(1));
    Require(!cancelled.success, "cancelled response should fail");
    RequireContains(cancelled.error, "native bridge test cancellation", "cancel reason should propagate");
}

void TestPhaseZeroDispatch() {
    OnPluginLoadStartTransport();
    Require(!StopRequested(), "load should reset stop request");

    const auto ping = DispatchFromSocketWorker(RequestEnvelope{"p1", "ping", "{}"});
    Require(ping.success, "phase-0 ping should succeed");
    RequireContains(ping.resultJson, "native_sdk_bridge_scaffold", "phase-0 ping result drifted");

    const auto cad = DispatchFromSocketWorker(RequestEnvelope{"c1", "get_layers", "{}"});
    Require(!cad.success, "phase-0 CAD request should fail immediately");
    RequireContains(cad.error, "native bridge phase 0 CAD handlers are not implemented", "phase-0 CAD rejection message drifted");

    const auto unknown = DispatchFromSocketWorker(RequestEnvelope{"u1", "missing", "{}"});
    Require(!unknown.success, "unknown action should fail");
    RequireContains(unknown.error, "unknown native bridge action", "unknown action message drifted");

    const auto stop = DispatchFromSocketWorker(RequestEnvelope{"s1", "stop", "{}"});
    Require(stop.success, "stop should succeed");
    Require(StopRequested(), "stop should set stop request");
    OnPluginUnloadStopTransport();
}

int main() {
    TestProtocol();
    TestDispatcherMetadata();
    TestQueue();
    TestPhaseZeroDispatch();
    std::cout << "OK: native bridge scaffold compile smoke passed\n";
    return 0;
}
