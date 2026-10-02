#include "StdAfx.h"

#include "RequestScheduler.hpp"

#include "JsonParams.hpp"

#include <algorithm>
#include <random>
#include <sstream>

namespace VectorworksMCP {

namespace {

std::int64_t ElapsedMs(
    std::chrono::steady_clock::time_point from,
    std::chrono::steady_clock::time_point to) {
    if (from == std::chrono::steady_clock::time_point{}) {
        return 0;
    }
    const auto value = std::chrono::duration_cast<std::chrono::milliseconds>(to - from).count();
    return value < 0 ? 0 : value;
}

bool IsTerminal(JobState state) {
    return state == JobState::Done || state == JobState::Failed || state == JobState::Cancelled;
}

}  // namespace

const char* UiStateName(UiState state) noexcept {
    switch (state) {
        case UiState::Idle: return "idle";
        case UiState::Modal: return "modal";
        case UiState::Tracking: return "tracking";
        case UiState::Loading: return "loading";
        case UiState::NoDocument: return "no_document";
    }
    return "idle";
}

const char* JobStateName(JobState state) noexcept {
    switch (state) {
        case JobState::Queued: return "queued";
        case JobState::Running: return "running";
        case JobState::Done: return "done";
        case JobState::Failed: return "failed";
        case JobState::Cancelled: return "cancelled";
    }
    return "failed";
}

void RequestScheduler::SetNotifier(Notifier notifier) {
    std::lock_guard<std::mutex> lock(mutex_);
    notifier_ = std::move(notifier);
}

std::string RequestScheduler::NewJobIdLocked() {
    static std::mt19937_64 generator{std::random_device{}()};
    std::ostringstream out;
    out << "job-" << ++jobCounter_ << "-" << std::hex << (generator() & 0xffffffffull);
    return out.str();
}

void RequestScheduler::PruneJobsLocked(std::chrono::steady_clock::time_point now) {
    for (auto it = jobs_.begin(); it != jobs_.end();) {
        const auto& entry = it->second;
        if (IsTerminal(entry->state) &&
            ElapsedMs(entry->finishedAt, now) > kJobRetentionMs) {
            it = jobs_.erase(it);
        } else {
            ++it;
        }
    }
    while (jobs_.size() > kMaxJobs) {
        auto oldest = jobs_.end();
        for (auto it = jobs_.begin(); it != jobs_.end(); ++it) {
            if (!IsTerminal(it->second->state)) continue;
            if (oldest == jobs_.end() || it->second->finishedAt < oldest->second->finishedAt) {
                oldest = it;
            }
        }
        if (oldest == jobs_.end()) break;
        jobs_.erase(oldest);
    }
}

Protocol::ResponseEnvelope RequestScheduler::SubmitAndWait(
    const Protocol::RequestEnvelope& request,
    const SubmitOptions& rawOptions,
    const std::function<std::string()>& uiStateProvider) {
    SubmitOptions options = rawOptions;
    if (options.timeoutMs <= 0) options.timeoutMs = kDefaultTimeoutMs;
    options.timeoutMs = std::clamp(options.timeoutMs, kMinTimeoutMs, kMaxTimeoutMs);
    options.busyWaitMs = std::clamp<std::int64_t>(options.busyWaitMs, 0, kMaxTimeoutMs);

    auto entry = std::make_shared<ScheduledRequest>();
    Notifier notifier;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto now = std::chrono::steady_clock::now();
        PruneJobsLocked(now);
        if (cancelled_) {
            return Protocol::Fail(request.id, "bridge_stopping", cancelReason_, true, "not_started");
        }
        if (inFlightIds_.count(request.id) != 0u) {
            return Protocol::Fail(
                request.id, "duplicate_request_id",
                "duplicate native bridge request id: " + request.id, false, "not_started");
        }
        if (queue_.size() >= kMaxPending) {
            return Protocol::Fail(
                request.id, "queue_full", "native bridge CAD request queue is full", true, "not_started");
        }
        if (options.async) {
            std::size_t unfinished = 0;
            for (const auto& job : jobs_) {
                if (!IsTerminal(job.second->state)) ++unfinished;
            }
            if (unfinished >= kMaxJobs) {
                return Protocol::Fail(
                    request.id, "too_many_jobs",
                    "native bridge already tracks the maximum number of unfinished jobs", true, "not_started");
            }
        }
        entry->request = request;
        entry->options = options;
        entry->state = JobState::Queued;
        entry->enqueuedAt = now;
        entry->enqueuedAtUnixMs = UnixMillisecondsNow();
        if (options.async) {
            entry->isJob = true;
            entry->jobId = NewJobIdLocked();
            jobs_[entry->jobId] = entry;
        }
        inFlightIds_.insert(request.id);
        queue_.push_back(entry);
        notifier = notifier_;
    }
    if (notifier) {
        try {
            notifier();
        } catch (...) {
        }
    }
    if (options.async) {
        return Protocol::Ok(
            request.id,
            "{\"job_id\":" + JsonString(entry->jobId) +
                ",\"state\":\"queued\",\"action\":" + JsonString(request.action) + "}");
    }

    std::unique_lock<std::mutex> lock(mutex_);
    cv_.wait_for(lock, std::chrono::milliseconds(options.timeoutMs), [&] {
        return IsTerminal(entry->state);
    });
    if (IsTerminal(entry->state)) {
        inFlightIds_.erase(request.id);
        return entry->response;
    }
    const auto now = std::chrono::steady_clock::now();
    if (entry->state == JobState::Queued) {
        queue_.erase(std::remove(queue_.begin(), queue_.end(), entry), queue_.end());
        entry->state = JobState::Cancelled;
        inFlightIds_.erase(request.id);
        lock.unlock();
        std::string uiState = uiStateProvider ? uiStateProvider() : "";
        return Protocol::Fail(
            request.id,
            "timeout_not_started",
            "the request did not start within timeout_ms; nothing ran (Vectorworks main thread busy or blocked)",
            true,
            "not_started",
            "{\"timeout_ms\":" + std::to_string(options.timeoutMs) +
                ",\"ui_state\":" + JsonString(uiState) + "}");
    }
    // Running: convert into a job so the result is never silently dropped.
    if (!entry->isJob) {
        entry->isJob = true;
        entry->jobId = NewJobIdLocked();
        jobs_[entry->jobId] = entry;
    }
    inFlightIds_.erase(request.id);
    const std::string details = "{\"job_id\":" + JsonString(entry->jobId) +
        ",\"action\":" + JsonString(request.action) +
        ",\"elapsed_ms\":" + std::to_string(ElapsedMs(entry->startedAt, now)) +
        ",\"timeout_ms\":" + std::to_string(options.timeoutMs) + "}";
    return Protocol::Fail(
        request.id,
        "still_running",
        "the request is still running on the Vectorworks main thread; poll job_status with details.job_id",
        false,
        "unknown",
        details);
}

std::string RequestScheduler::JobStatusJson(const std::string& jobId, const std::string& uiState) {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto now = std::chrono::steady_clock::now();
    PruneJobsLocked(now);
    const auto found = jobs_.find(jobId);
    if (found == jobs_.end()) {
        throw Protocol::BridgeError(
            "job_not_found", "job_id is unknown or expired (jobs are retained for 15 minutes)", false, "not_started");
    }
    const auto& entry = found->second;
    std::string json = "{\"job_id\":" + JsonString(entry->jobId);
    json += ",\"action\":" + JsonString(entry->request.action);
    json += ",\"request_id\":" + JsonString(entry->request.id);
    json += ",\"state\":" + JsonString(JobStateName(entry->state));
    json += ",\"enqueued_at\":" + JsonString(IsoUtc(entry->enqueuedAtUnixMs));
    json += ",\"started_at\":";
    json += entry->startedAtUnixMs ? JsonString(IsoUtc(entry->startedAtUnixMs)) : std::string("null");
    json += ",\"finished_at\":";
    json += entry->finishedAtUnixMs ? JsonString(IsoUtc(entry->finishedAtUnixMs)) : std::string("null");
    std::int64_t elapsed = 0;
    if (entry->state == JobState::Running) {
        elapsed = ElapsedMs(entry->startedAt, now);
    } else if (IsTerminal(entry->state) && entry->startedAt != std::chrono::steady_clock::time_point{}) {
        elapsed = ElapsedMs(entry->startedAt, entry->finishedAt);
    }
    json += ",\"elapsed_ms\":" + std::to_string(elapsed);
    json += ",\"cancellable\":";
    json += entry->state == JobState::Queued ? "true" : "false";
    json += ",\"ui_state\":" + JsonString(uiState);
    if (entry->state == JobState::Running && (uiState == "modal" || uiState == "tracking")) {
        json += ",\"blocked_by_ui\":true,\"hint_error_code\":\"vectorworks_busy\"";
    }
    if (entry->state == JobState::Done) {
        json += ",\"result\":" + (entry->response.resultJson.empty() ? std::string("null") : entry->response.resultJson);
    } else if (entry->state == JobState::Failed || entry->state == JobState::Cancelled) {
        json += ",\"error\":" + JsonString(entry->response.error.empty() ? "job failed" : entry->response.error);
        if (!entry->response.errorCode.empty()) {
            json += ",\"error_code\":" + JsonString(entry->response.errorCode);
        }
        if (!entry->response.commitState.empty()) {
            json += ",\"commit_state\":" + JsonString(entry->response.commitState);
        }
        if (entry->response.retryable >= 0) {
            json += ",\"retryable\":";
            json += entry->response.retryable ? "true" : "false";
        }
        if (!entry->response.detailsJson.empty()) {
            json += ",\"details\":" + entry->response.detailsJson;
        }
    }
    json += "}";
    return json;
}

std::string RequestScheduler::CancelJobJson(const std::string& jobId) {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = jobs_.find(jobId);
    if (found == jobs_.end()) {
        throw Protocol::BridgeError(
            "job_not_found", "job_id is unknown or expired", false, "not_started");
    }
    auto entry = found->second;
    if (entry->state == JobState::Queued) {
        queue_.erase(std::remove(queue_.begin(), queue_.end(), entry), queue_.end());
        FinishLocked(
            entry,
            Protocol::Fail(entry->request.id, "cancelled", "job was cancelled before it started", false, "not_started"),
            JobState::Cancelled);
        return "{\"job_id\":" + JsonString(jobId) + ",\"state\":\"cancelled\",\"cancelled\":true,\"cancellable\":true}";
    }
    return "{\"job_id\":" + JsonString(jobId) + ",\"state\":" + JsonString(JobStateName(entry->state)) +
        ",\"cancelled\":false,\"cancellable\":false}";
}

void RequestScheduler::FinishLocked(
    const std::shared_ptr<ScheduledRequest>& entry,
    Protocol::ResponseEnvelope response,
    JobState state) {
    entry->response = std::move(response);
    entry->state = state;
    entry->finishedAt = std::chrono::steady_clock::now();
    entry->finishedAtUnixMs = UnixMillisecondsNow();
    if (running_ == entry) {
        running_.reset();
    }
    if (entry->isJob) {
        inFlightIds_.erase(entry->request.id);
    }
    cv_.notify_all();
}

std::shared_ptr<ScheduledRequest> RequestScheduler::TryDequeue(UiState state) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (cancelled_ || running_) {
        return nullptr;
    }
    const auto now = std::chrono::steady_clock::now();
    for (auto it = queue_.begin(); it != queue_.end();) {
        auto entry = *it;
        const bool gateOpen = state == UiState::Idle ||
            (state == UiState::NoDocument && !entry->options.needsDocument);
        if (gateOpen) {
            queue_.erase(it);
            entry->state = JobState::Running;
            entry->startedAt = now;
            entry->startedAtUnixMs = UnixMillisecondsNow();
            running_ = entry;
            return entry;
        }
        if (state == UiState::NoDocument && entry->options.needsDocument) {
            it = queue_.erase(it);
            FinishLocked(
                entry,
                Protocol::Fail(
                    entry->request.id,
                    "no_active_document",
                    "no Vectorworks document is open; open or create a document first",
                    false,
                    "not_started",
                    "{\"ui_state\":\"no_document\"}"),
                JobState::Failed);
            continue;
        }
        if (ElapsedMs(entry->enqueuedAt, now) > entry->options.busyWaitMs) {
            it = queue_.erase(it);
            FinishLocked(
                entry,
                Protocol::Fail(
                    entry->request.id,
                    "vectorworks_busy",
                    std::string("Vectorworks is busy (") + UiStateName(state) +
                        "); close dialogs / finish the current tool action and retry",
                    true,
                    "not_started",
                    std::string("{\"ui_state\":") + JsonString(UiStateName(state)) +
                        ",\"busy_wait_ms\":" + std::to_string(entry->options.busyWaitMs) + "}"),
                JobState::Failed);
            continue;
        }
        ++it;
    }
    return nullptr;
}

void RequestScheduler::Complete(
    const std::shared_ptr<ScheduledRequest>& entry,
    Protocol::ResponseEnvelope response) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (IsTerminal(entry->state)) {
        if (running_ == entry) running_.reset();
        return;
    }
    const JobState state = response.success ? JobState::Done : JobState::Failed;
    FinishLocked(entry, std::move(response), state);
}

void RequestScheduler::CancelAll(const std::string& reason) {
    std::lock_guard<std::mutex> lock(mutex_);
    cancelled_ = true;
    cancelReason_ = reason.empty() ? "native bridge request queue cancelled" : reason;
    while (!queue_.empty()) {
        auto entry = queue_.front();
        queue_.pop_front();
        FinishLocked(
            entry,
            Protocol::Fail(entry->request.id, "bridge_stopping", cancelReason_, true, "not_started"),
            JobState::Cancelled);
    }
    cv_.notify_all();
}

void RequestScheduler::Reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    cancelled_ = false;
    cancelReason_.clear();
}

std::size_t RequestScheduler::QueueDepth() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return queue_.size();
}

std::int64_t RequestScheduler::RunningMilliseconds() const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!running_) return 0;
    return ElapsedMs(running_->startedAt, std::chrono::steady_clock::now());
}

std::string RequestScheduler::RunningAction() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return running_ ? running_->request.action : std::string();
}

std::size_t RequestScheduler::JobCount() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return jobs_.size();
}

}  // namespace VectorworksMCP
