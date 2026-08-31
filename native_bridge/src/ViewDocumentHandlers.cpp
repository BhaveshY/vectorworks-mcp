#include "StdAfx.h"

#include "ViewDocumentHandlers.hpp"

#include <filesystem>
#include <system_error>
#include <utility>

#if defined(SDK_VERSION)
#define VECTORWORKS_MCP_VIEW_DOCUMENT_HAS_SDK 1
#include "Interfaces/VectorWorks/Extension/IExtendedProps.h"
#include "Interfaces/VectorWorks/Filing/IFileIdentifier.h"
#else
#define VECTORWORKS_MCP_VIEW_DOCUMENT_HAS_SDK 0
#endif

namespace VectorworksMCP::ViewDocument {
namespace {

namespace fs = std::filesystem;
constexpr const char* kReplaceDirtyConfirmation = "REPLACE_DIRTY_DOCUMENT";
constexpr const char* kReplaceExistingConfirmation = "REPLACE_EXISTING_FILE";

bool ValidStandardView(short value) {
    return value == standardViewUserDefined || (value >= standardViewFront && value <= standardViewBottomLeftRearIso);
}

bool ValidProjection(short value) {
    return value >= projectionOrthogonal && value <= projectionPlan;
}

bool ValidRenderMode(short value) {
    return value >= renderWireFrame && value <= renderCustomRenderWorks && value != 10;
}

fs::path CanonicalAbsolutePath(const std::string& raw, bool mustExist) {
    if (raw.empty()) {
        throw Error(ErrorCode::InvalidPath, "file_path must be a non-empty absolute path");
    }
    std::error_code error;
    fs::path path = fs::u8path(raw);
    if (!path.is_absolute()) {
        throw Error(ErrorCode::InvalidPath, "file_path must be absolute");
    }
    path = mustExist ? fs::canonical(path, error) : fs::weakly_canonical(path, error);
    if (error) {
        throw Error(
            mustExist ? ErrorCode::InputNotFound : ErrorCode::InvalidPath,
            mustExist ? "document path does not exist" : "document path could not be canonicalized");
    }
    if (mustExist && !fs::is_regular_file(path, error)) {
        throw Error(ErrorCode::InputNotFound, "document path is not a regular file");
    }
    return path;
}

#if VECTORWORKS_MCP_VIEW_DOCUMENT_HAS_SDK
struct ActiveDocumentReadback {
    bool readable = false;
    bool saved = false;
    std::string path;
};

VectorWorks::Filing::IFileIdentifierPtr FileIdentifier(const fs::path& path) {
    VectorWorks::Filing::IFileIdentifierPtr identifier(VectorWorks::Filing::IID_FileIdentifier);
    if (!identifier) {
        throw Error(ErrorCode::InterfaceUnavailable, "Vectorworks file identifier interface is unavailable");
    }
    if (VCOM_FAILED(identifier->Set(TXString(path.u8string())))) {
        throw Error(ErrorCode::InvalidPath, "Vectorworks rejected the canonical document path");
    }
    return identifier;
}

ActiveDocumentReadback ReadActiveDocument() {
    ActiveDocumentReadback readback;
    if (!gSDK) {
        return readback;
    }
    VectorWorks::Filing::IFileIdentifierPtr active(VectorWorks::Filing::IID_FileIdentifier);
    if (!active || !gSDK->GetActiveDocument(&active, readback.saved)) {
        return readback;
    }
    TXString activePath;
    if (VCOM_FAILED(active->GetFileFullPath(activePath))) {
        return readback;
    }
    readback.path = activePath.GetStdString();
    readback.readable = !readback.path.empty();
    return readback;
}

ViewState ReadView() {
    if (!gSDK) {
        throw Error(ErrorCode::InterfaceUnavailable, "Vectorworks SDK is unavailable");
    }
    MCObjectHandle layer = gSDK->GetActiveLayer();
    if (!layer) {
        layer = gSDK->GetCurrentLayer();
    }
    if (!layer) {
        throw Error(ErrorCode::SdkOperationFailed, "active document has no readable layer view");
    }
    return {
        static_cast<short>(gSDK->GetCurrentView()),
        static_cast<short>(gSDK->GetProjection(layer)),
        static_cast<short>(gSDK->GetRenderMode(layer)),
    };
}
#endif

}  // namespace

Error::Error(
    ErrorCode code,
    const std::string& message,
    std::string requestedPath,
    std::string activePath,
    CommitState commitState)
    : std::runtime_error(message),
      code_(code),
      requestedPath_(std::move(requestedPath)),
      activePath_(std::move(activePath)),
      commitState_(commitState) {}
ErrorCode Error::Code() const noexcept { return code_; }
const std::string& Error::RequestedPath() const noexcept { return requestedPath_; }
const std::string& Error::ActivePath() const noexcept { return activePath_; }
CommitState Error::State() const noexcept { return commitState_; }

const char* ErrorCodeName(ErrorCode code) noexcept {
    switch (code) {
        case ErrorCode::InvalidRequest: return "invalid_request";
        case ErrorCode::InvalidPath: return "invalid_path";
        case ErrorCode::InputNotFound: return "input_not_found";
        case ErrorCode::ConfirmationRequired: return "confirmation_required";
        case ErrorCode::DocumentDirty: return "document_dirty";
        case ErrorCode::InterfaceUnavailable: return "interface_unavailable";
        case ErrorCode::SdkOperationFailed: return "sdk_operation_failed";
        case ErrorCode::ReadbackMismatch: return "readback_mismatch";
    }
    return "sdk_operation_failed";
}

const char* CommitStateName(CommitState state) noexcept {
    switch (state) {
        case CommitState::NotStarted: return "not_started";
        case CommitState::Accepted: return "accepted";
        case CommitState::Committed: return "committed";
        case CommitState::Unknown: return "unknown";
    }
    return "unknown";
}

ViewState GetView() {
#if VECTORWORKS_MCP_VIEW_DOCUMENT_HAS_SDK
    return ReadView();
#else
    throw Error(ErrorCode::InterfaceUnavailable, "view control requires the Vectorworks SDK build");
#endif
}

ViewState SetView(const SetViewRequest& request) {
#if VECTORWORKS_MCP_VIEW_DOCUMENT_HAS_SDK
    if (!request.setStandardView && !request.setProjection && !request.setRenderMode &&
        !request.fitToObjects && !request.clearSelection) {
        throw Error(
            ErrorCode::InvalidRequest,
            "set_view requires standard_view, projection, render_mode, fit_to_objects, or clear_selection");
    }
    if (request.setStandardView && !ValidStandardView(request.standardView)) {
        throw Error(ErrorCode::InvalidRequest, "standard_view is not a supported Vectorworks standard view value");
    }
    if (request.setProjection && !ValidProjection(request.projection)) {
        throw Error(ErrorCode::InvalidRequest, "projection is not a supported Vectorworks projection value");
    }
    if (request.setRenderMode && !ValidRenderMode(request.renderMode)) {
        throw Error(ErrorCode::InvalidRequest, "render_mode is not a supported Vectorworks render mode value");
    }
    MCObjectHandle layer = gSDK ? gSDK->GetActiveLayer() : nullptr;
    if (!layer && gSDK) {
        layer = gSDK->GetCurrentLayer();
    }
    if (!gSDK || !layer) {
        throw Error(ErrorCode::SdkOperationFailed, "active document has no writable layer view");
    }
    if (request.setStandardView) {
        gSDK->SetCurrentView(request.standardView, true);
    }
    if (request.setProjection) {
        gSDK->SetProjection(layer, request.projection, false, false);
    }
    if (request.setRenderMode) {
        gSDK->SetRenderMode(layer, request.renderMode, true, false);
    }
    if (request.fitToObjects) {
        if (request.clearSelection) {
            gSDK->SelectAll();
        }
        // The SDK documents the short return type but does not define it as a
        // Boolean result (and its own mock returns zero). Dispatch is the
        // authoritative operation; visual capture provides framing proof.
        gSDK->DoMenuName(TXString("Fit to Objects"), 0);
        if (request.clearSelection) {
            gSDK->DeselectAll();
        }
    } else if (request.clearSelection) {
        gSDK->DeselectAll();
    }
    if (request.fitToObjects || request.clearSelection) {
        gSDK->DrawScreen();
    }
    ViewState actual = ReadView();
    actual.fitToObjectsApplied = request.fitToObjects;
    actual.selectionCleared = request.clearSelection;
    if ((request.setStandardView && actual.standardView != request.standardView) ||
        (request.setProjection && actual.projection != request.projection) ||
        (request.setRenderMode && actual.renderMode != request.renderMode)) {
        throw Error(ErrorCode::ReadbackMismatch, "Vectorworks view readback did not match the requested state");
    }
    return actual;
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "view control requires the Vectorworks SDK build");
#endif
}

DocumentResult SaveDocument(
    const std::string& absolutePath,
    const std::string& replaceExistingConfirmation) {
#if VECTORWORKS_MCP_VIEW_DOCUMENT_HAS_SDK
    if (!gSDK) {
        throw Error(ErrorCode::InterfaceUnavailable, "Vectorworks SDK is unavailable");
    }
    if (absolutePath.empty()) {
        VectorWorks::Filing::IFileIdentifierPtr active(VectorWorks::Filing::IID_FileIdentifier);
        bool alreadySaved = false;
        if (!active || !gSDK->GetActiveDocument(&active, alreadySaved) || !alreadySaved) {
            throw Error(ErrorCode::InvalidPath, "unsaved documents require an explicit absolute file_path");
        }
        TXString currentPath;
        active->GetFileFullPath(currentPath);
        if (!gSDK->SaveActiveFile()) {
            throw Error(ErrorCode::SdkOperationFailed, "SaveActiveFile failed; no dialog fallback was attempted");
        }
        const std::string path = currentPath.GetStdString();
        return {"save_document", path, true, path, path, CommitState::Committed};
    }
    const fs::path path = CanonicalAbsolutePath(absolutePath, false);
    std::error_code error;
    const fs::path parent = path.parent_path();
    if (parent.empty() || !fs::is_directory(parent, error)) {
        throw Error(ErrorCode::InvalidPath, "save destination parent directory does not exist");
    }
    if (fs::exists(path, error)) {
        VectorWorks::Filing::IFileIdentifierPtr active(VectorWorks::Filing::IID_FileIdentifier);
        bool saved = false;
        TXString activePathText;
        fs::path activePath;
        if (active && gSDK->GetActiveDocument(&active, saved) && saved &&
            VCOM_SUCCEEDED(active->GetFileFullPath(activePathText))) {
            activePath = fs::weakly_canonical(fs::u8path(activePathText.GetStdString()), error);
            error.clear();
        }
        if (activePath != path && replaceExistingConfirmation != kReplaceExistingConfirmation) {
            throw Error(
                ErrorCode::ConfirmationRequired,
                "save destination exists; pass replace_confirmation=REPLACE_EXISTING_FILE");
        }
    }
    auto identifier = FileIdentifier(path);
    if (gSDK->SaveActiveDocumentPath(identifier) != kVCOMError_NoError) {
        throw Error(ErrorCode::SdkOperationFailed, "SaveActiveDocumentPath failed; no dialog fallback was attempted");
    }
    if (!fs::is_regular_file(path, error) || error) {
        throw Error(ErrorCode::ReadbackMismatch, "saved document was not found at the requested path");
    }
    const std::string requested = path.u8string();
    const ActiveDocumentReadback active = ReadActiveDocument();
    return {
        "save_document",
        requested,
        true,
        requested,
        active.readable ? active.path : requested,
        CommitState::Committed,
    };
#else
    (void) absolutePath;
    (void) replaceExistingConfirmation;
    throw Error(ErrorCode::InterfaceUnavailable, "document save requires the Vectorworks SDK build");
#endif
}

PreparedOpenDocument PrepareOpenDocument(
    const std::string& absolutePath,
    const std::string& replaceDirtyConfirmation) {
#if VECTORWORKS_MCP_VIEW_DOCUMENT_HAS_SDK
    if (!gSDK) {
        throw Error(ErrorCode::InterfaceUnavailable, "Vectorworks SDK is unavailable");
    }
#if SDK_VERSION >= 3000
    // Vectorworks 2025 removed IsActiveFileChangedAfterLastSave from ISDK.
    // Without an authoritative dirty-state read, fail conservatively and
    // require the same explicit replacement confirmation used for a known
    // dirty 2024 document.
    const bool replacementConfirmationRequired = true;
#else
    const bool replacementConfirmationRequired = gSDK->IsActiveFileChangedAfterLastSave();
#endif
    if (replacementConfirmationRequired && replaceDirtyConfirmation != kReplaceDirtyConfirmation) {
        throw Error(
            ErrorCode::ConfirmationRequired,
            "active document has or may have unsaved changes; pass replace_dirty_confirmation=REPLACE_DIRTY_DOCUMENT");
    }
    return {CanonicalAbsolutePath(absolutePath, true).u8string()};
#else
    (void) absolutePath;
    (void) replaceDirtyConfirmation;
    throw Error(ErrorCode::InterfaceUnavailable, "document open requires the Vectorworks SDK build");
#endif
}

bool LaunchPreparedOpenDocument(const PreparedOpenDocument& request) {
#if VECTORWORKS_MCP_VIEW_DOCUMENT_HAS_SDK
    const fs::path path = CanonicalAbsolutePath(request.canonicalPath, true);
    if (!gSDK->GetActiveLayer()) {
        VectorWorks::Extension::IExtendedPropsPtr extendedProps(
            VectorWorks::Extension::IID_ExtendedProps);
        if (!extendedProps) {
            throw Error(ErrorCode::InterfaceUnavailable, "application document interface is unavailable");
        }
        const bool opened = extendedProps->OpenOrActivateFile(TXString(path.u8string()));
        if (opened) {
            gSDK->DrawScreen();
        }
        return opened;
    }
    VectorWorks::TVWArray_OpenFileInformation openFiles;
    gSDK->GetOpenFilesList(openFiles);
    for (size_t index = 0u; index < openFiles.GetSize(); ++index) {
        const auto& openFile = openFiles[index];
        if (!openFile.fpFileID || openFile.fIsInMemoryOnly) {
            continue;
        }
        TXString openPathText;
        if (VCOM_FAILED(openFile.fpFileID->GetFileFullPath(openPathText))) {
            continue;
        }
        std::error_code error;
        const fs::path openPath = fs::weakly_canonical(
            fs::u8path(openPathText.GetStdString()), error);
        if (error) {
            continue;
        }
        const bool sameFile = fs::equivalent(path, openPath, error);
        if (!error && sameFile) {
            const bool activated = openFile.fIsActive || gSDK->SwitchToOpenFile(openFile.fFileRef);
            if (activated) {
                gSDK->DrawScreen();
            }
            return activated;
        }
    }
    auto identifier = FileIdentifier(path);
    const bool opened = gSDK && gSDK->OpenDocumentPath(identifier, false);
    if (opened) {
        gSDK->DrawScreen();
    }
    return opened;
#else
    (void) request;
    return false;
#endif
}

}  // namespace VectorworksMCP::ViewDocument
