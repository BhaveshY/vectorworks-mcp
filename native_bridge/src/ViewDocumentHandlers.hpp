#pragma once

#include <stdexcept>
#include <string>

namespace VectorworksMCP::ViewDocument {

enum class ErrorCode {
    InvalidRequest,
    InvalidPath,
    InputNotFound,
    ConfirmationRequired,
    DocumentDirty,
    InterfaceUnavailable,
    SdkOperationFailed,
    ReadbackMismatch,
};

enum class CommitState {
    NotStarted,
    Accepted,
    Committed,
    Unknown,
};

class Error final : public std::runtime_error {
public:
    Error(
        ErrorCode code,
        const std::string& message,
        std::string requestedPath = {},
        std::string activePath = {},
        CommitState commitState = CommitState::NotStarted);
    ErrorCode Code() const noexcept;
    const std::string& RequestedPath() const noexcept;
    const std::string& ActivePath() const noexcept;
    CommitState State() const noexcept;

private:
    ErrorCode code_;
    std::string requestedPath_;
    std::string activePath_;
    CommitState commitState_ = CommitState::NotStarted;
};

struct ViewState {
    short standardView = 0;
    short projection = 0;
    short renderMode = 0;
    bool fitToObjectsApplied = false;
    bool selectionCleared = false;
};

struct SetViewRequest {
    bool setStandardView = false;
    short standardView = 0;
    bool setProjection = false;
    short projection = 0;
    bool setRenderMode = false;
    short renderMode = 0;
    bool fitToObjects = false;
    bool clearSelection = false;
};

struct DocumentResult {
    std::string operation;
    std::string canonicalPath;
    bool saved = false;
    std::string requestedPath;
    std::string activePath;
    CommitState commitState = CommitState::NotStarted;
};

struct PreparedOpenDocument {
    std::string canonicalPath;
};

ViewState GetView();
ViewState SetView(const SetViewRequest& request);
DocumentResult SaveDocument(
    const std::string& absolutePath,
    const std::string& replaceExistingConfirmation);
PreparedOpenDocument PrepareOpenDocument(
    const std::string& absolutePath,
    const std::string& replaceDirtyConfirmation);
bool LaunchPreparedOpenDocument(const PreparedOpenDocument& request);

const char* ErrorCodeName(ErrorCode code) noexcept;
const char* CommitStateName(CommitState state) noexcept;

}  // namespace VectorworksMCP::ViewDocument
