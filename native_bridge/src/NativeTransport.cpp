#include "StdAfx.h"

#include "NativeTransport.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <exception>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <mstcpip.h>
#else
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace VectorworksMCP {

namespace {

#ifdef _WIN32
using SocketHandle = SOCKET;
constexpr SocketHandle kInvalidSocket = INVALID_SOCKET;

void EnsureSocketRuntime() {
    static std::once_flag once;
    static int result = 0;
    std::call_once(once, [] {
        WSADATA data;
        result = WSAStartup(MAKEWORD(2, 2), &data);
    });
    if (result != 0) {
        throw std::runtime_error("WSAStartup failed for native Vectorworks MCP transport");
    }
}

void CloseSocket(SocketHandle socket) {
    if (socket != kInvalidSocket) closesocket(socket);
}

void ShutdownSocket(SocketHandle socket) {
    if (socket != kInvalidSocket) shutdown(socket, SD_BOTH);
}

int LastSocketError() {
    return WSAGetLastError();
}

bool IsInterruptedAcceptError(int error) {
    return error == WSAEINTR || error == WSAENOTSOCK || error == WSAEINVAL;
}

bool IsAddressBusyError(int error) {
    return error == WSAEADDRINUSE || error == WSAEACCES;
}

void SetSocketTimeouts(SocketHandle socket, int receiveSeconds, int sendSeconds) {
    const DWORD receiveMs = static_cast<DWORD>(receiveSeconds) * 1000u;
    const DWORD sendMs = static_cast<DWORD>(sendSeconds) * 1000u;
    setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&receiveMs), sizeof(receiveMs));
    setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&sendMs), sizeof(sendMs));
}

void EnableKeepAlive(SocketHandle socket) {
    BOOL enabled = TRUE;
    setsockopt(socket, SOL_SOCKET, SO_KEEPALIVE, reinterpret_cast<const char*>(&enabled), sizeof(enabled));
    tcp_keepalive settings{};
    settings.onoff = 1;
    settings.keepalivetime = 30000;
    settings.keepaliveinterval = 5000;
    DWORD returned = 0;
    WSAIoctl(socket, SIO_KEEPALIVE_VALS, &settings, sizeof(settings), nullptr, 0, &returned, nullptr, nullptr);
    BOOL noDelay = TRUE;
    setsockopt(socket, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&noDelay), sizeof(noDelay));
}

void SetExclusiveAddressUse(SocketHandle socket) {
    BOOL enabled = TRUE;
    setsockopt(socket, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, reinterpret_cast<const char*>(&enabled), sizeof(enabled));
}
#else
using SocketHandle = int;
constexpr SocketHandle kInvalidSocket = -1;

void EnsureSocketRuntime() {}

void CloseSocket(SocketHandle socket) {
    if (socket != kInvalidSocket) close(socket);
}

void ShutdownSocket(SocketHandle socket) {
    if (socket != kInvalidSocket) shutdown(socket, SHUT_RDWR);
}

int LastSocketError() {
    return errno;
}

bool IsInterruptedAcceptError(int error) {
    return error == EBADF || error == EINVAL || error == EINTR;
}

bool IsAddressBusyError(int error) {
    return error == EADDRINUSE || error == EACCES;
}

void SetSocketTimeouts(SocketHandle socket, int receiveSeconds, int sendSeconds) {
    timeval receive{};
    receive.tv_sec = receiveSeconds;
    timeval send{};
    send.tv_sec = sendSeconds;
    setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &receive, sizeof(receive));
    setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, &send, sizeof(send));
}

void EnableKeepAlive(SocketHandle socket) {
    int enabled = 1;
    setsockopt(socket, SOL_SOCKET, SO_KEEPALIVE, &enabled, sizeof(enabled));
}

void SetExclusiveAddressUse(SocketHandle) {}
#endif

class SocketOwner {
public:
    explicit SocketOwner(SocketHandle socket = kInvalidSocket) : socket_(socket) {}
    ~SocketOwner() { CloseSocket(socket_); }
    SocketOwner(const SocketOwner&) = delete;
    SocketOwner& operator=(const SocketOwner&) = delete;
    SocketHandle Get() const { return socket_; }
    SocketHandle Release() {
        const auto socket = socket_;
        socket_ = kInvalidSocket;
        return socket;
    }

private:
    SocketHandle socket_;
};

bool ReadExact(SocketHandle socket, char* buffer, std::size_t size) {
    std::size_t offset = 0;
    while (offset < size) {
        const auto remaining = static_cast<int>(size - offset);
        const int received = recv(socket, buffer + offset, remaining, 0);
        if (received <= 0) return false;
        offset += static_cast<std::size_t>(received);
    }
    return true;
}

bool WriteExact(SocketHandle socket, const char* buffer, std::size_t size) {
    std::size_t offset = 0;
    while (offset < size) {
        const auto remaining = static_cast<int>(size - offset);
        const int sent = send(socket, buffer + offset, remaining, 0);
        if (sent <= 0) return false;
        offset += static_cast<std::size_t>(sent);
    }
    return true;
}

bool WriteFrame(SocketHandle socket, const std::string& payload) {
    const auto header = Protocol::EncodeFrameHeader(static_cast<std::uint32_t>(payload.size()));
    return WriteExact(socket, reinterpret_cast<const char*>(header.data()), header.size()) &&
        WriteExact(socket, payload.data(), payload.size());
}

// Serialises a response, replacing it with a structured error when it would
// exceed the 16 MiB frame limit (M8) or cannot be serialised.
std::string SerializeBounded(const Protocol::ResponseEnvelope& response) {
    std::string payload;
    try {
        payload = Protocol::SerializeResponseEnvelope(response);
    } catch (const std::exception& error) {
        payload = Protocol::SerializeResponseEnvelope(Protocol::Fail(
            response.id.empty() ? "native-transport-error" : response.id,
            "internal_error",
            std::string("native bridge produced an invalid response: ") + error.what(),
            false,
            response.success ? "unknown" : response.commitState));
    }
    if (payload.size() > Protocol::kMaxFrameBytes) {
        payload = Protocol::SerializeResponseEnvelope(Protocol::Fail(
            response.id.empty() ? "native-transport-error" : response.id,
            "response_too_large",
            "native bridge response exceeded the 16 MiB frame limit; request a smaller page (limit/offset or max_bytes)",
            false,
            response.success ? "committed" : response.commitState,
            "{\"bytes\":" + std::to_string(payload.size()) + "}"));
    }
    return payload;
}

sockaddr_in MakeLoopbackAddress(const std::string& host, std::uint16_t port) {
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    if (inet_pton(AF_INET, host.c_str(), &address.sin_addr) != 1) {
        throw std::runtime_error("native transport host must be an IPv4 loopback address");
    }
    const auto loopback = ntohl(address.sin_addr.s_addr);
    if ((loopback >> 24) != 127u) {
        throw std::runtime_error("native transport refuses to bind outside 127.0.0.0/8");
    }
    return address;
}

std::uint16_t BoundPort(SocketHandle socket) {
    sockaddr_in address{};
#ifdef _WIN32
    int length = sizeof(address);
#else
    socklen_t length = sizeof(address);
#endif
    if (getsockname(socket, reinterpret_cast<sockaddr*>(&address), &length) != 0) {
        throw std::runtime_error("native transport could not inspect bound port");
    }
    return ntohs(address.sin_port);
}

}  // namespace

class NativeTransport::Impl {
public:
    ~Impl() {
        try {
            Stop();
        } catch (...) {
        }
    }

    void Start(const NativeTransportOptions& options, Dispatcher dispatcher) {
        if (!dispatcher) {
            throw std::invalid_argument("native transport dispatcher is required");
        }
        // Reap a previous run first (C3): never assign to a joinable thread.
        // Active client connections from a previous listener keep serving;
        // only finished workers and the exited listener are joined here.
        JoinThreads(false);

        std::lock_guard<std::mutex> lock(mutex_);
        if (running_) {
            throw std::runtime_error("native transport is already running");
        }
        EnsureSocketRuntime();

        SocketHandle bound = kInvalidSocket;
        std::string lastBindError;
        const std::uint16_t first = options.portFirst;
        const std::uint16_t last = options.portLast < first ? first : options.portLast;
        for (std::uint32_t port = first; port <= last && bound == kInvalidSocket; ++port) {
            SocketOwner candidate(socket(AF_INET, SOCK_STREAM, IPPROTO_TCP));
            if (candidate.Get() == kInvalidSocket) {
                throw std::runtime_error("native transport could not create listener socket");
            }
            SetExclusiveAddressUse(candidate.Get());
            const auto address = MakeLoopbackAddress(options.host, static_cast<std::uint16_t>(port));
            if (bind(candidate.Get(), reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
                const int error = LastSocketError();
                lastBindError = "port " + std::to_string(port) + " bind error " + std::to_string(error);
                if (IsAddressBusyError(error)) {
                    continue;
                }
                continue;
            }
            if (listen(candidate.Get(), SOMAXCONN) != 0) {
                lastBindError = "port " + std::to_string(port) + " listen failed";
                continue;
            }
            bound = candidate.Release();
        }
        if (bound == kInvalidSocket) {
            throw std::runtime_error(
                "native transport could not bind any loopback port in " + std::to_string(first) + "-" +
                std::to_string(last) + " (" + lastBindError + ")");
        }

        dispatcher_ = std::move(dispatcher);
        maxClients_ = options.maxClients < 1 ? 1 : options.maxClients;
        clientIdleSeconds_ = options.clientIdleSeconds < 5 ? 5 : options.clientIdleSeconds;
        stopRequested_.store(false);
        listenSocket_.store(bound);
        boundPort_ = BoundPort(bound);
        running_ = true;
        lastError_.clear();
        listenerThread_ = std::thread([this] { AcceptLoop(); });
    }

    void RequestStop() {
        stopRequested_.store(true);
        SocketHandle listener = kInvalidSocket;
        std::vector<SocketHandle> clients;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            listener = listenSocket_.exchange(kInvalidSocket);
            clients = clientSockets_;
        }
        ShutdownSocket(listener);
        CloseSocket(listener);
        for (const auto client : clients) {
            ShutdownSocket(client);
        }
    }

    void Stop() {
        RequestStop();
        JoinThreads(true);
        std::lock_guard<std::mutex> lock(mutex_);
        running_ = false;
        listenSocket_.store(kInvalidSocket);
        clientSockets_.clear();
    }

    bool IsRunning() const { return running_.load(); }
    std::uint16_t Port() const { return boundPort_.load(); }

    std::string LastError() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return lastError_;
    }

    int ClientCount() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return static_cast<int>(clientSockets_.size());
    }

private:
    struct ClientWorker {
        std::shared_ptr<std::atomic_bool> completed;
        std::thread thread;
    };

    void JoinThreads(bool includeActiveClients) {
        std::thread listener;
        std::vector<ClientWorker> clients;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (listenerThread_.joinable()) {
                listener = std::move(listenerThread_);
            }
            if (includeActiveClients) {
                clients.swap(clientThreads_);
                for (const auto client : clientSockets_) {
                    ShutdownSocket(client);
                }
            } else {
                auto worker = clientThreads_.begin();
                while (worker != clientThreads_.end()) {
                    if (worker->completed->load()) {
                        clients.push_back(std::move(*worker));
                        worker = clientThreads_.erase(worker);
                    } else {
                        ++worker;
                    }
                }
            }
        }
        if (listener.joinable()) {
            if (listener.get_id() == std::this_thread::get_id()) {
                listener.detach();
            } else {
                stopRequested_.store(true);
                const SocketHandle socket = listenSocket_.exchange(kInvalidSocket);
                ShutdownSocket(socket);
                CloseSocket(socket);
                listener.join();
            }
        }
        for (auto& client : clients) {
            if (client.thread.joinable()) {
                if (client.thread.get_id() == std::this_thread::get_id()) {
                    client.thread.detach();
                } else {
                    client.thread.join();
                }
            }
        }
    }

    void ReapCompletedClients() {
        std::vector<std::thread> finished;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto worker = clientThreads_.begin();
            while (worker != clientThreads_.end()) {
                if (worker->completed->load()) {
                    finished.push_back(std::move(worker->thread));
                    worker = clientThreads_.erase(worker);
                } else {
                    ++worker;
                }
            }
        }
        for (auto& thread : finished) {
            if (thread.joinable()) thread.join();
        }
    }

    void RejectClient(SocketHandle client, const std::string& code, const std::string& message) {
        SetSocketTimeouts(client, 1, 1);
        try {
            WriteFrame(client, Protocol::SerializeResponseEnvelope(Protocol::Fail(
                "native-transport-rejected", code, message, true, "not_started")));
        } catch (...) {
        }
        ShutdownSocket(client);
        CloseSocket(client);
    }

    void AcceptLoop() {
        int consecutiveErrors = 0;
        while (!stopRequested_.load()) {
            try {
                ReapCompletedClients();
            } catch (...) {
            }
            const auto listener = listenSocket_.load();
            if (listener == kInvalidSocket) break;
            const auto client = accept(listener, nullptr, nullptr);
            if (client == kInvalidSocket) {
                const int error = LastSocketError();
                if (stopRequested_.load() || IsInterruptedAcceptError(error)) break;
                SetLastError("native transport accept failed with error " + std::to_string(error));
                ++consecutiveErrors;
                if (consecutiveErrors > 50) {
                    // Give up this listener; the main-thread pump restarts the
                    // transport with back-off (L2: no busy spin).
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(std::min(1000, 50 * consecutiveErrors)));
                continue;
            }
            consecutiveErrors = 0;

            bool reject = false;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                reject = static_cast<int>(clientSockets_.size()) >= maxClients_;
            }
            if (reject) {
                SetLastError("native transport rejected a client because the client cap was reached");
                RejectClient(client, "too_many_clients",
                    "native bridge already serves the maximum number of clients (" + std::to_string(maxClients_) +
                    "); close idle connections and retry");
                continue;
            }

            EnableKeepAlive(client);
            SetSocketTimeouts(client, clientIdleSeconds_, 60);
            std::lock_guard<std::mutex> lock(mutex_);
            bool socketRegistered = false;
            bool workerRegistered = false;
            try {
                auto completed = std::make_shared<std::atomic_bool>(false);
                clientSockets_.push_back(client);
                socketRegistered = true;
                clientThreads_.push_back({completed, std::thread{}});
                workerRegistered = true;
                clientThreads_.back().thread = std::thread([this, client, completed] {
                    try {
                        HandleClient(client);
                    } catch (...) {
                        SetLastError("native transport client failed unexpectedly");
                    }
                    RemoveClient(client);
                    CloseSocket(client);
                    completed->store(true);
                });
            } catch (...) {
                if (workerRegistered) clientThreads_.pop_back();
                if (socketRegistered) clientSockets_.pop_back();
                CloseSocket(client);
                lastError_ = "native transport could not start a client worker";
            }
        }

        std::lock_guard<std::mutex> lock(mutex_);
        running_ = false;
        const SocketHandle socket = listenSocket_.exchange(kInvalidSocket);
        CloseSocket(socket);
    }

    void HandleClient(SocketHandle client) {
        std::string payload;
        while (!stopRequested_.load()) {
            std::array<std::uint8_t, Protocol::kFrameHeaderBytes> header{};
            if (!ReadExact(client, reinterpret_cast<char*>(header.data()), header.size())) {
                break;  // closed, reset or idle timeout
            }
            std::uint32_t size = 0;
            try {
                size = Protocol::DecodeFrameHeader(header);
            } catch (const std::exception& error) {
                // The stream cannot be resynchronised after a bad header.
                try {
                    WriteFrame(client, Protocol::SerializeResponseEnvelope(Protocol::Fail(
                        "native-transport-error", "invalid_frame", error.what(), false, "not_started")));
                } catch (...) {
                }
                break;
            }
            payload.assign(size, '\0');
            if (!ReadExact(client, payload.data(), payload.size())) {
                break;
            }

            DispatchOutcome outcome;
            try {
                const auto request = Protocol::ParseRequestEnvelope(payload);
                outcome = dispatcher_(request);
                if (outcome.response.id.empty()) outcome.response.id = request.id;
            } catch (const Protocol::BridgeError& error) {
                outcome.response = Protocol::FromBridgeError("native-transport-error", error);
            } catch (const std::exception& error) {
                outcome.response = Protocol::Fail(
                    "native-transport-error", "invalid_request", error.what(), false, "not_started");
            } catch (...) {
                outcome.response = Protocol::Fail(
                    "native-transport-error", "internal_error", "native bridge dispatcher failed", false, "unknown");
            }
            if (!WriteFrame(client, SerializeBounded(outcome.response))) {
                break;
            }
            if (outcome.closeConnection) {
                break;
            }
        }
        ShutdownSocket(client);
    }

    void RemoveClient(SocketHandle client) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = clientSockets_.begin();
        while (it != clientSockets_.end()) {
            if (*it == client) {
                it = clientSockets_.erase(it);
            } else {
                ++it;
            }
        }
    }

    void SetLastError(std::string message) {
        std::lock_guard<std::mutex> lock(mutex_);
        lastError_ = std::move(message);
    }

    mutable std::mutex mutex_;
    Dispatcher dispatcher_;
    std::atomic<SocketHandle> listenSocket_{kInvalidSocket};
    std::vector<SocketHandle> clientSockets_;
    std::vector<ClientWorker> clientThreads_;
    std::thread listenerThread_;
    std::atomic_bool stopRequested_{true};
    std::atomic_bool running_{false};
    std::atomic<std::uint16_t> boundPort_{0};
    std::string lastError_;
    int maxClients_ = 16;
    int clientIdleSeconds_ = 120;
};

NativeTransport::NativeTransport() : impl_(new Impl()) {}

NativeTransport::~NativeTransport() {
    delete impl_;
}

void NativeTransport::Start(const NativeTransportOptions& options, Dispatcher dispatcher) {
    impl_->Start(options, std::move(dispatcher));
}

void NativeTransport::RequestStop() {
    impl_->RequestStop();
}

void NativeTransport::Stop() {
    impl_->Stop();
}

bool NativeTransport::IsRunning() const {
    return impl_->IsRunning();
}

std::uint16_t NativeTransport::Port() const {
    return impl_->Port();
}

std::string NativeTransport::LastError() const {
    return impl_->LastError();
}

int NativeTransport::ClientCount() const {
    return impl_->ClientCount();
}

}  // namespace VectorworksMCP
