#pragma once

#include "BridgeProtocol.hpp"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

namespace VectorworksMCP {

constexpr std::size_t kDefaultMaxPendingCadRequests = 128u;

struct QueuedCadRequest {
    Protocol::RequestEnvelope request;
    bool completed = false;
    Protocol::ResponseEnvelope response;
};

class CadRequestQueue {
public:
    explicit CadRequestQueue(std::size_t maxPendingRequests = kDefaultMaxPendingCadRequests)
        : maxPendingRequests_(maxPendingRequests == 0u ? kDefaultMaxPendingCadRequests : maxPendingRequests) {}

    // Socket worker thread must not call Vectorworks document APIs directly.
    // It only hands work to the main/plugin event context.
    std::optional<Protocol::ResponseEnvelope> EnqueueFromSocketThread(const Protocol::RequestEnvelope& request) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (cancelled_) {
                return Protocol::ResponseEnvelope{request.id, false, "", cancellationReason_};
            }
            if (request.id.empty()) {
                return Protocol::ResponseEnvelope{"", false, "", "native bridge request id is required"};
            }
            if (requests_.find(request.id) != requests_.end()) {
                return Protocol::ResponseEnvelope{request.id, false, "", "duplicate native bridge request id: " + request.id};
            }
            if (requests_.size() >= maxPendingRequests_) {
                return Protocol::ResponseEnvelope{request.id, false, "", "native bridge CAD request queue is full"};
            }
            requests_.emplace(request.id, QueuedCadRequest{request, false, {}});
            pending_.push_back(request.id);
        }
        cv_.notify_all();
        return std::nullopt;
    }

    // Called from the Vectorworks SDK main/plugin event context.
    std::optional<Protocol::RequestEnvelope> TryDequeueOnVectorworksMainContext() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (cancelled_) {
            return std::nullopt;
        }
        while (!pending_.empty()) {
            const auto id = pending_.front();
            pending_.pop_front();
            const auto found = requests_.find(id);
            if (found != requests_.end()) {
                return found->second.request;
            }
        }
        return std::nullopt;
    }

    // Called from the Vectorworks SDK main/plugin event context after CAD/API work.
    bool CompleteFromVectorworksMainContext(const Protocol::ResponseEnvelope& response) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto found = requests_.find(response.id);
            if (found == requests_.end()) {
                return false;
            }
            found->second.response = response;
            found->second.completed = true;
        }
        cv_.notify_all();
        return true;
    }

    // Called by the socket worker thread while waiting for main-context CAD/API work.
    Protocol::ResponseEnvelope WaitForResponseOnSocketThread(
        const std::string& id,
        std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        const bool ready = cv_.wait_for(lock, timeout, [&] {
            const auto found = requests_.find(id);
            return cancelled_ || (found != requests_.end() && found->second.completed);
        });

        const auto found = requests_.find(id);
        if (found != requests_.end() && found->second.completed) {
            auto response = found->second.response;
            requests_.erase(found);
            return response;
        }

        if (found != requests_.end()) {
            requests_.erase(found);
        }
        pending_.erase(std::remove(pending_.begin(), pending_.end(), id), pending_.end());

        if (ready && cancelled_) {
            return {id, false, "", cancellationReason_};
        }
        return {id, false, "", "native bridge timed out waiting for Vectorworks main/plugin context"};
    }

    // Called by stop/unload paths to release socket worker waiters.
    void CancelAll(const std::string& reason) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            cancelled_ = true;
            cancellationReason_ = reason.empty() ? "native bridge request queue cancelled" : reason;
            pending_.clear();
            for (auto& entry : requests_) {
                auto& record = entry.second;
                if (!record.completed) {
                    record.response = {entry.first, false, "", cancellationReason_};
                    record.completed = true;
                }
            }
        }
        cv_.notify_all();
    }

    void ResetCancellation() {
        std::lock_guard<std::mutex> lock(mutex_);
        cancelled_ = false;
        cancellationReason_.clear();
    }

    std::size_t PendingCountForDiagnostics() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return pending_.size();
    }

    std::size_t InFlightCountForDiagnostics() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return requests_.size();
    }

private:
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<std::string> pending_;
    std::unordered_map<std::string, QueuedCadRequest> requests_;
    std::size_t maxPendingRequests_;
    bool cancelled_ = false;
    std::string cancellationReason_;
};

}  // namespace VectorworksMCP
