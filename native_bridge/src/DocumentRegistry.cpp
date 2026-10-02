#include "StdAfx.h"

#include "DocumentRegistry.hpp"

#include "BridgeProtocol.hpp"
#include "DocumentationHandlers.hpp"
#include "UiGate.hpp"
#include "ViewDocumentHandlers.hpp"

#include "Interfaces/VectorWorks/Filing/IFileIdentifier.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <iterator>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <system_error>
#include <unordered_map>
#include <unordered_set>

namespace VectorworksMCP::Docs {

namespace {

namespace fs = std::filesystem;

struct Entry {
    DocumentInfo info;
    MCObjectHandle header = nullptr;  // only known while the document is active
};

std::vector<Entry> gEntries;
std::uint64_t gSerial = 0;
std::uint64_t gGeneration = 1;
std::string gActiveFingerprint;
std::chrono::steady_clock::time_point gLastRefresh{};

std::mutex gCacheMutex;
std::string gCachedActiveJson = "null";
int gCachedCount = 0;

std::string HashHex(const std::string& text) {
    std::uint64_t hash = 14695981039346656037ull;
    for (const unsigned char ch : text) {
        hash ^= ch;
        hash *= 1099511628211ull;
    }
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << hash;
    return out.str();
}

std::string NewFingerprint(std::int32_t fileRef) {
    ++gSerial;
    return "vw-doc-" + HashHex(Documentation::BridgeSessionId() + ":" + std::to_string(gSerial) + ":" +
        std::to_string(fileRef)).substr(0, 16);
}

std::string ToLowerAscii(std::string value) {
    for (auto& ch : value) {
        if (ch >= 'A' && ch <= 'Z') ch = static_cast<char>(ch - 'A' + 'a');
    }
    return value;
}

std::string PathKey(const std::string& path) {
    std::string key = NormalizeReportedPath(path);
    for (auto& ch : key) {
        if (ch == '/') ch = '\\';
    }
    return ToLowerAscii(key);
}

int ActiveDirtyState() {
#if SDK_VERSION >= 3000
    return -1;
#else
    return gSDK->IsActiveFileChangedAfterLastSave() ? 1 : 0;
#endif
}

void UpdateCacheLocked() {
    std::string activeJson = "null";
    for (const auto& entry : gEntries) {
        if (entry.info.active) {
            activeJson = DocumentJson(entry.info);
            break;
        }
    }
    std::lock_guard<std::mutex> lock(gCacheMutex);
    gCachedActiveJson = activeJson;
    gCachedCount = static_cast<int>(gEntries.size());
}

VectorWorks::Filing::IFileIdentifierPtr FileIdentifierFor(const std::string& path) {
    VectorWorks::Filing::IFileIdentifierPtr identifier(VectorWorks::Filing::IID_FileIdentifier);
    if (!identifier) {
        throw Protocol::BridgeError("interface_unavailable", "Vectorworks file identifier interface is unavailable");
    }
    if (VCOM_FAILED(identifier->Set(TXString(path.c_str())))) {
        throw Protocol::BridgeError("invalid_path", "Vectorworks rejected the path: " + path);
    }
    return identifier;
}

std::string DocumentDetailsJson(const std::string& expected) {
    return "{\"expected_fingerprint\":" + JsonString(expected) +
        ",\"active_document\":" + ActiveDocumentJson() +
        ",\"open_documents\":" + OpenDocumentsJson() + "}";
}

bool SwitchTo(const DocumentInfo& info) {
    if (info.active) return true;
    if (info.fileRef < 0) return false;
    UiGate::NoteDocumentTransition();
    const bool switched = gSDK->SwitchToOpenFile(info.fileRef);
    Refresh(true);
    return switched && ActiveFingerprint() == info.fingerprint;
}

std::wstring Widen(const std::string& value) {
    if (value.empty()) return std::wstring();
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0);
    std::wstring out(static_cast<std::size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), size);
    return out;
}

std::string Narrow(const std::wstring& value) {
    if (value.empty()) return std::string();
    const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), size, nullptr, nullptr);
    return out;
}

std::string ApplicationDirectory() {
    wchar_t buffer[MAX_PATH * 4] = {};
    const DWORD length = GetModuleFileNameW(nullptr, buffer, static_cast<DWORD>(std::size(buffer)));
    if (length == 0) return std::string();
    fs::path path(std::wstring(buffer, length));
    return Narrow(path.parent_path().wstring());
}

std::string ResolveTemplatePath(const std::string& requested, std::vector<std::string>& tried) {
    std::error_code error;
    if (!requested.empty()) {
        const fs::path path = fs::u8path(requested);
        tried.push_back(requested);
        if (path.is_absolute() && fs::is_regular_file(path, error)) {
            return fs::canonical(path, error).u8string();
        }
        throw Protocol::BridgeError("input_not_found", "template_path does not exist: " + requested);
    }
    if (const char* configured = std::getenv("VW_MCP_NEW_DOCUMENT_TEMPLATE")) {
        if (configured[0] != '\0') {
            tried.push_back(configured);
            const fs::path path = fs::u8path(configured);
            if (fs::is_regular_file(path, error)) return path.u8string();
        }
    }
    const std::string appDir = ApplicationDirectory();
    if (appDir.empty()) return std::string();
    const fs::path templates = fs::u8path(appDir) / "Libraries" / "Defaults" / "Templates";
    for (const char* name : {"Blank (Metric).sta", "Blank (UK Metric).sta", "Blank (Imperial).sta"}) {
        const fs::path candidate = templates / name;
        tried.push_back(candidate.u8string());
        if (fs::is_regular_file(candidate, error)) return candidate.u8string();
    }
    // Localised installs: any top-level template whose name suggests "blank".
    if (fs::is_directory(templates, error)) {
        for (const auto& item : fs::directory_iterator(templates, error)) {
            if (!item.is_regular_file()) continue;
            const std::string file = ToLowerAscii(item.path().filename().u8string());
            if (item.path().extension() == ".sta" &&
                (file.rfind("blank", 0) == 0 || file.rfind("leer", 0) == 0 || file.rfind("vide", 0) == 0 ||
                 file.rfind("vuoto", 0) == 0 || file.rfind("blanco", 0) == 0)) {
                tried.push_back(item.path().u8string());
                return item.path().u8string();
            }
        }
    }
    return std::string();
}

std::string TempDirectory() {
    wchar_t buffer[MAX_PATH + 2] = {};
    const DWORD length = GetTempPathW(MAX_PATH + 1, buffer);
    fs::path base = length ? fs::path(std::wstring(buffer, length)) : fs::temp_directory_path();
    fs::path dir = base / L"vectorworks-mcp" / L"discard";
    std::error_code error;
    fs::create_directories(dir, error);
    return Narrow(dir.wstring());
}

}  // namespace

std::string NormalizeReportedPath(std::string path) {
    // GetOpenFilesList documents "Mac-encoded" paths. On Windows a
    // colon-separated path ("C:Users:me:file.vwx") is converted to
    // "C:\Users\me\file.vwx"; normal Windows paths pass through unchanged.
    if (path.find('\\') == std::string::npos && path.find('/') == std::string::npos) {
        const auto first = path.find(':');
        if (first != std::string::npos && path.find(':', first + 1) != std::string::npos) {
            std::string out;
            out.reserve(path.size() + 1);
            for (std::size_t index = 0; index < path.size(); ++index) {
                const char ch = path[index];
                if (ch == ':') {
                    if (index == first && first == 1u) {
                        out += ":\\";
                    } else {
                        out += '\\';
                    }
                } else {
                    out += ch;
                }
            }
            return out;
        }
    }
    return path;
}

void ResetForModuleInit() {
    gEntries.clear();
    gActiveFingerprint.clear();
    gLastRefresh = {};
    std::lock_guard<std::mutex> lock(gCacheMutex);
    gCachedActiveJson = "null";
    gCachedCount = 0;
}

void Refresh(bool force) {
    const auto now = std::chrono::steady_clock::now();
    if (!force && gLastRefresh != std::chrono::steady_clock::time_point{} &&
        now - gLastRefresh < std::chrono::milliseconds(200)) {
        return;
    }
    gLastRefresh = now;
    if (!gSDK) return;

    VectorWorks::TVWArray_OpenFileInformation files;
    gSDK->GetOpenFilesList(files);
    MCObjectHandle header = gSDK->GetDrawingHeader();

    std::vector<Entry> next;
    next.reserve(files.GetSize() + 1u);
    for (size_t index = 0; index < files.GetSize(); ++index) {
        const auto& file = files[index];
        Entry entry;
        entry.info.fileRef = file.fFileRef;
        entry.info.active = file.fIsActive;
        entry.info.untitled = file.fIsInMemoryOnly;
        if (file.fpFileID) {
            TXString path;
            TXString name;
            if (VCOM_SUCCEEDED(file.fpFileID->GetFileFullPath(path))) {
                entry.info.path = NormalizeReportedPath(path.GetStdString());
            }
            if (VCOM_SUCCEEDED(file.fpFileID->GetFileName(name))) {
                entry.info.name = name.GetStdString();
            }
        }
        if (entry.info.untitled) {
            entry.info.path.clear();
        }
        if (entry.info.name.empty()) {
            entry.info.name = entry.info.untitled ? "Untitled" : fs::u8path(entry.info.path).filename().u8string();
        }
        if (entry.info.active) {
            entry.header = header;
            entry.info.dirty = ActiveDirtyState();
        }
        // Reuse the fingerprint of the same open document.
        for (const auto& previous : gEntries) {
            if (previous.info.fileRef != entry.info.fileRef) continue;
            const bool headerChanged = entry.info.active && previous.header && header && previous.header != header;
            if (!headerChanged) {
                entry.info.fingerprint = previous.info.fingerprint;
                if (!entry.info.active) {
                    entry.header = previous.header;
                    entry.info.dirty = previous.info.active ? previous.info.dirty : previous.info.dirty;
                }
            }
            break;
        }
        if (entry.info.fingerprint.empty()) {
            entry.info.fingerprint = NewFingerprint(entry.info.fileRef);
        }
        next.push_back(std::move(entry));
    }
    if (next.empty() && header) {
        // Defensive: a drawing is active but the open-files list is empty.
        Entry entry;
        entry.info.fileRef = -2;
        entry.info.active = true;
        entry.info.untitled = true;
        entry.info.name = "Untitled";
        entry.header = header;
        entry.info.dirty = ActiveDirtyState();
        for (const auto& previous : gEntries) {
            if (previous.info.fileRef == -2 && previous.header == header) {
                entry.info.fingerprint = previous.info.fingerprint;
            }
        }
        if (entry.info.fingerprint.empty()) entry.info.fingerprint = NewFingerprint(-2);
        next.push_back(std::move(entry));
    }

    // Closed documents: drop their handle tables eagerly.
    for (const auto& previous : gEntries) {
        const bool stillOpen = std::any_of(next.begin(), next.end(), [&](const Entry& entry) {
            return entry.info.fingerprint == previous.info.fingerprint;
        });
        if (!stillOpen) {
            Handles::ClearForDocument(previous.info.fingerprint);
            ++gGeneration;
            UiGate::NoteDocumentTransition();
        }
    }
    std::string active;
    for (const auto& entry : next) {
        if (entry.info.active) active = entry.info.fingerprint;
    }
    if (active != gActiveFingerprint) {
        ++gGeneration;
        if (!gActiveFingerprint.empty()) {
            UiGate::NoteDocumentTransition();
        }
        gActiveFingerprint = active;
    }
    gEntries = std::move(next);
    UpdateCacheLocked();
}

std::vector<DocumentInfo> List() {
    std::vector<DocumentInfo> out;
    out.reserve(gEntries.size());
    for (const auto& entry : gEntries) out.push_back(entry.info);
    return out;
}

bool HasActiveDocument() {
    return !gActiveFingerprint.empty() && gSDK && gSDK->GetDrawingHeader() != nullptr;
}

std::string ActiveFingerprint() {
    return gActiveFingerprint;
}

const DocumentInfo* FindByFingerprint(const std::string& fingerprint) {
    for (const auto& entry : gEntries) {
        if (entry.info.fingerprint == fingerprint) return &entry.info;
    }
    return nullptr;
}

std::uint64_t Generation() {
    return gGeneration;
}

std::string DocumentJson(const DocumentInfo& info) {
    std::string json = "{\"fingerprint\":" + JsonString(info.fingerprint);
    json += ",\"name\":" + JsonString(info.name);
    json += ",\"path\":" + JsonString(info.path);
    json += ",\"dirty\":";
    json += info.dirty < 0 ? "null" : (info.dirty ? "true" : "false");
    json += ",\"active\":";
    json += info.active ? "true" : "false";
    json += ",\"untitled\":";
    json += info.untitled ? "true" : "false";
    json += ",\"file_ref\":" + std::to_string(info.fileRef);
    json += "}";
    return json;
}

std::string ActiveDocumentJson() {
    for (const auto& entry : gEntries) {
        if (entry.info.active) return DocumentJson(entry.info);
    }
    return "null";
}

std::string OpenDocumentsJson() {
    std::string json = "[";
    for (std::size_t index = 0; index < gEntries.size(); ++index) {
        if (index) json += ",";
        json += DocumentJson(gEntries[index].info);
    }
    return json + "]";
}

std::string CachedActiveDocumentJson() {
    std::lock_guard<std::mutex> lock(gCacheMutex);
    return gCachedActiveJson;
}

int CachedOpenDocumentCount() {
    std::lock_guard<std::mutex> lock(gCacheMutex);
    return gCachedCount;
}

void EnsureExpectedDocument(const Params& params) {
    const auto found = params.find("expect_document_fingerprint");
    if (found == params.end() || found->second.type != ParamValue::Type::String ||
        found->second.stringValue.empty()) {
        return;
    }
    const std::string expected = found->second.stringValue;
    Refresh(true);
    if (ActiveFingerprint() == expected) return;
    const bool activate = GetLenientBoolParam(params, "activate_document", false);
    const DocumentInfo* target = FindByFingerprint(expected);
    if (activate && target) {
        const DocumentInfo copy = *target;
        if (SwitchTo(copy)) return;
    }
    throw Protocol::BridgeError(
        "document_changed",
        target
            ? "the active Vectorworks document is not the expected document; pass activate_document:true or activate it first"
            : "the expected Vectorworks document is not open (closed or never opened in this bridge session)",
        false,
        "not_started",
        DocumentDetailsJson(expected));
}

std::string HandleListDocuments(const Params&) {
    Refresh(true);
    return "{\"documents\":" + OpenDocumentsJson() +
        ",\"active_fingerprint\":" + (gActiveFingerprint.empty() ? std::string("null") : JsonString(gActiveFingerprint)) +
        ",\"count\":" + std::to_string(gEntries.size()) +
        ",\"document_generation\":" + std::to_string(gGeneration) + "}";
}

std::string HandleActivateDocument(const Params& params) {
    Refresh(true);
    const std::string fingerprint = GetStringParam(params, "fingerprint");
    const std::string path = GetStringParam(params, "path");
    const std::string name = GetStringParam(params, "name");
    if (fingerprint.empty() && path.empty() && name.empty()) {
        throw Protocol::BridgeError("invalid_request", "activate_document requires fingerprint, path or name");
    }
    std::vector<DocumentInfo> matches;
    for (const auto& entry : gEntries) {
        if (!fingerprint.empty()) {
            if (entry.info.fingerprint == fingerprint) matches.push_back(entry.info);
        } else if (!path.empty()) {
            if (!entry.info.path.empty() && PathKey(entry.info.path) == PathKey(path)) matches.push_back(entry.info);
        } else if (entry.info.name == name) {
            matches.push_back(entry.info);
        }
    }
    if (matches.empty() && fingerprint.empty() && path.empty()) {
        for (const auto& entry : gEntries) {
            if (ToLowerAscii(entry.info.name) == ToLowerAscii(name)) matches.push_back(entry.info);
        }
    }
    if (matches.empty()) {
        throw Protocol::BridgeError(
            "document_not_found", "no open Vectorworks document matches the request", false, "not_started",
            "{\"open_documents\":" + OpenDocumentsJson() + "}");
    }
    if (matches.size() > 1u) {
        throw Protocol::BridgeError(
            "ambiguous_document", "several open documents match; pass a fingerprint", false, "not_started",
            "{\"open_documents\":" + OpenDocumentsJson() + "}");
    }
    const std::string previous = gActiveFingerprint;
    const DocumentInfo target = matches.front();
    bool switched = false;
    if (!target.active) {
        if (!SwitchTo(target)) {
            throw Protocol::BridgeError(
                "sdk_operation_failed", "SwitchToOpenFile did not activate the requested document", true, "unknown",
                "{\"open_documents\":" + OpenDocumentsJson() + "}");
        }
        switched = true;
        gSDK->DrawScreen();
    }
    return "{\"active_document\":" + ActiveDocumentJson() +
        ",\"previous_fingerprint\":" + (previous.empty() ? std::string("null") : JsonString(previous)) +
        ",\"switched\":" + (switched ? "true" : "false") + "}";
}

std::string HandleNewDocument(const Params& params) {
    Refresh(true);
    std::vector<std::string> tried;
    const std::string templatePath = ResolveTemplatePath(GetStringParam(params, "template_path"), tried);
    if (templatePath.empty()) {
        throw Protocol::BridgeError(
            "unsupported",
            "the Vectorworks SDK has no dialog-free 'new document' call and no blank template was found; "
            "pass template_path (a .sta/.vwx file) or set VW_MCP_NEW_DOCUMENT_TEMPLATE",
            false, "not_started",
            "{\"tried\":" + JsonStringArray(tried) + "}");
    }
    std::unordered_set<std::string> before;
    for (const auto& entry : gEntries) before.insert(entry.info.fingerprint);
    bool opened = false;
    {
        UiGate::ScopedLoading loading;
        auto identifier = FileIdentifierFor(templatePath);
        opened = gSDK->OpenDocumentPath(identifier, false);
    }
    Refresh(true);
    const DocumentInfo* created = nullptr;
    for (const auto& entry : gEntries) {
        if (before.count(entry.info.fingerprint) == 0u && entry.info.active) created = &entry.info;
    }
    if (!created) {
        for (const auto& entry : gEntries) {
            if (before.count(entry.info.fingerprint) == 0u) created = &entry.info;
        }
    }
    if (!opened || !created) {
        throw Protocol::BridgeError(
            "sdk_operation_failed",
            "OpenDocumentPath did not open a new document from the template",
            false,
            opened ? "unknown" : "not_started",
            "{\"template_path\":" + JsonString(templatePath) + "}");
    }
    std::vector<std::string> warnings;
    if (!created->untitled) {
        warnings.push_back(
            "the template file was opened directly instead of as an untitled copy; do not save over it");
    }
    if (!created->active) {
        warnings.push_back("the new document is open but not active");
    }
    gSDK->DrawScreen();
    return "{\"document\":" + DocumentJson(*created) +
        ",\"template_path\":" + JsonString(templatePath) +
        ",\"untitled\":" + (created->untitled ? "true" : "false") +
        ",\"method\":\"open_template\",\"warnings\":" + JsonStringArray(warnings) + "}";
}

std::string HandleCloseDocument(const Params& params) {
    Refresh(true);
    std::string fingerprint = GetStringParam(params, "fingerprint");
    if (fingerprint.empty()) fingerprint = gActiveFingerprint;
    const DocumentInfo* found = FindByFingerprint(fingerprint);
    if (!found) {
        throw Protocol::BridgeError(
            "document_not_found", "no open document has that fingerprint", false, "not_started",
            "{\"open_documents\":" + OpenDocumentsJson() + "}");
    }
    const DocumentInfo target = *found;
    bool discard = false;
    const auto discardParam = params.find("discard_changes");
    if (discardParam != params.end()) {
        if (discardParam->second.type == ParamValue::Type::String) {
            discard = ToLower(discardParam->second.stringValue) == "discard";
        } else if (discardParam->second.type == ParamValue::Type::Boolean) {
            discard = discardParam->second.boolValue;
        }
    }
    const std::string previous = gActiveFingerprint;
    if (!target.active && !SwitchTo(target)) {
        throw Protocol::BridgeError("sdk_operation_failed", "could not activate the document to close it", true, "not_started");
    }
    const DocumentInfo* active = FindByFingerprint(fingerprint);
    const int dirty = active ? active->dirty : -1;
    const bool hasUntitledContent = active && active->untitled;
    if ((dirty != 0 || (dirty < 0 && hasUntitledContent)) && !discard) {
        if (previous != fingerprint) {
            if (const DocumentInfo* back = FindByFingerprint(previous)) {
                const DocumentInfo copy = *back;
                SwitchTo(copy);
            }
        }
        throw Protocol::BridgeError(
            "document_dirty",
            dirty > 0
                ? "the document has unsaved changes; pass discard_changes:\"discard\" to close it without saving"
                : "the document's unsaved-changes state is unknown; pass discard_changes:\"discard\" to close it without saving",
            false,
            "not_started",
            "{\"fingerprint\":" + JsonString(fingerprint) + ",\"dirty\":" +
                (dirty < 0 ? std::string("null") : (dirty ? std::string("true") : std::string("false"))) + "}");
    }

    std::string tempFile;
    bool discarded = false;
    {
        UiGate::ScopedLoading loading;
        if (dirty != 0 && discard) {
            // Saving to a private temp file first makes the document clean,
            // so CloseDocument can never show a save prompt. The original
            // file on disk is left untouched.
            const std::string dir = TempDirectory();
            tempFile = dir + "\\discard-" + fingerprint + "-" + std::to_string(UnixMillisecondsNow()) + ".vwx";
            auto identifier = FileIdentifierFor(tempFile);
            if (gSDK->SaveActiveDocumentPath(identifier) != kVCOMError_NoError) {
                throw Protocol::BridgeError(
                    "sdk_operation_failed",
                    "could not detach unsaved changes before closing; nothing was closed",
                    false, "not_started");
            }
            discarded = true;
        }
        gSDK->CloseDocument();
    }
    Refresh(true);
    const bool stillOpen = FindByFingerprint(fingerprint) != nullptr;
    if (!tempFile.empty()) {
        std::error_code error;
        fs::remove(fs::u8path(tempFile), error);
    }
    if (stillOpen) {
        throw Protocol::BridgeError(
            "sdk_operation_failed", "CloseDocument returned but the document is still open", true, "unknown");
    }
    Handles::ClearForDocument(fingerprint);
    if (previous != fingerprint) {
        if (const DocumentInfo* back = FindByFingerprint(previous)) {
            const DocumentInfo copy = *back;
            SwitchTo(copy);
        }
    }
    return "{\"closed\":true,\"fingerprint\":" + JsonString(fingerprint) +
        ",\"discarded_changes\":" + (discarded ? "true" : "false") +
        ",\"active_document\":" + ActiveDocumentJson() +
        ",\"open_document_count\":" + std::to_string(gEntries.size()) + "}";
}

std::string HandleOpenDocument(const Params& params) {
    Refresh(true);
    const std::string requested = GetStringParam(params, "file_path", GetStringParam(params, "path"));
    // Vectorworks opens documents side by side; opening never replaces the
    // active document, so the legacy replace_dirty_confirmation is accepted
    // but no longer required.
    auto prepared = ViewDocument::PrepareOpenDocumentPath(requested);
    std::string alreadyOpenFingerprint;
    for (const auto& entry : gEntries) {
        if (!entry.info.path.empty() && PathKey(entry.info.path) == PathKey(prepared.canonicalPath)) {
            alreadyOpenFingerprint = entry.info.fingerprint;
        }
    }
    bool opened = false;
    {
        UiGate::ScopedLoading loading;
        opened = ViewDocument::LaunchPreparedOpenDocument(prepared);
    }
    Refresh(true);
    if (!opened) {
        throw ViewDocument::Error(
            ViewDocument::ErrorCode::SdkOperationFailed,
            "Vectorworks did not open the document (no dialog fallback was attempted)",
            prepared.canonicalPath,
            "",
            ViewDocument::CommitState::Unknown);
    }
    const std::string activeJson = ActiveDocumentJson();
    std::string activePath;
    for (const auto& entry : gEntries) {
        if (entry.info.active) activePath = entry.info.path;
    }
    return "{\"operation\":\"open_document\",\"path\":" + JsonString(prepared.canonicalPath) +
        ",\"saved\":false,\"requested_path\":" + JsonString(prepared.canonicalPath) +
        ",\"active_path\":" + JsonString(activePath) +
        ",\"commit_state\":\"committed\",\"already_open\":" + (alreadyOpenFingerprint.empty() ? "false" : "true") +
        ",\"document\":" + activeJson + "}";
}

std::string HandleSaveDocument(const Params& params) {
    Refresh(true);
    const auto result = ViewDocument::SaveDocument(
        GetStringParam(params, "file_path", GetStringParam(params, "path")),
        GetStringParam(params, "replace_confirmation"));
    Refresh(true);
    return "{\"operation\":" + JsonString(result.operation) +
        ",\"path\":" + JsonString(result.canonicalPath) +
        ",\"saved\":" + (result.saved ? "true" : "false") +
        ",\"requested_path\":" + JsonString(result.requestedPath) +
        ",\"active_path\":" + JsonString(result.activePath) +
        ",\"commit_state\":" + JsonString(ViewDocument::CommitStateName(result.commitState)) +
        ",\"document\":" + ActiveDocumentJson() + "}";
}

}  // namespace VectorworksMCP::Docs

namespace VectorworksMCP::Handles {

namespace {

struct HandleEntry {
    std::string uuid;
    std::string fingerprint;
};

std::unordered_map<std::string, HandleEntry> gById;
std::unordered_map<std::string, std::string> gByKey;
std::uint64_t gCounter = 0;
constexpr std::size_t kMaxHandles = 500000u;

std::string UuidOf(MCObjectHandle object) {
    TXString uuid;
    if (object && gSDK->GetObjectUuid(object, uuid) && !uuid.IsEmpty()) {
        return uuid.GetStdString();
    }
    return std::string();
}

}  // namespace

std::string IdFor(MCObjectHandle object) {
    if (!object) return std::string();
    const std::string uuid = UuidOf(object);
    const std::string fingerprint = Docs::ActiveFingerprint();
    if (uuid.empty()) {
        return "h0-" + std::to_string(++gCounter);
    }
    const std::string key = fingerprint + "|" + uuid;
    const auto existing = gByKey.find(key);
    if (existing != gByKey.end()) return existing->second;
    if (gById.size() >= kMaxHandles) {
        gById.clear();
        gByKey.clear();
    }
    const std::string id = "h" + std::to_string(++gCounter);
    gById.emplace(id, HandleEntry{uuid, fingerprint});
    gByKey.emplace(key, id);
    return id;
}

MCObjectHandle Resolve(const std::string& rawId) {
    std::string id = TrimCopy(rawId);
    if (id.rfind("handle:", 0) == 0) id = id.substr(7);
    if (id.empty()) {
        throw Protocol::BridgeError("invalid_request", "handle is required");
    }
    const auto found = gById.find(id);
    if (found == gById.end()) {
        throw Protocol::BridgeError(
            "stale_reference",
            "handle '" + id + "' is unknown to this bridge session (objects are referenced by uuid; re-read the object)",
            false, "not_started", "{\"handle\":" + JsonString(id) + "}");
    }
    const auto& entry = found->second;
    if (entry.fingerprint != Docs::ActiveFingerprint()) {
        throw Protocol::BridgeError(
            "stale_reference",
            "handle '" + id + "' belongs to a different document than the active one",
            false, "not_started",
            "{\"handle\":" + JsonString(id) + ",\"uuid\":" + JsonString(entry.uuid) +
                ",\"document_fingerprint\":" + JsonString(entry.fingerprint) + "}");
    }
    MCObjectHandle object = gSDK->GetObjectByUuid(TXString(entry.uuid.c_str()));
    if (!object) {
        throw Protocol::BridgeError(
            "stale_reference",
            "handle '" + id + "' refers to an object that no longer exists",
            false, "not_started",
            "{\"handle\":" + JsonString(id) + ",\"uuid\":" + JsonString(entry.uuid) + "}");
    }
    return object;
}

void ClearForDocument(const std::string& fingerprint) {
    for (auto it = gById.begin(); it != gById.end();) {
        if (it->second.fingerprint == fingerprint) {
            gByKey.erase(it->second.fingerprint + "|" + it->second.uuid);
            it = gById.erase(it);
        } else {
            ++it;
        }
    }
}

void ClearAll() {
    gById.clear();
    gByKey.clear();
}

std::size_t Count() {
    return gById.size();
}

}  // namespace VectorworksMCP::Handles
