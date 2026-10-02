#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace VectorworksMCP::NativeIO {

enum class ErrorCode {
    InvalidRequest,
    InvalidPath,
    UnsupportedExtension,
    InputNotFound,
    OutputExists,
    ReplaceConfirmationRequired,
    InterfaceUnavailable,
    SdkOperationFailed,
    OutputVerificationFailed,
};

class Error final : public std::runtime_error {
public:
    Error(ErrorCode code, const std::string& message);

    ErrorCode Code() const noexcept;

private:
    ErrorCode code_;
};

enum class OverwritePolicy {
    FailIfExists,
    Replace,
};

struct OutputTarget {
    std::string absolutePath;
    OverwritePolicy overwritePolicy = OverwritePolicy::FailIfExists;
    std::string replaceConfirmation;
};

struct ImageExportRequest {
    OutputTarget output;
    // 1.0 defaults: no viewport update / plug-in reset during capture/export.
    bool updateViewports = false;
    bool resetPlugInObjects = false;
    bool exportGeoreferencing = false;
    // Optional export rectangle in view (screen pixel) coordinates.
    bool hasExportRect = false;
    short rectLeft = 0;
    short rectTop = 0;
    short rectRight = 0;
    short rectBottom = 0;
};

struct PDFExportRequest {
    OutputTarget output;
    bool currentViewOnly = false;
    std::int32_t resolutionDpi = 300;
    bool updateViewports = false;
    bool resetPlugInObjects = false;
    bool recalculateWorksheets = true;
    // 1.0: "current_view" | "all_pages" | "sheet" (sheet = activate the sheet
    // layer named by `sheet`, export its current view, restore the layer).
    std::string range;
    std::string sheet;
};

struct IFCRequest {
    std::string absolutePath;
    OutputTarget output;
};

struct VectorworksExportRequest {
    OutputTarget output;
    short targetFileVersion = 0;
};

struct DWGImportRequest {
    std::string absolutePath;
};

struct DWGExportRequest {
    OutputTarget output;
    bool updateViewports = false;
    bool resetPlugInObjects = false;
    bool recalculateWorksheets = true;
};

struct LayerSnapshot {
    std::string uuid;
    std::string name;
    short actualNodeType = 0;
    bool visible = false;
};

struct DocumentMutationSnapshot {
    std::string documentPath;
    std::string activeLayerUuid;
    std::string activeLayerName;
    std::vector<LayerSnapshot> layers;
    std::vector<std::string> objectUuids;
};

struct DocumentMutationReceipt {
    DocumentMutationSnapshot before;
    DocumentMutationSnapshot after;
    std::vector<std::string> createdObjectUuids;
    std::vector<std::string> deletedObjectUuids;
    std::vector<LayerSnapshot> createdLayers;
    std::vector<LayerSnapshot> deletedLayers;
    std::vector<LayerSnapshot> changedLayers;
    bool activeLayerChanged = false;
    bool verified = false;
};

struct Result {
    std::string operation;
    std::string canonicalPath;
    std::uintmax_t sizeBytes = 0;
    bool replacedExisting = false;
    bool hasDocumentMutationReceipt = false;
    DocumentMutationReceipt documentMutationReceipt;
};

DocumentMutationSnapshot CaptureDocumentMutationSnapshot();
DocumentMutationReceipt BuildDocumentMutationReceipt(
    DocumentMutationSnapshot before,
    DocumentMutationSnapshot after);

Result ExportImage(const ImageExportRequest& request);
Result CaptureView(const ImageExportRequest& request);
Result ExportPDF(const PDFExportRequest& request);
Result ExportVectorworksDocument(const VectorworksExportRequest& request);
Result ImportDWG(const DWGImportRequest& request);
Result ExportDWG(const DWGExportRequest& request);
Result ExportIFC(const IFCRequest& request);
Result ImportIFC(const IFCRequest& request);

const char* ErrorCodeName(ErrorCode code) noexcept;

}  // namespace VectorworksMCP::NativeIO
