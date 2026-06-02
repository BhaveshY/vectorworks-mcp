#include "BridgeDispatcher.hpp"
#include "BridgeProtocol.hpp"
#include "CadRequestQueue.hpp"
#include "NativeTransport.hpp"

#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace VectorworksMCP {
void OnPluginLoadStartTransport();
void OnPluginUnloadStopTransport();
Protocol::ResponseEnvelope DispatchFromSocketWorker(const Protocol::RequestEnvelope& request);
bool StopRequested();
std::uint16_t NativeTransportPortForDiagnostics();
}  // namespace VectorworksMCP

using VectorworksMCP::CadRequestQueue;
using VectorworksMCP::FindActionSpec;
using VectorworksMCP::NativeTransport;
using VectorworksMCP::NativeTransportOptions;
using VectorworksMCP::NativeTransportPortForDiagnostics;
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

#ifdef _WIN32
using TestSocket = SOCKET;
constexpr TestSocket kInvalidTestSocket = INVALID_SOCKET;

void CloseTestSocket(TestSocket socket) {
    if (socket != kInvalidTestSocket) {
        closesocket(socket);
    }
}

void SetEnv(const char* name, const char* value) {
    _putenv_s(name, value);
}
#else
using TestSocket = int;
constexpr TestSocket kInvalidTestSocket = -1;

void CloseTestSocket(TestSocket socket) {
    if (socket != kInvalidTestSocket) {
        close(socket);
    }
}

void SetEnv(const char* name, const char* value) {
    setenv(name, value, 1);
}
#endif

class TestSocketOwner {
public:
    explicit TestSocketOwner(TestSocket socket = kInvalidTestSocket) : socket_(socket) {}
    ~TestSocketOwner() {
        CloseTestSocket(socket_);
    }

    TestSocketOwner(const TestSocketOwner&) = delete;
    TestSocketOwner& operator=(const TestSocketOwner&) = delete;

    TestSocket Get() const {
        return socket_;
    }

private:
    TestSocket socket_;
};

bool TestReadExact(TestSocket socket, char* buffer, std::size_t size) {
    std::size_t offset = 0;
    while (offset < size) {
        const int received = recv(socket, buffer + offset, static_cast<int>(size - offset), 0);
        if (received <= 0) {
            return false;
        }
        offset += static_cast<std::size_t>(received);
    }
    return true;
}

bool TestWriteExact(TestSocket socket, const char* buffer, std::size_t size) {
    std::size_t offset = 0;
    while (offset < size) {
        const int sent = send(socket, buffer + offset, static_cast<int>(size - offset), 0);
        if (sent <= 0) {
            return false;
        }
        offset += static_cast<std::size_t>(sent);
    }
    return true;
}

TestSocketOwner ConnectToNativeTransport(std::uint16_t port) {
    TestSocket socketHandle = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    Require(socketHandle != kInvalidTestSocket, "test client socket creation failed");

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    Require(inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) == 1, "test client address parse failed");
    Require(connect(socketHandle, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0, "test client connect failed");
    return TestSocketOwner(socketHandle);
}

void SendClientFrame(TestSocket socket, const std::string& payload) {
    const auto header = EncodeFrameHeader(static_cast<std::uint32_t>(payload.size()));
    Require(TestWriteExact(socket, reinterpret_cast<const char*>(header.data()), header.size()), "client frame header write failed");
    Require(TestWriteExact(socket, payload.data(), payload.size()), "client frame payload write failed");
}

void SendClientFrameHeader(TestSocket socket, std::uint32_t payloadSize) {
    const std::array<std::uint8_t, VectorworksMCP::Protocol::kFrameHeaderBytes> header{
        static_cast<std::uint8_t>((payloadSize >> 24) & 0xFFu),
        static_cast<std::uint8_t>((payloadSize >> 16) & 0xFFu),
        static_cast<std::uint8_t>((payloadSize >> 8) & 0xFFu),
        static_cast<std::uint8_t>(payloadSize & 0xFFu),
    };
    Require(TestWriteExact(socket, reinterpret_cast<const char*>(header.data()), header.size()), "client frame header write failed");
}

std::string ReadClientFrame(TestSocket socket) {
    std::array<std::uint8_t, VectorworksMCP::Protocol::kFrameHeaderBytes> header{};
    Require(TestReadExact(socket, reinterpret_cast<char*>(header.data()), header.size()), "client frame header read failed");
    const auto payloadSize = DecodeFrameHeader(header);
    std::string payload(payloadSize, '\0');
    Require(TestReadExact(socket, payload.data(), payload.size()), "client frame payload read failed");
    return payload;
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
    SetEnv("VW_MCP_HOST", "127.0.0.1");
    SetEnv("VW_MCP_PORT", "0");
    OnPluginLoadStartTransport();
    Require(!StopRequested(), "load should reset stop request");
    Require(NativeTransportPortForDiagnostics() > 0, "plugin load should start native transport on a real port");

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

void TestNativeTransportRoundTrip() {
    NativeTransport transport;
    NativeTransportOptions options;
    options.host = "127.0.0.1";
    options.port = 0;
    transport.Start(options, DispatchFromSocketWorker);
    const auto port = transport.Port();
    Require(port > 0, "native transport should expose its bound port");

    auto client = ConnectToNativeTransport(port);
    SendClientFrame(client.Get(), R"({"id":"tcp-ping","action":"ping","params":{}})");
    const auto ping = ReadClientFrame(client.Get());
    RequireContains(ping, R"("id":"tcp-ping")", "transport ping response id drifted");
    RequireContains(ping, R"("success":true)", "transport ping should succeed");
    RequireContains(ping, "native_sdk_bridge_scaffold", "transport ping result should identify native bridge");

    SendClientFrame(client.Get(), R"({"id":"tcp-stop","action":"stop","params":{}})");
    const auto stop = ReadClientFrame(client.Get());
    RequireContains(stop, R"("id":"tcp-stop")", "transport stop response id drifted");
    RequireContains(stop, R"("success":true)", "transport stop should succeed");
    transport.Stop();
    Require(!transport.IsRunning(), "native transport should stop after stop request");

    NativeTransport restart;
    restart.Start(options, DispatchFromSocketWorker);
    Require(restart.Port() > 0, "native transport should restart cleanly after stop");
    restart.Stop();

    NativeTransport malformed;
    malformed.Start(options, DispatchFromSocketWorker);
    auto malformedClient = ConnectToNativeTransport(malformed.Port());
    SendClientFrameHeader(malformedClient.Get(), VectorworksMCP::Protocol::kMaxFrameBytes + 1u);
    const auto malformedResponse = ReadClientFrame(malformedClient.Get());
    RequireContains(malformedResponse, R"("id":"native-transport-error")", "malformed frame should get transport error id");
    RequireContains(malformedResponse, R"("success":false)", "malformed frame should fail without killing transport");
    RequireContains(malformedResponse, "frame size is outside", "malformed frame error should describe frame size");

    auto healthyClient = ConnectToNativeTransport(malformed.Port());
    SendClientFrame(healthyClient.Get(), R"({"id":"after-bad-frame","action":"ping","params":{}})");
    const auto healthyPing = ReadClientFrame(healthyClient.Get());
    RequireContains(healthyPing, R"("id":"after-bad-frame")", "transport should accept clients after malformed frame");
    RequireContains(healthyPing, R"("success":true)", "transport ping after malformed frame should succeed");
    malformed.Stop();
}

int main() {
    TestProtocol();
    TestDispatcherMetadata();
    TestQueue();
    TestPhaseZeroDispatch();
    TestNativeTransportRoundTrip();
    std::cout << "OK: native bridge scaffold compile smoke passed\n";
    return 0;
}
