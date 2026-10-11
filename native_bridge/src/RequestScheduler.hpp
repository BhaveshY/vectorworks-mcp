#pragma once

// Request scheduler shared by the socket threads and the Vectorworks main
// thread (contract 1.4 / 1.5). Replaces the 0.x CadRequestQueue:
//   * per-request timeout_ms (default 25 s, max 600 s);
//   * requests that have not started by the deadline are removed and fail
//     `timeout_not_started` (nothing ran);
//   * requests that already started are converted into jobs and the caller
//     gets `still_running` + details.job_id (never a silent drop);
//   * params.async:true queues a job and answers immediately;
//   * the main thread dequeues only when the UI gate allows it and fails
//     queued work with `vectorworks_busy` / `no_active_document`.

#include "BridgeProtocol.hpp"

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>

namespace VectorworksMCP {

enum class UiState {
    Idle,
    Modal,
    Tracking,
    Loading,
    NoDocument,
};

const char* UiStateName(UiState state) noexcept;

struct SubmitOptions {
    std::int64_t timeoutMs = 25000;
    bool async = false;
    std::int64_t busyWaitMs = 3000;
    bool needsDocument = true;
    bool mutates = false;
    // DialogWatch::DismissSequence() when the request was submitted; results
    // report dialogs auto-dismissed after this point.
    std::uint64_t dialogSeq = 0;
};

enum class JobState {
    Queued,
    Running,
    Done,
    Failed,
    Cancelled,
};

const char* JobStateName(JobState state) noexcept;

struct ScheduledRequest {
    Protocol::RequestEnvelope request;
    SubmitOptions options;
    JobState state = JobState::Queued;
    bool isJob = false;
    std::string jobId;
    std::chrono::steady_clock::time_point enqueuedAt{};
    std::chrono::steady_clock::time_point startedAt{};
    std::chrono::steady_clock::time_point finishedAt{};
    std::int64_t enqueuedAtUnixMs = 0;
    std::int64_t startedAtUnixMs = 0;
    std::int64_t finishedAtUnixMs = 0;
    Protocol::ResponseEnvelope response;
};

class RequestScheduler {
public:
    static constexpr std::size_t kMaxPending = 128u;
    static constexpr std::size_t kMaxJobs = 64u;
    static constexpr std::int64_t kJobRetentionMs = 15 * 60 * 1000;
    static constexpr std::int64_t kDefaultTimeoutMs = 25000;
    static constexpr std::int64_t kMaxTimeoutMs = 600000;
    static constexpr std::int64_t kMinTimeoutMs = 50;

    using Notifier = std::function<void()>;
    // Returns extra JSON members (",\"key\":value...") appended to the
    // details of vectorworks_busy / timeout_not_started failures.
    using DetailsProvider = std::function<std::string()>;

    void SetNotifier(Notifier notifier);
    void SetBusyDetailsProvider(DetailsProvider provider);

    // ---- socket threads -------------------------------------------------
    // `whileWaiting` (optional) runs on the waiting socket thread about every
    // 200 ms until the request finishes (dialog watchdog).
    Protocol::ResponseEnvelope SubmitAndWait(
        const Protocol::RequestEnvelope& request,
        const SubmitOptions& options,
        const std::function<std::string()>& uiStateProvider,
        const std::function<void()>& whileWaiting = {});
    std::string JobStatusJson(const std::string& jobId, const std::string& uiState);
    std::string CancelJobJson(const std::string& jobId);

    // ---- Vectorworks main thread ----------------------------------------
    // Returns the next request that may run in `state`; fails requests that
    // can never run in that state or whose busy-wait budget elapsed.
    std::shared_ptr<ScheduledRequest> TryDequeue(UiState state);
    void Complete(const std::shared_ptr<ScheduledRequest>& entry, Protocol::ResponseEnvelope response);

    // ---- lifecycle / diagnostics ------------------------------------------
    void CancelAll(const std::string& reason);
    void Reset();
    std::size_t QueueDepth() const;
    std::int64_t RunningMilliseconds() const;
    std::string RunningAction() const;
    std::size_t JobCount() const;

private:
    void PruneJobsLocked(std::chrono::steady_clock::time_point now);
    void FinishLocked(
        const std::shared_ptr<ScheduledRequest>& entry,
        Protocol::ResponseEnvelope response,
        JobState state);
    std::string NewJobIdLocked();
    std::string BusyExtraLocked() const;

    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<std::shared_ptr<ScheduledRequest>> queue_;
    std::shared_ptr<ScheduledRequest> running_;
    std::unordered_map<std::string, std::shared_ptr<ScheduledRequest>> jobs_;
    std::unordered_set<std::string> inFlightIds_;
    Notifier notifier_;
    DetailsProvider busyDetails_;
    bool cancelled_ = false;
    std::string cancelReason_;
    std::uint64_t jobCounter_ = 0;
};

}  // namespace VectorworksMCP
