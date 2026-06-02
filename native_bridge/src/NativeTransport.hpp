#pragma once

#include "BridgeProtocol.hpp"

#include <cstdint>
#include <functional>
#include <string>

namespace VectorworksMCP {

struct NativeTransportOptions {
    std::string host = "127.0.0.1";
    std::uint16_t port = 9877;
};

class NativeTransport {
public:
    using Dispatcher = std::function<Protocol::ResponseEnvelope(const Protocol::RequestEnvelope&)>;

    NativeTransport();
    ~NativeTransport();

    NativeTransport(const NativeTransport&) = delete;
    NativeTransport& operator=(const NativeTransport&) = delete;

    void Start(const NativeTransportOptions& options, Dispatcher dispatcher);
    void RequestStop();
    void Stop();

    bool IsRunning() const;
    std::uint16_t Port() const;
    std::string LastError() const;

private:
    class Impl;
    Impl* impl_;
};

}  // namespace VectorworksMCP
