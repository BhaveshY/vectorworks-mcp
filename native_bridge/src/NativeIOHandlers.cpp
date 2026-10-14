#include "StdAfx.h"

#include "NativeIOHandlers.hpp"
#include "DialogWatch.hpp"
#include "ImageTranscode.hpp"
#include "ScriptEngine.hpp"
#include "WallInserts.hpp"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdio>
#include <fstream>
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
#include "Interfaces/VectorWorks/Extension/IIFCSupport.h"
#else
#define VECTORWORKS_MCP_NATIVE_IO_HAS_SDK 0
#endif

namespace VectorworksMCP::NativeIO {

namespace {

namespace fs = std::filesystem;

constexpr const char* kReplaceConfirmation = "REPLACE_EXISTING_FILE";

#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
#if SDK_VERSION >= 3000
constexpr auto kDwgExportMode = VectorWorks::Filing::EExportMode::eDWGDXF;
#else
constexpr auto kDwgExportMode = VectorWorks::Filing::eExportDWGDXF;
#endif
#endif

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
            "replacing an existing file requires options replace=true and replace_confirmation='REPLACE_EXISTING_FILE'");
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

// "png" | "jpeg" | "bmp" | "tiff" | "" from the file's magic bytes.
std::string ImageContainer(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    unsigned char head[8] = {};
    input.read(reinterpret_cast<char*>(head), sizeof(head));
    const std::streamsize got = input.gcount();
    if (got >= 8 && head[0] == 0x89 && head[1] == 'P' && head[2] == 'N' && head[3] == 'G') return "png";
    if (got >= 3 && head[0] == 0xFF && head[1] == 0xD8 && head[2] == 0xFF) return "jpeg";
    if (got >= 2 && head[0] == 'B' && head[1] == 'M') return "bmp";
    if (got >= 4 && ((head[0] == 'I' && head[1] == 'I') || (head[0] == 'M' && head[1] == 'M'))) return "tiff";
    return std::string();
}

std::string ExpectedContainer(const fs::path& path) {
    const std::string extension = Lower(PathText(path.extension()));
    if (extension == ".png") return "png";
    if (extension == ".jpg" || extension == ".jpeg") return "jpeg";
    if (extension == ".bmp") return "bmp";
    if (extension == ".tif" || extension == ".tiff") return "tiff";
    return std::string();
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
    if (request.hasExportRect) {
        options.fExportRect = ViewRect(request.rectLeft, request.rectTop, request.rectRight, request.rectBottom);
    }
    if (!gSDK || !gSDK->ExportImage(identifier, options)) {
        throw Error(
            ErrorCode::SdkOperationFailed,
            "ISDK::ExportImage is unavailable or failed; no menu or dialog fallback was attempted");
    }
    // Make the file's content match its extension (ExportImage writes JPEG).
    const std::string written = ImageContainer(staged);
    const std::string expected = ExpectedContainer(output.target);
    std::string encoded = written;
    std::string extra;
    if (!written.empty() && !expected.empty() && written != expected) {
        std::string transcodeError;
        bool transcoded = false;
        // ISDK::ExportImage writes JPEG data whatever the extension (live
        // 2026-10-14: every ".png" capture was JPEG), which blurs plan lines.
        {
        const fs::path converted = UniqueSibling(output.target, "image-converted");
        ScopedPathCleanup convertedCleanup(converted);
        if (ImageTranscode::TranscodeFile(staged.wstring(), converted.wstring(), expected, transcodeError) &&
            ImageContainer(converted) == expected) {
            std::error_code error;
            fs::remove(staged, error);
            fs::rename(converted, staged, error);
            transcoded = !error;
            if (error) transcodeError = "could not replace the staged image";
        }
        }
        if (transcoded) {
            encoded = expected;
            extra = ",\"transcoded_from\":\"" + written + "\"";
        } else {
            extra = ",\"transcode_error\":\"" + transcodeError + "\"";
        }
    }
    CommitStagedFile(staged, output);
    cleanup.Release();
    Result completed = CompletedResult(operation, output);
    completed.extraJson = ",\"encoded_format\":\"" + (encoded.empty() ? std::string("unknown") : encoded) + "\"" + extra;
    return completed;
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
            const auto add = [&](MCObjectHandle candidate) {
                const std::string uuid = ObjectUuid(candidate);
                if (!uuid.empty()) {
                    objectUuids.insert(uuid);
                }
            };
            add(object);
            WallInserts::ForEachInsert(object, add);  // doors/windows in walls
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
    const std::string range = Lower(request.range);
    const bool sheetRange = range == "sheet";
    const bool currentView = request.currentViewOnly || range == "current_view" || sheetRange;
    if (!range.empty() && range != "current_view" && range != "all_pages" && range != "sheet") {
        throw Error(ErrorCode::InvalidRequest, "range must be current_view, all_pages or sheet");
    }
    options.fExportRangeKind = currentView
        ? VectorWorks::Filing::SExportPDFOptions::eCurrentView
        : VectorWorks::Filing::SExportPDFOptions::eAllPages;
    MCObjectHandle previousLayer = nullptr;
    if (sheetRange) {
        if (request.sheet.empty()) {
            throw Error(ErrorCode::InvalidRequest, "range=sheet requires sheet (sheet layer name)");
        }
        MCObjectHandle sheetLayer = nullptr;
        gSDK->ForEachLayerN([&](MCObjectHandle layer) {
            if (sheetLayer || !layer) return;
            TXString name;
            gSDK->GetObjectName(layer, name);
            if (name.GetStdString() == request.sheet) sheetLayer = layer;
        });
        if (!sheetLayer) {
            throw Error(ErrorCode::InputNotFound, "sheet layer was not found: " + request.sheet);
        }
        previousLayer = gSDK->GetActiveLayer();
        gSDK->SetCurrentLayerN(sheetLayer);
    }
    struct RestoreLayer {
        MCObjectHandle layer;
        ~RestoreLayer() {
            if (layer && gSDK && gSDK->GetActiveLayer() != layer) gSDK->SetCurrentLayerN(layer);
        }
    } restoreLayer{previousLayer};
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
    if (VCOM_FAILED(exporter->InitExportOptions(kDwgExportMode, &options))) {
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
        static_cast<short>(kDwgExportMode),
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

#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
namespace {

std::int64_t IfcNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// "step" (ISO-10303-21 .ifc), "zip" (.ifczip) or "xml" (.ifcxml); "" when
// the file is missing, empty or not recognisably IFC.
std::string IfcFileKind(const fs::path& path) {
    std::error_code error;
    if (!fs::is_regular_file(path, error) || error) return std::string();
    if (fs::file_size(path, error) == 0u || error) return std::string();
    std::ifstream input(path, std::ios::binary);
    char head[64] = {};
    input.read(head, sizeof(head));
    const std::string text(head, static_cast<std::size_t>(input.gcount()));
    std::size_t start = 0;
    if (text.compare(0, 3, "\xEF\xBB\xBF") == 0) start = 3;
    while (start < text.size() && std::isspace(static_cast<unsigned char>(text[start]))) ++start;
    if (text.compare(start, 12, "ISO-10303-21") == 0) return "step";
    if (text.compare(0, 2, "PK") == 0) return "zip";
    if (start < text.size() && text[start] == '<') return "xml";
    return std::string();
}

std::string JsonEscape(const std::string& text) {
    std::string out = "\"";
    for (const unsigned char ch : text) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch < 0x20) {
                    char buffer[8];
                    std::snprintf(buffer, sizeof(buffer), "\\u%04x", ch);
                    out += buffer;
                } else {
                    out += static_cast<char>(ch);
                }
        }
    }
    return out + "\"";
}

std::string JoinJson(const std::vector<std::string>& items) {
    std::string json = "[";
    for (std::size_t index = 0; index < items.size(); ++index) {
        if (index) json += ",";
        json += items[index];
    }
    return json + "]";
}

std::string IfcMethod(const IFCRequest& request) {
    const std::string method = Lower(request.method.empty() ? std::string("auto") : request.method);
    if (method != "auto" && method != "sdk" && method != "script") {
        throw Error(ErrorCode::InvalidRequest, "method must be auto, sdk or script");
    }
    return method;
}

bool InternalScriptUsable(std::string& reason) {
    if (!Scripting::ScriptingAllowed()) {
        reason = "script_disabled";
        return false;
    }
    if (Scripting::ScriptEnginePoisoned()) {
        reason = "script_engine_unavailable";
        return false;
    }
    return true;
}

}  // namespace
#endif

// IFC export (findings #17 and 2026-10-14 live run, VW 2024 29.0.8 Architect):
// IIFCSupport::ExportFile(staged, bShowUI=false) returned VCOM 1 in ~240 ms
// and showed the modal "IFC Export Report" (acknowledged by DialogWatch, which
// now reads the report grid rows before pressing OK); vs.IFC_ExportNoUI
// returned False in ~30 ms; no file anywhere, untitled or saved documents,
// plain or dotted staging names.
//
// Every attempt is recorded. Sequence per schema (request.schema, default
// IFC2x3 then IFC4): SetIFCScheme + UpdateVWDocumentIFCData, then
// ExportFile(bShowUI=false), then IIFCSupport::IFC_ExportNoUI (the native
// entry point vs.IFC_ExportNoUI wraps, VW 2021+). Method "auto" finally tries
// vs.IFC_ExportNoUI through the internal Python engine. The produced file, not
// the VCOM status, decides success. The output is written straight to the
// requested path when it does not exist yet (removed again on failure),
// otherwise to a plain sibling "<stem>.vw-mcp-ifc-<n>.ifc" (no leading dot)
// that replaces it. The document's original IFC schema is restored.
namespace {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
fs::path PlainIfcSibling(const fs::path& target) {
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    for (unsigned int attempt = 0; attempt < 100u; ++attempt) {
        const std::string name = PathText(target.stem()) + ".vw-mcp-ifc-" + std::to_string(stamp % 1000000000LL) +
            "-" + std::to_string(attempt) + PathText(target.extension());
        const fs::path candidate = target.parent_path() / fs::u8path(name);
        std::error_code error;
        if (!fs::exists(candidate, error) && !error) return candidate;
    }
    throw Error(ErrorCode::InvalidPath, "could not reserve a staging path beside the output file");
}

const char* IfcSchemaName(VectorWorks::IFCLib::EIFCSupportSchema schema) {
    switch (schema) {
        case VectorWorks::IFCLib::kIFCSupportScheme_2x2: return "IFC2x2";
        case VectorWorks::IFCLib::kIFCSupportScheme_2x3: return "IFC2x3";
        case VectorWorks::IFCLib::kIFCSupportScheme_2x4: return "IFC4";
#if SDK_VERSION >= 3000
        case VectorWorks::IFCLib::kIFCSupportScheme_4x3: return "IFC4x3";
#endif
    }
    return "unknown";
}

std::vector<VectorWorks::IFCLib::EIFCSupportSchema> IfcSchemas(const std::string& requested) {
    using namespace VectorWorks::IFCLib;
    std::string value = Lower(requested);
    value.erase(std::remove_if(value.begin(), value.end(), [](char ch) { return ch == ' ' || ch == '_' || ch == '.'; }),
                value.end());
    if (value.empty() || value == "auto") return {kIFCSupportScheme_2x3, kIFCSupportScheme_2x4};
    if (value == "ifc2x3" || value == "2x3") return {kIFCSupportScheme_2x3};
    if (value == "ifc4" || value == "4" || value == "ifc2x4" || value == "2x4") return {kIFCSupportScheme_2x4};
#if SDK_VERSION >= 3000
    if (value == "ifc4x3" || value == "4x3") return {kIFCSupportScheme_4x3};
#endif
    throw Error(ErrorCode::InvalidRequest, "schema must be auto, ifc2x3 or ifc4");
}

std::string HexStatus(VCOMError status) {
    return std::to_string(static_cast<long long>(status));
}
#endif
}  // namespace

Result ExportIFC(const IFCRequest& request) {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    using namespace VectorWorks::IFCLib;
    const std::string method = IfcMethod(request);
    const std::vector<EIFCSupportSchema> schemas = IfcSchemas(request.schema);
    const PreparedOutput output = PrepareOutput(request.output, {".ifc", ".ifczip", ".ifcxml"});
    const bool direct = !output.targetExisted;
    const fs::path staged = direct ? output.target : PlainIfcSibling(output.target);
    ScopedPathCleanup cleanup(staged);
    const std::uint64_t dialogSeq = DialogWatch::DismissSequence();
    std::vector<std::string> attempts;
    std::string usedMethod;
    std::string usedSchema;
    std::string fileKind;
    std::string setupJson = "{}";
    const auto reportSince = [](std::uint64_t seq) { return DialogWatch::HandlerReportTextSince(seq, 1500); };
    const auto clearStaged = [&]() {
        std::error_code ignored;
        fs::remove(staged, ignored);
    };
    if (method != "script") {
        VCOMPtr<IIFCSupport> ifc(IID_IFCSupport);
        if (!ifc) {
            attempts.push_back("{\"method\":\"sdk:IIFCSupport\",\"ok\":false,\"error\":\"interface_unavailable\"}");
        } else {
            // Live 2026-10-14: SetIFCScheme / IFC_SetIFCScheme / UpdateVWDocumentIFCData /
            // IFC_ExportHeaderAsJson never made the export work, and after a document that
            // ran them was closed, the next IFC export in another document crashed
            // Vectorworks (0xC0000005). Only the export entry points are called now.
            setupJson = "{}";
            auto identifier = FileIdentifier(staged);
            for (const EIFCSupportSchema schema : {schemas.front()}) {  // the document's own settings
                if (!usedMethod.empty()) break;
                (void) schema;
                const std::string common;
                // 1) ExportFile without UI: disabled. Live 2026-10-14 a successful
                //    ExportFile was followed by a Vectorworks crash (0xC0000005) at the
                //    next IFC export in another document; IFC_ExportNoUI below writes
                //    the same file.
                if (method == "exportfile") {
                    clearStaged();
                    const std::uint64_t seq = DialogWatch::DismissSequence();
                    const std::int64_t started = IfcNowMs();
                    const VCOMError status = ifc->ExportFile(identifier, false);
                    const std::int64_t elapsed = IfcNowMs() - started;
                    fileKind = IfcFileKind(staged);
                    std::string attempt = "{\"method\":\"sdk:IIFCSupport::ExportFile\",\"ok\":" +
                        std::string(fileKind.empty() ? "false" : "true") + common +
                        ",\"vcom_result\":" + HexStatus(status) +
                        ",\"sdk_reported_success\":" + (VCOM_FAILED(status) ? "false" : "true") +
                        ",\"file_produced\":" + (fileKind.empty() ? "false" : "true") +
                        ",\"elapsed_ms\":" + std::to_string(elapsed);
                    const std::string report = reportSince(seq);
                    if (!report.empty()) attempt += ",\"report\":" + JsonEscape(report);
                    attempts.push_back(attempt + "}");
                    if (!fileKind.empty()) {
                        usedMethod = "sdk:IIFCSupport::ExportFile";
                        usedSchema = IfcSchemaName(schema);
                        break;
                    }
                }
                // 2) IIFCSupport::IFC_ExportNoUI (same entry point as vs.IFC_ExportNoUI).
                {
                    clearStaged();
                    const std::uint64_t seq = DialogWatch::DismissSequence();
                    const std::int64_t started = IfcNowMs();
                    const VCOMError status = ifc->IFC_ExportNoUI(identifier);
                    const std::int64_t elapsed = IfcNowMs() - started;
                    fileKind = IfcFileKind(staged);
                    std::string attempt = "{\"method\":\"sdk:IIFCSupport::IFC_ExportNoUI\",\"ok\":" +
                        std::string(fileKind.empty() ? "false" : "true") + common +
                        ",\"vcom_result\":" + HexStatus(status) +
                        ",\"file_produced\":" + (fileKind.empty() ? "false" : "true") +
                        ",\"elapsed_ms\":" + std::to_string(elapsed);
                    const std::string report = reportSince(seq);
                    if (!report.empty()) attempt += ",\"report\":" + JsonEscape(report);
                    attempts.push_back(attempt + "}");
                    if (!fileKind.empty()) {
                        usedMethod = "sdk:IIFCSupport::IFC_ExportNoUI";
                        usedSchema = IfcSchemaName(schema);
                    }
                }
            }
        }
    }
    if (usedMethod.empty() && method != "sdk") {
        std::string reason;
        if (!InternalScriptUsable(reason)) {
            attempts.push_back("{\"method\":\"script:vs.IFC_ExportNoUI\",\"ok\":false,\"skipped\":" + JsonEscape(reason) + "}");
        } else {
            clearStaged();  // never accept a partial file of an earlier attempt
            const std::int64_t started = IfcNowMs();
            std::string scriptResult;
            std::string scriptError;
            try {
                scriptResult = Scripting::RunInternalPython(
                    "result = bool(vs.IFC_ExportNoUI(" + Scripting::PythonStringLiteral(PathText(staged)) + "))\n");
            } catch (const std::exception& error) {
                scriptError = error.what();
            }
            const std::int64_t elapsed = IfcNowMs() - started;
            fileKind = IfcFileKind(staged);
            std::string attempt = "{\"method\":\"script:vs.IFC_ExportNoUI\",\"ok\":" +
                std::string(fileKind.empty() ? "false" : "true") +
                ",\"returned\":" + (scriptResult.empty() ? std::string("null") : scriptResult) +
                ",\"file_produced\":" + (fileKind.empty() ? "false" : "true") +
                ",\"elapsed_ms\":" + std::to_string(elapsed);
            if (!scriptError.empty()) attempt += ",\"error\":" + JsonEscape(scriptError.substr(0, 600));
            attempts.push_back(attempt + "}");
            if (!fileKind.empty()) usedMethod = "script:vs.IFC_ExportNoUI";
        }
    }
    if (usedMethod.empty()) {
        clearStaged();
        const std::string report = reportSince(dialogSeq);
        throw Error(
            ErrorCode::SdkOperationFailed,
            "IFC export produced no IFC file (no dialog fallback was attempted)" +
            (report.empty() ? std::string("; no IFC report dialog was shown")
                            : "; IFC Export Report: " + report) +
            "; setup: " + setupJson + "; attempts: " + JoinJson(attempts));
    }
    if (!direct) CommitStagedFile(staged, output);
    cleanup.Release();
    Result completed = CompletedResult("export_ifc", output);
    completed.extraJson = ",\"method\":" + JsonEscape(usedMethod) + ",\"ifc_format\":" + JsonEscape(fileKind) +
        (usedSchema.empty() ? std::string() : ",\"schema\":" + JsonEscape(usedSchema)) +
        ",\"setup\":" + setupJson + ",\"attempts\":" + JoinJson(attempts);
    return completed;
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "IIFCSupport requires an SDK-backed bridge");
#endif
}

Result ImportIFC(const IFCRequest& request) {
#if VECTORWORKS_MCP_NATIVE_IO_HAS_SDK
    const std::string method = IfcMethod(request);
    const fs::path input = CanonicalExistingInput(request.absolutePath, {".ifc", ".ifczip", ".ifcxml"});
    DocumentMutationSnapshot before = CaptureDocumentMutationSnapshot();
    std::vector<std::string> attempts;
    std::string usedMethod;
    DocumentMutationReceipt mutation;
    bool haveMutation = false;
    if (method != "script") {
        VCOMPtr<VectorWorks::IFCLib::IIFCSupport> ifc(VectorWorks::IFCLib::IID_IFCSupport);
        if (!ifc) {
            attempts.push_back("{\"method\":\"sdk:IIFCSupport::ImportFile\",\"ok\":false,\"error\":\"interface_unavailable\"}");
        } else {
            auto identifier = FileIdentifier(input);
            const std::int64_t started = IfcNowMs();
            const VCOMError status = ifc->ImportFile(identifier, false);
            const std::int64_t elapsed = IfcNowMs() - started;
            mutation = BuildDocumentMutationReceipt(before, CaptureDocumentMutationSnapshot());
            haveMutation = true;
            const bool ok = !VCOM_FAILED(status) || mutation.verified;
            attempts.push_back("{\"method\":\"sdk:IIFCSupport::ImportFile\",\"ok\":" + std::string(ok ? "true" : "false") +
                               ",\"vcom_result\":" + std::to_string(static_cast<long long>(status)) +
                               ",\"sdk_reported_success\":" + (VCOM_FAILED(status) ? "false" : "true") +
                               ",\"document_changed\":" + (mutation.verified ? "true" : "false") +
                               ",\"elapsed_ms\":" + std::to_string(elapsed) + "}");
            if (ok) usedMethod = "sdk:IIFCSupport::ImportFile";
        }
    }
    if (usedMethod.empty() && method != "sdk") {
        std::string reason;
        if (!InternalScriptUsable(reason)) {
            attempts.push_back("{\"method\":\"script:vs.IFC_ImportNoUI\",\"ok\":false,\"skipped\":" + JsonEscape(reason) + "}");
        } else {
            const std::int64_t started = IfcNowMs();
            std::string scriptError;
            try {
                Scripting::RunInternalPython(
                    "vs.IFC_ImportNoUI(" + Scripting::PythonStringLiteral(PathText(input)) + ")\nresult = True\n");
            } catch (const std::exception& error) {
                scriptError = error.what();
            }
            const std::int64_t elapsed = IfcNowMs() - started;
            mutation = BuildDocumentMutationReceipt(before, CaptureDocumentMutationSnapshot());
            haveMutation = true;
            std::string attempt = "{\"method\":\"script:vs.IFC_ImportNoUI\",\"ok\":" +
                std::string(mutation.verified ? "true" : "false") +
                ",\"document_changed\":" + (mutation.verified ? "true" : "false") +
                ",\"elapsed_ms\":" + std::to_string(elapsed);
            if (!scriptError.empty()) attempt += ",\"error\":" + JsonEscape(scriptError.substr(0, 600));
            attempts.push_back(attempt + "}");
            if (mutation.verified) usedMethod = "script:vs.IFC_ImportNoUI";
        }
    }
    if (usedMethod.empty()) {
        throw Error(
            ErrorCode::SdkOperationFailed,
            "IFC import failed (no dialog fallback was attempted); attempts: " + JoinJson(attempts) +
            "; see dismissed_dialogs for any IFC report text");
    }
    if (!haveMutation) mutation = BuildDocumentMutationReceipt(std::move(before), CaptureDocumentMutationSnapshot());
    std::error_code error;
    const std::uintmax_t size = fs::file_size(input, error);
    Result completed{"import_ifc", PathText(input), error ? 0u : size, false};
    completed.hasDocumentMutationReceipt = true;
    completed.documentMutationReceipt = std::move(mutation);
    completed.extraJson = ",\"method\":" + JsonEscape(usedMethod) + ",\"attempts\":" + JoinJson(attempts);
    if (!completed.documentMutationReceipt.verified) {
        completed.extraJson += ",\"warnings\":[\"the IFC import reported success but no document change was observed\"]";
    }
    return completed;
#else
    (void) request;
    throw Error(ErrorCode::InterfaceUnavailable, "IIFCSupport requires an SDK-backed bridge");
#endif
}

}  // namespace VectorworksMCP::NativeIO
