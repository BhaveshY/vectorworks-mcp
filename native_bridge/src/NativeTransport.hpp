#pragma once

#include "BridgeProtocol.hpp"

#include <cstdint>
#include <functional>
#include <string>

namespace VectorworksMCP {

struct NativeTransportOptions {
    std::string host = "127.0.0.1";
    // Ports are tried in order portFirst..portLast (inclusive). VW_MCP_PORT
    // pins a single port (portFirst == portLast).
    std::uint16_t portFirst = 9877;
    std::uint16_t portLast = 9886;
    int maxClients = 16;
    int clientIdleSeconds = 120;
};

struct DispatchOutcome {
    Protocol::ResponseEnvelope response;
    // True when the connection must be closed after the response is written
    // (the v2 `stop` action closes only the calling connection).
    bool closeConnection = false;
};

class NativeTransport {
public:
    using Dispatcher = std::function<DispatchOutcome(const Protocol::RequestEnvelope&)>;

    NativeTransport();
    ~NativeTransport();

    NativeTransport(const NativeTransport&) = delete;
    NativeTransport& operator=(const NativeTransport&) = delete;

    // Binds with SO_EXCLUSIVEADDRUSE on the first free port of the range.
    // Joins/reaps any previous listener and client threads first, so it is
    // safe to call again after the accept loop exited.
    void Start(const NativeTransportOptions& options, Dispatcher dispatcher);
    void RequestStop();
    void Stop();

    bool IsRunning() const;
    std::uint16_t Port() const;
    std::string LastError() const;
    int ClientCount() const;

private:
    class Impl;
    Impl* impl_;
};

}  // namespace VectorworksMCP
