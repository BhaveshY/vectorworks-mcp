#include "NativeTransport.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <exception>
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
    if (socket != kInvalidSocket) {
        closesocket(socket);
    }
}

void ShutdownSocket(SocketHandle socket) {
    if (socket != kInvalidSocket) {
        shutdown(socket, SD_BOTH);
    }
}

int LastSocketError() {
    return WSAGetLastError();
}

bool IsInterruptedAcceptError(int error) {
    return error == WSAEINTR || error == WSAENOTSOCK || error == WSAEINVAL;
}
#else
using SocketHandle = int;
constexpr SocketHandle kInvalidSocket = -1;

void EnsureSocketRuntime() {}

void CloseSocket(SocketHandle socket) {
    if (socket != kInvalidSocket) {
        close(socket);
    }
}

void ShutdownSocket(SocketHandle socket) {
    if (socket != kInvalidSocket) {
        shutdown(socket, SHUT_RDWR);
    }
}

int LastSocketError() {
    return errno;
}

bool IsInterruptedAcceptError(int error) {
    return error == EBADF || error == EINVAL || error == EINTR;
}
#endif

class SocketOwner {
public:
    explicit SocketOwner(SocketHandle socket = kInvalidSocket) : socket_(socket) {}
    ~SocketOwner() {
        CloseSocket(socket_);
    }

    SocketOwner(const SocketOwner&) = delete;
    SocketOwner& operator=(const SocketOwner&) = delete;

    SocketOwner(SocketOwner&& other) noexcept : socket_(other.socket_) {
        other.socket_ = kInvalidSocket;
    }

    SocketOwner& operator=(SocketOwner&& other) noexcept {
        if (this != &other) {
            CloseSocket(socket_);
            socket_ = other.socket_;
            other.socket_ = kInvalidSocket;
        }
        return *this;
    }

    SocketHandle Get() const {
        return socket_;
    }

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
        if (received <= 0) {
            return false;
        }
        offset += static_cast<std::size_t>(received);
    }
    return true;
}

bool WriteExact(SocketHandle socket, const char* buffer, std::size_t size) {
    std::size_t offset = 0;
    while (offset < size) {
        const auto remaining = static_cast<int>(size - offset);
        const int sent = send(socket, buffer + offset, remaining, 0);
        if (sent <= 0) {
            return false;
        }
        offset += static_cast<std::size_t>(sent);
    }
    return true;
}

bool ReadFrame(SocketHandle socket, std::string& payload) {
    std::array<std::uint8_t, Protocol::kFrameHeaderBytes> header{};
    if (!ReadExact(socket, reinterpret_cast<char*>(header.data()), header.size())) {
        return false;
    }
    const auto payloadSize = Protocol::DecodeFrameHeader(header);
    payload.assign(payloadSize, '\0');
    return ReadExact(socket, payload.data(), payload.size());
}

bool WriteFrame(SocketHandle socket, const std::string& payload) {
    const auto header = Protocol::EncodeFrameHeader(static_cast<std::uint32_t>(payload.size()));
    return WriteExact(socket, reinterpret_cast<const char*>(header.data()), header.size()) &&
        WriteExact(socket, payload.data(), payload.size());
}

Protocol::ResponseEnvelope ErrorResponse(std::string id, const std::exception& error) {
    if (id.empty()) {
        id = "native-transport-error";
    }
    return {std::move(id), false, "", error.what()};
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
        Stop();
    }

    void Start(const NativeTransportOptions& options, Dispatcher dispatcher) {
        if (!dispatcher) {
            throw std::invalid_argument("native transport dispatcher is required");
        }

        std::lock_guard<std::mutex> lock(mutex_);
        if (running_) {
            throw std::runtime_error("native transport is already running");
        }

        EnsureSocketRuntime();
        SocketOwner listener(socket(AF_INET, SOCK_STREAM, IPPROTO_TCP));
        if (listener.Get() == kInvalidSocket) {
            throw std::runtime_error("native transport could not create listener socket");
        }

        int enabled = 1;
        setsockopt(listener.Get(), SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&enabled), sizeof(enabled));

        const auto address = MakeLoopbackAddress(options.host, options.port);
        if (bind(listener.Get(), reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
            throw std::runtime_error("native transport could not bind loopback port");
        }
        if (listen(listener.Get(), SOMAXCONN) != 0) {
            throw std::runtime_error("native transport could not listen on loopback port");
        }

        dispatcher_ = std::move(dispatcher);
        stopRequested_.store(false);
        listenSocket_.store(listener.Release());
        boundPort_ = BoundPort(listenSocket_.load());
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
            for (const auto client : clientSockets_) {
                clients.push_back(client);
            }
        }
        ShutdownSocket(listener);
        CloseSocket(listener);
        for (const auto client : clients) {
            ShutdownSocket(client);
        }
    }

    void Stop() {
        RequestStop();
        if (listenerThread_.joinable()) {
            listenerThread_.join();
        }

        std::vector<std::thread> clients;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            clients.swap(clientThreads_);
            running_ = false;
            listenSocket_.store(kInvalidSocket);
            clientSockets_.clear();
        }
        for (auto& client : clients) {
            if (client.joinable()) {
                client.join();
            }
        }
    }

    bool IsRunning() const {
        return running_;
    }

    std::uint16_t Port() const {
        return boundPort_;
    }

    std::string LastError() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return lastError_;
    }

private:
    void AcceptLoop() {
        while (!stopRequested_.load()) {
            const auto listener = listenSocket_.load();
            if (listener == kInvalidSocket) {
                break;
            }
            const auto client = accept(listener, nullptr, nullptr);
            if (client == kInvalidSocket) {
                const int error = LastSocketError();
                if (stopRequested_.load() || IsInterruptedAcceptError(error)) {
                    break;
                }
                SetLastError("native transport accept failed");
                continue;
            }

            std::lock_guard<std::mutex> lock(mutex_);
            clientSockets_.push_back(client);
            clientThreads_.emplace_back([this, client] { HandleClient(client); });
        }

        std::lock_guard<std::mutex> lock(mutex_);
        running_ = false;
        listenSocket_.store(kInvalidSocket);
    }

    void HandleClient(SocketHandle client) {
        SocketOwner clientOwner(client);
        {
            std::string payload;
            while (!stopRequested_.load()) {
                bool shouldStop = false;
                bool frameWasRead = false;
                try {
                    if (!ReadFrame(client, payload)) {
                        break;
                    }
                    frameWasRead = true;
                    const auto request = Protocol::ParseRequestEnvelope(payload);
                    auto response = dispatcher_(request);
                    shouldStop = request.action == "stop";
                    if (!WriteFrame(client, Protocol::SerializeResponseEnvelope(response))) {
                        break;
                    }
                } catch (const std::exception& error) {
                    auto response = ErrorResponse("", error);
                    if (!WriteFrame(client, Protocol::SerializeResponseEnvelope(response))) {
                        break;
                    }
                    if (!frameWasRead) {
                        break;
                    }
                }
                if (shouldStop) {
                    RequestStop();
                    break;
                }
            }
        }
        RemoveClient(client);
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
    std::vector<std::thread> clientThreads_;
    std::thread listenerThread_;
    std::atomic_bool stopRequested_{true};
    std::atomic_bool running_{false};
    std::uint16_t boundPort_ = 0;
    std::string lastError_;
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

}  // namespace VectorworksMCP
