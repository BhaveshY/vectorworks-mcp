#pragma once

#include "BridgeProtocol.hpp"

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
            pending_.push_back(request.id);
            requests_[request.id].request = request;
        }
        cv_.notify_all();
    }

    // Called from the Vectorworks SDK main/plugin event context.
    std::optional<Protocol::RequestEnvelope> TryDequeueOnVectorworksMainContext() {
        std::lock_guard<std::mutex> lock(mutex_);
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
    Protocol::ResponseEnvelope WaitForResponseOnSocketThread(const std::string& id) {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [&] {
            const auto found = requests_.find(id);
            return found != requests_.end() && found->second.completed;
        });
        auto response = requests_[id].response;
        requests_.erase(id);
        return response;
    }

private:
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<std::string> pending_;
    std::unordered_map<std::string, QueuedCadRequest> requests_;
};

}  // namespace VectorworksMCP
