#pragma once

#include "BridgeProtocol.hpp"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

namespace VectorworksMCP {

struct QueuedCadRequest {
    Protocol::RequestEnvelope request;
    bool completed = false;
    Protocol::ResponseEnvelope response;
};

class CadRequestQueue {
public:
    // Socket worker thread must not call Vectorworks document APIs directly.
    // It only hands work to the main/plugin event context.
    void EnqueueFromSocketThread(const Protocol::RequestEnvelope& request) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto& record = requests_[request.id];
            record.request = request;
            if (cancelled_) {
                record.response = {request.id, false, "", cancellationReason_};
                record.completed = true;
            } else {
                pending_.push_back(request.id);
            }
        }
        cv_.notify_all();
    }

    // Called from the Vectorworks SDK main/plugin event context.
    std::optional<Protocol::RequestEnvelope> TryDequeueOnVectorworksMainContext() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (cancelled_) {
            return std::nullopt;
        }
        if (pending_.empty()) {
            return std::nullopt;
        }
        const auto id = pending_.front();
        pending_.pop_front();
        return requests_[id].request;
    }

    // Called from the Vectorworks SDK main/plugin event context after CAD/API work.
    void CompleteFromVectorworksMainContext(const Protocol::ResponseEnvelope& response) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto& record = requests_[response.id];
            record.response = response;
            record.completed = true;
        }
        cv_.notify_all();
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

private:
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<std::string> pending_;
    std::unordered_map<std::string, QueuedCadRequest> requests_;
    bool cancelled_ = false;
    std::string cancellationReason_;
};

}  // namespace VectorworksMCP
