#include "StdAfx.h"

#include "NativeIOHandlers.hpp"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <filesystem>
#include <iterator>
#include <set>
#include <sstream>
#include <system_error>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#if defined(SDK_VERSION)
#define VECTORWORKS_MCP_NATIVE_IO_HAS_SDK 1
#include "Interfaces/VectorWorks/Filing/IExportPDF.h"
#include "Interfaces/VectorWorks/Filing/IFileIdentifier.h"
#include "Interfaces/VectorWorks/Filing/IFolderIdentifier.h"
#include "Interfaces/VectorWorks/Filing/IImportExportDWG.h"
#else
#define VECTORWORKS_MCP_NATIVE_IO_HAS_SDK 0
#endif

namespace VectorworksMCP::NativeIO {

namespace {

namespace fs = std::filesystem;

constexpr const char* kReplaceConfirmation = "REPLACE_EXISTING_FILE";

std::string Lower(std::string value) {
    std::transform(
        value.begin(),
        value.end(),
        value.begin(),
        [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
    return value;
}

std::string PathText(const fs::path& path) {
    return path.u8string();
}

bool HasAllowedExtension(const fs::path& path, const std::vector<std::string>& allowed) {
    const std::string extension = Lower(PathText(path.extension()));
    return std::find(allowed.begin(), allowed.end(), extension) != allowed.end();
}

std::string AllowedExtensionsText(const std::vector<std::string>& allowed) {
    std::ostringstream out;
    for (std::size_t index = 0; index < allowed.size(); ++index) {
        if (index != 0u) {
            out << ", ";
        }
        out << allowed[index];
    }
    return out.str();
}

fs::path CanonicalExistingInput(
    const std::string& value,
    const std::vector<std::string>& allowedExtensions) {
    if (value.empty()) {
        throw Error(ErrorCode::InvalidPath, "input path is required");
    }
    const fs::path supplied = fs::u8path(value);
    if (!supplied.is_absolute()) {
        throw Error(ErrorCode::InvalidPath, "input path must be absolute");
    }
    std::error_code error;
    const fs::path canonical = fs::canonical(supplied, error);
    if (error || !fs::is_regular_file(canonical, error) || error) {
        throw Error(ErrorCode::InputNotFound, "input file does not exist: " + value);
    }
    if (!HasAllowedExtension(canonical, allowedExtensions)) {
        throw Error(
            ErrorCode::UnsupportedExtension,
            "input extension must be one of: " + AllowedExtensionsText(allowedExtensions));
    }
    return canonical;
}

struct PreparedOutput {
    fs::path target;
    bool targetExisted = false;
};

PreparedOutput PrepareOutput(
    const OutputTarget& output,
    const std::vector<std::string>& allowedExtensions) {
    if (output.absolutePath.empty()) {
        throw Error(ErrorCode::InvalidPath, "output path is required");
    }
    const fs::path supplied = fs::u8path(output.absolutePath);
    if (!supplied.is_absolute() || supplied.filename().empty()) {
        throw Error(ErrorCode::InvalidPath, "output path must be an absolute file path");
    }
    if (!HasAllowedExtension(supplied, allowedExtensions)) {
        throw Error(
            ErrorCode::UnsupportedExtension,
            "output extension must be one of: " + AllowedExtensionsText(allowedExtensions));
    }

    std::error_code error;
    const fs::path parent = fs::canonical(supplied.parent_path(), error);
    if (error || !fs::is_directory(parent, error) || error) {
        throw Error(ErrorCode::InvalidPath, "output parent directory does not exist");
    }
    const fs::path target = (parent / supplied.filename()).lexically_normal();
    const bool exists = fs::exists(target, error);
    if (error) {
        throw Error(ErrorCode::InvalidPath, "could not inspect output path");
    }
    if (exists && !fs::is_regular_file(target, error)) {
        throw Error(ErrorCode::InvalidPath, "output path exists and is not a regular file");
    }
    if (exists && output.overwritePolicy == OverwritePolicy::FailIfExists) {
        throw Error(ErrorCode::OutputExists, "output file already exists");
    }
    if (exists && output.overwritePolicy == OverwritePolicy::Replace &&
        output.replaceConfirmation != kReplaceConfirmation) {
        throw Error(
            ErrorCode::ReplaceConfirmationRequired,
            "replacing an existing file requires replaceConfirmation='REPLACE_EXISTING_FILE'");
    }
    return {target, exists};
}

fs::path UniqueSibling(const fs::path& target, const std::string& tag) {
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    for (unsigned int attempt = 0; attempt < 100u; ++attempt) {
        const std::string name = "." + PathText(target.stem()) + ".vw-mcp-" + tag + "-" +
            std::to_string(stamp) + "-" + std::to_string(attempt) + PathText(target.extension());
        const fs::path candidate = target.parent_path() / fs::u8path(name);
        std::error_code error;
        if (!fs::exists(candidate, error) && !error) {
            return candidate;
        }
    }
    throw Error(ErrorCode::InvalidPath, "could not reserve a staging path beside the output file");
}

class ScopedPathCleanup final {
public:
    explicit ScopedPathCleanup(fs::path path) : path_(std::move(path)) {}

    ~ScopedPathCleanup() {
        if (!released_) {
            std::error_code ignored;
            fs::remove_all(path_, ignored);
        }
    }

    void Release() noexcept {
        released_ = true;
    }

private:
    fs::path path_;
    bool released_ = false;
};

void VerifyProducedFile(const fs::path& path) {
    std::error_code error;
    if (!fs::is_regular_file(path, error) || error) {
        throw Error(ErrorCode::OutputVerificationFailed, "SDK export did not create the requested file");
    }
    if (fs::file_size(path, error) == 0u || error) {
        throw Error(ErrorCode::OutputVerificationFailed, "SDK export created an empty file");
    }
}

void CommitStagedFile(const fs::path& staged, const PreparedOutput& output) {
    VerifyProducedFile(staged);
    std::error_code error;
    if (!output.targetExisted) {
        fs::rename(staged, output.target, error);
        if (error) {
            throw Error(ErrorCode::SdkOperationFailed, "could not move staged export to its requested path");
        }
        return;
    }

    const fs::path backup = UniqueSibling(output.target, "backup");
    fs::rename(output.target, backup, error);
    if (error) {
        throw Error(ErrorCode::SdkOperationFailed, "could not preserve the existing output before replacement");
    }
    bool committed = false;
    try {
        fs::rename(staged, output.target, error);
        if (error) {
            throw Error(ErrorCode::SdkOperationFailed, "could not replace the existing output file");
        }
        committed = true;
    } catch (...) {
        std::error_code rollbackError;
        fs::rename(backup, output.target, rollbackError);
        throw;
    }
    if (committed) {
        fs::remove(backup, error);
    }
}

Result CompletedResult(
    const std::string& operation,
    const PreparedOutput& output) {
    VerifyProducedFile(output.target);
    std::error_code error;
    const std::uintmax_t size = fs::file_size(output.target, error);
    if (error) {
        throw Error(ErrorCode::OutputVerificationFailed, "could not read exported file size");
    }
    return {operation, PathText(output.target), size, output.targetExisted};
}

std::string ObjectUuid(MCObjectHandle object) {
    if (!gSDK || !object) {
        return {};
    }
    TXString uuid;
    return gSDK->GetObjectUuid(object, uuid) && !uuid.IsEmpty()
        ? uuid.GetStdString()
        : std::string();
}

std::string ObjectName(MCObjectHandle object) {
    if (!gSDK || !object) {
        return {};
    }
    TXString name;
    gSDK->GetObjectName(object, name);
    return name.GetStdString();
}

std::string LayerIdentity(const LayerSnapshot& layer) {
    return layer.uuid.empty()
        ? "name:" + layer.name + "|type:" + std::to_string(layer.actualNodeType)
        : "uuid:" + layer.uuid;
}

bool SameLayerState(const LayerSnapshot& left, const LayerSnapshot& right) {
    return left.uuid == right.uuid &&
        left.name == right.name &&
        left.actualNodeType == right.actualNodeType &&
        left.visible == right.visible;
}

#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK

VectorWorks::Filing::IFileIdentifierPtr FileIdentifier(const fs::path& path) {
    VectorWorks::Filing::IFileIdentifierPtr identifier(VectorWorks::Filing::IID_FileIdentifier);
    if (!identifier) {
        throw Error(
            ErrorCode::InterfaceUnavailable,
            "Vectorworks file identifier interface is unavailable in this runtime/license");
    }
    const VCOMError error = identifier->Set(TXString(PathText(path)));
    if (VCOM_FAILED(error)) {
        throw Error(ErrorCode::InvalidPath, "Vectorworks rejected the canonical file path");
    }
    return identifier;
}

Result ExportImageImpl(const ImageExportRequest& request, const std::string& operation) {
    const PreparedOutput output = PrepareOutput(
        request.output,
        {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"});
    const fs::path staged = UniqueSibling(output.target, "image");
    ScopedPathCleanup cleanup(staged);
    auto identifier = FileIdentifier(staged);
    VectorWorks::SExportImageOptions options;
    options.fUpdateVewports = request.updateViewports;
    options.fResetAllPlugIns = request.resetPlugInObjects;
    options.fExportGeoreferencing = request.exportGeoreferencing;
    if (!gSDK || !gSDK->ExportImage(identifier, options)) {
        throw Error(
            ErrorCode::SdkOperationFailed,
            "ISDK::ExportImage is unavailable or failed; no menu or dialog fallback was attempted");
    }
    CommitStagedFile(staged, output);
    cleanup.Release();
    return CompletedResult(operation, output);
}

class PDFDocument final {
public:
    explicit PDFDocument(VectorWorks::Filing::IExportPDF* exporter) : exporter_(exporter) {}

    ~PDFDocument() {
        if (open_) {
            exporter_->ClosePDFDocument();
        }
    }

    void MarkOpen() noexcept {
        open_ = true;
    }

    VCOMError Close() {
        if (!open_) {
            return kVCOMError_NoError;
        }
        open_ = false;
        return exporter_->ClosePDFDocument();
    }

private:
    VectorWorks::Filing::IExportPDF* exporter_;
    bool open_ = false;
};

#endif

}  // namespace

DocumentMutationSnapshot CaptureDocumentMutationSnapshot() {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    if (!gSDK) {
        throw Error(ErrorCode::InterfaceUnavailable, "Vectorworks SDK is unavailable");
    }

    DocumentMutationSnapshot snapshot;
    VectorWorks::Filing::IFileIdentifierPtr activeFile(VectorWorks::Filing::IID_FileIdentifier);
    bool saved = false;
    if (activeFile && gSDK->GetActiveDocument(&activeFile, saved)) {
        TXString path;
        if (VCOM_SUCCEEDED(activeFile->GetFileFullPath(path))) {
            snapshot.documentPath = path.GetStdString();
        }
    }

    MCObjectHandle activeLayer = gSDK->GetActiveLayer();
    if (!activeLayer) {
        activeLayer = gSDK->GetCurrentLayer();
    }
    snapshot.activeLayerUuid = ObjectUuid(activeLayer);
    snapshot.activeLayerName = ObjectName(activeLayer);

    std::unordered_set<std::string> layerIdentities;
    gSDK->ForEachLayerN([&](MCObjectHandle layer) {
        if (!layer) {
            return;
        }
        LayerSnapshot item;
        item.uuid = ObjectUuid(layer);
        item.name = ObjectName(layer);
        item.actualNodeType = gSDK->GetObjectTypeN(layer);
        item.visible = gSDK->IsVisible(layer);
        if (layerIdentities.insert(LayerIdentity(item)).second) {
            snapshot.layers.push_back(std::move(item));
        }
    });
    std::sort(snapshot.layers.begin(), snapshot.layers.end(), [](const LayerSnapshot& left, const LayerSnapshot& right) {
        return LayerIdentity(left) < LayerIdentity(right);
    });

    std::unordered_set<std::string> objectUuids;
    gSDK->ForEachObjectN(
        allObjects + descendIntoAll + descendIntoViewports + descendIntoAuxLists,
        [&](MCObjectHandle object) {
            const std::string uuid = ObjectUuid(object);
            if (!uuid.empty()) {
                objectUuids.insert(uuid);
            }
        });
    snapshot.objectUuids.assign(objectUuids.begin(), objectUuids.end());
    std::sort(snapshot.objectUuids.begin(), snapshot.objectUuids.end());
    return snapshot;
#else
    throw Error(
        ErrorCode::InterfaceUnavailable,
        "document mutation snapshots require an SDK-backed bridge");
#endif
}

DocumentMutationReceipt BuildDocumentMutationReceipt(
    DocumentMutationSnapshot before,
    DocumentMutationSnapshot after) {
    DocumentMutationReceipt receipt;
    receipt.before = std::move(before);
    receipt.after = std::move(after);

    const auto normalizeUuids = [](std::vector<std::string>& values) {
        values.erase(
            std::remove(values.begin(), values.end(), std::string()),
            values.end());
        std::sort(values.begin(), values.end());
        values.erase(std::unique(values.begin(), values.end()), values.end());
    };
    normalizeUuids(receipt.before.objectUuids);
    normalizeUuids(receipt.after.objectUuids);

    std::set_difference(
        receipt.after.objectUuids.begin(),
        receipt.after.objectUuids.end(),
        receipt.before.objectUuids.begin(),
        receipt.before.objectUuids.end(),
        std::back_inserter(receipt.createdObjectUuids));
    std::set_difference(
        receipt.before.objectUuids.begin(),
        receipt.before.objectUuids.end(),
        receipt.after.objectUuids.begin(),
        receipt.after.objectUuids.end(),
        std::back_inserter(receipt.deletedObjectUuids));

    std::unordered_map<std::string, LayerSnapshot> beforeLayers;
    std::unordered_map<std::string, LayerSnapshot> afterLayers;
    for (const auto& layer : receipt.before.layers) {
        beforeLayers.emplace(LayerIdentity(layer), layer);
    }
    for (const auto& layer : receipt.after.layers) {
        afterLayers.emplace(LayerIdentity(layer), layer);
    }
    for (const auto& [identity, layer] : afterLayers) {
        const auto prior = beforeLayers.find(identity);
        if (prior == beforeLayers.end()) {
            receipt.createdLayers.push_back(layer);
        } else if (!SameLayerState(prior->second, layer)) {
            receipt.changedLayers.push_back(layer);
        }
    }
    for (const auto& [identity, layer] : beforeLayers) {
        if (afterLayers.find(identity) == afterLayers.end()) {
            receipt.deletedLayers.push_back(layer);
        }
    }
    const auto sortLayers = [](std::vector<LayerSnapshot>& layers) {
        std::sort(layers.begin(), layers.end(), [](const LayerSnapshot& left, const LayerSnapshot& right) {
            return LayerIdentity(left) < LayerIdentity(right);
        });
    };
    sortLayers(receipt.createdLayers);
    sortLayers(receipt.deletedLayers);
    sortLayers(receipt.changedLayers);

    receipt.activeLayerChanged =
        receipt.before.activeLayerUuid != receipt.after.activeLayerUuid ||
        receipt.before.activeLayerName != receipt.after.activeLayerName;
    receipt.verified =
        !receipt.createdObjectUuids.empty() ||
        !receipt.deletedObjectUuids.empty() ||
        !receipt.createdLayers.empty() ||
        !receipt.deletedLayers.empty() ||
        !receipt.changedLayers.empty() ||
        receipt.activeLayerChanged;
    return receipt;
}

Error::Error(ErrorCode code, const std::string& message) : std::runtime_error(message), code_(code) {}

ErrorCode Error::Code() const noexcept {
    return code_;
}

const char* ErrorCodeName(ErrorCode code) noexcept {
    switch (code) {
        case ErrorCode::InvalidRequest: return "invalid_request";
        case ErrorCode::InvalidPath: return "invalid_path";
        case ErrorCode::UnsupportedExtension: return "unsupported_extension";
        case ErrorCode::InputNotFound: return "input_not_found";
        case ErrorCode::OutputExists: return "output_exists";
        case ErrorCode::ReplaceConfirmationRequired: return "replace_confirmation_required";
        case ErrorCode::InterfaceUnavailable: return "interface_unavailable";
        case ErrorCode::SdkOperationFailed: return "sdk_operation_failed";
        case ErrorCode::OutputVerificationFailed: return "output_verification_failed";
    }
    return "native_io_error";
}

Result ExportImage(const ImageExportRequest& request) {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    return ExportImageImpl(request, "export_image");
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "ISDK::ExportImage requires an SDK-backed bridge");
#endif
}

Result CaptureView(const ImageExportRequest& request) {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    return ExportImageImpl(request, "capture_view");
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "ISDK::ExportImage requires an SDK-backed bridge");
#endif
}

Result ExportPDF(const PDFExportRequest& request) {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    if (request.resolutionDpi < 72 || request.resolutionDpi > 2400) {
        throw Error(ErrorCode::InvalidRequest, "PDF resolution must be between 72 and 2400 DPI");
    }
    const PreparedOutput output = PrepareOutput(request.output, {".pdf"});
    const fs::path staged = UniqueSibling(output.target, "pdf");
    ScopedPathCleanup cleanup(staged);
    auto identifier = FileIdentifier(staged);
    VectorWorks::Filing::IExportPDFPtr exporter(VectorWorks::Filing::IID_ExportPDF);
    if (!exporter) {
        throw Error(
            ErrorCode::InterfaceUnavailable,
            "IExportPDF is unavailable in this Vectorworks runtime/license");
    }

    VectorWorks::Filing::SExportPDFOptions options;
    if (VCOM_FAILED(exporter->GetOptions(options))) {
        throw Error(ErrorCode::SdkOperationFailed, "IExportPDF::GetOptions failed");
    }
    options.fResolution = request.resolutionDpi;
    options.fOpenInViewer = false;
    options.fExportRangeKind = request.currentViewOnly
        ? VectorWorks::Filing::SExportPDFOptions::eCurrentView
        : VectorWorks::Filing::SExportPDFOptions::eAllPages;
    options.fUpdateViewports = request.updateViewports;
    options.fResetPluginObjects = request.resetPlugInObjects;
    options.fRecalculateWorksheets = request.recalculateWorksheets;
    if (VCOM_FAILED(exporter->SetOptions(options))) {
        throw Error(ErrorCode::SdkOperationFailed, "IExportPDF::SetOptions failed");
    }

    PDFDocument document(exporter);
    if (VCOM_FAILED(exporter->OpenPDFDocument(identifier))) {
        throw Error(ErrorCode::SdkOperationFailed, "IExportPDF::OpenPDFDocument failed");
    }
    document.MarkOpen();
    if (VCOM_FAILED(exporter->ExportPDFPages(TXString()))) {
        throw Error(ErrorCode::SdkOperationFailed, "IExportPDF::ExportPDFPages failed");
    }
    if (VCOM_FAILED(document.Close())) {
        throw Error(ErrorCode::SdkOperationFailed, "IExportPDF::ClosePDFDocument failed");
    }
    CommitStagedFile(staged, output);
    cleanup.Release();
    return CompletedResult("export_pdf", output);
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "IExportPDF requires an SDK-backed bridge");
#endif
}

Result ExportVectorworksDocument(const VectorworksExportRequest& request) {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    constexpr short currentFileVersion = static_cast<short>(SDK_VERSION / 100);
    if (request.targetFileVersion <= 0 || request.targetFileVersion > currentFileVersion) {
        throw Error(
            ErrorCode::InvalidRequest,
            "targetFileVersion must be a positive SDK file version no newer than this Vectorworks runtime");
    }
    const PreparedOutput output = PrepareOutput(request.output, {".vwx"});
    const fs::path staged = UniqueSibling(output.target, "vwx");
    ScopedPathCleanup cleanup(staged);
    auto identifier = FileIdentifier(staged);
    if (!gSDK || !gSDK->ExportDocument(identifier, request.targetFileVersion)) {
        throw Error(
            ErrorCode::SdkOperationFailed,
            "ISDK::ExportDocument is unavailable or failed for the requested file version");
    }
    CommitStagedFile(staged, output);
    cleanup.Release();
    return CompletedResult("export_vectorworks_document", output);
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "ISDK::ExportDocument requires an SDK-backed bridge");
#endif
}

Result ImportDWG(const DWGImportRequest& request) {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    const fs::path input = CanonicalExistingInput(request.absolutePath, {".dwg"});
    DocumentMutationSnapshot before = CaptureDocumentMutationSnapshot();
    auto identifier = FileIdentifier(input);
    VectorWorks::Filing::IImportExportDWGPtr importer(VectorWorks::Filing::IID_IImportExportDWG);
    if (!importer) {
        throw Error(
            ErrorCode::InterfaceUnavailable,
            "IImportExportDWG is unavailable in this Vectorworks runtime/license");
    }
    const VCOMError result = importer->Import(
        VectorWorks::Filing::eDWGImportNormal,
        identifier,
        true);
    if (VCOM_FAILED(result)) {
        throw Error(
            ErrorCode::SdkOperationFailed,
            "silent IImportExportDWG import failed; no menu or dialog fallback was attempted");
    }
    DocumentMutationReceipt mutation = BuildDocumentMutationReceipt(
        std::move(before),
        CaptureDocumentMutationSnapshot());
    if (!mutation.verified) {
        throw Error(
            ErrorCode::OutputVerificationFailed,
            "silent DWG import returned success but no document mutation was observed");
    }
    std::error_code error;
    const std::uintmax_t size = fs::file_size(input, error);
    if (error) {
        throw Error(ErrorCode::InputNotFound, "could not read the canonical DWG input file");
    }
    Result completed{"import_dwg", PathText(input), size, false};
    completed.hasDocumentMutationReceipt = true;
    completed.documentMutationReceipt = std::move(mutation);
    return completed;
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "IImportExportDWG requires an SDK-backed bridge");
#endif
}

Result ExportDWG(const DWGExportRequest& request) {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    const PreparedOutput output = PrepareOutput(request.output, {".dwg"});
    VectorWorks::Filing::IImportExportDWGPtr exporter(VectorWorks::Filing::IID_IImportExportDWG);
    if (!exporter) {
        throw Error(
            ErrorCode::InterfaceUnavailable,
            "IImportExportDWG is unavailable in this Vectorworks runtime/license");
    }

    const fs::path stagingDirectory = UniqueSibling(output.target, "dwg-dir").replace_extension();
    ScopedPathCleanup cleanup(stagingDirectory);
    std::error_code error;
    if (!fs::create_directory(stagingDirectory, error) || error) {
        throw Error(ErrorCode::InvalidPath, "could not create a private DWG staging directory");
    }
    VectorWorks::Filing::IFolderIdentifierPtr folder(VectorWorks::Filing::IID_FolderIdentifier);
    if (!folder || VCOM_FAILED(folder->Set(TXString(PathText(stagingDirectory))))) {
        throw Error(ErrorCode::InterfaceUnavailable, "Vectorworks folder identifier is unavailable");
    }

    VectorWorks::Filing::SExportOptionsForPublish options{};
    if (VCOM_FAILED(exporter->InitExportOptions(VectorWorks::Filing::eExportDWGDXF, &options))) {
        throw Error(ErrorCode::SdkOperationFailed, "IImportExportDWG::InitExportOptions failed");
    }
    options.fCustomLocation = true;
    options.fDefaultLocationPath = TXString(PathText(stagingDirectory));
    options.fFilesInSubFolder = false;
    options.fCreateSeparateFolder = false;
    options.fCreateXRefsForDLVPs = false;
    options.fCreateFilesForLayers = false;
    options.fUpdateViewports = request.updateViewports;
    options.fResetAllPluginObjects = request.resetPlugInObjects;
    options.fRecalculateWS = request.recalculateWorksheets;

    const TXString displayName(PathText(output.target.stem()));
    const VCOMError exportResult = exporter->Export(
        static_cast<short>(VectorWorks::Filing::eExportDWGDXF),
        &folder,
        displayName,
        &options,
        true);
    if (VCOM_FAILED(exportResult)) {
        throw Error(
            ErrorCode::SdkOperationFailed,
            "silent IImportExportDWG export failed; no menu or dialog fallback was attempted");
    }

    std::vector<fs::path> produced;
    for (const auto& entry : fs::recursive_directory_iterator(stagingDirectory, error)) {
        if (error) {
            break;
        }
        if (entry.is_regular_file() && Lower(PathText(entry.path().extension())) == ".dwg") {
            produced.push_back(entry.path());
        }
    }
    if (error || produced.size() != 1u) {
        throw Error(
            ErrorCode::OutputVerificationFailed,
            "silent DWG export did not produce exactly one verifiable DWG file");
    }
    CommitStagedFile(produced.front(), output);
    return CompletedResult("export_dwg", output);
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "IImportExportDWG requires an SDK-backed bridge");
#endif
}

}  // namespace VectorworksMCP::NativeIO
