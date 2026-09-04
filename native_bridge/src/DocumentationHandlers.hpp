#pragma once

#include "VectorworksSDK.h"
#include "NativeTransaction.hpp"

#include <cstdint>
#include <string>
#include <vector>

namespace VectorworksMCP::Documentation {

struct DocumentBinding {
    std::string filePath;
    std::string fileName;
    std::string documentFingerprint;
    std::uint64_t documentGeneration = 0;
    std::string bridgeSessionId;
    std::string activeLayerUuid;
    std::string activeLayerName;
    bool dirty = false;
};

struct ExpectedTargetBinding {
    std::string filePath;
    std::string documentFingerprint;
    std::uint64_t documentGeneration = 0;
    std::string bridgeSessionId;
    std::string activeLayerUuid;
    std::string activeLayerName;
    bool dirty = false;
    bool hasDirty = false;
};

struct VisibilityEntry {
    std::string identity;
    short visibility = 0;
};

enum class OperationKind {
    CreateSheetLayer,
    UpdateSheetLayer,
    DeleteSheetLayer,
    CreateViewport,
    UpdateViewport,
    DeleteViewport,
    CreateViewportAnnotation,
    UpdateViewportAnnotation,
    DeleteViewportAnnotation,
};

struct Operation {
    OperationKind kind = OperationKind::CreateSheetLayer;
    std::string localRef;
    std::string targetRef;
    std::string sheetLayerRef;
    std::string viewportRef;

    std::string name;
    std::string title;
    std::string description;
    std::string className;
    std::string annotationKind;
    std::string text;
    std::string confirmation;

    bool hasName = false;
    bool hasTitle = false;
    bool hasDescription = false;
    bool hasClassName = false;
    bool hasText = false;
    bool hasVisibility = false;
    short visibility = 0;
    bool hasDpi = false;
    short dpi = 72;
    bool hasSheetWidthMm = false;
    bool hasSheetHeightMm = false;
    double sheetWidthMm = 0.0;
    double sheetHeightMm = 0.0;

    bool hasScale = false;
    double scale = 1.0;
    bool hasPlacement = false;
    double x = 0.0;
    double y = 0.0;
    bool hasProjectionType = false;
    short projectionType = 0;
    bool hasViewType = false;
    short viewType = 0;
    bool hasRenderType = false;
    short renderType = 0;
    bool hasForegroundRenderType = false;
    short foregroundRenderType = 0;
    bool replaceSourceLayers = false;
    bool replaceSourceClasses = false;
    bool hasSourceLayers = false;
    bool hasSourceClasses = false;
    std::vector<VisibilityEntry> sourceLayers;
    std::vector<VisibilityEntry> sourceClasses;
    bool hasCrop = false;
    bool clearCrop = false;
    std::vector<WorldPt> cropPoints;

    bool hasDelta = false;
    double deltaX = 0.0;
    double deltaY = 0.0;
    double x1 = 0.0;
    double y1 = 0.0;
    double x2 = 0.0;
    double y2 = 0.0;
    double offset = 0.0;
    double textOffset = 0.0;
    short dimensionType = 0;
    short markerStyle = 0;
    short markerSize = 0;
    short markerAngle = 0;
    std::vector<WorldPt> points;
};

std::string BridgeSessionId();
DocumentBinding ReadDocumentBinding(VectorWorks::ISDK& sdk);
std::string DocumentBindingJson(const DocumentBinding& binding);
void ValidateTargetBinding(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected);

std::string ReadSheetLayers(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected,
    std::size_t offset,
    std::size_t limit);
std::string ReadViewports(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected,
    const std::string& sheetLayerUuid,
    std::size_t offset,
    std::size_t limit);
std::string ReadViewportAnnotations(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected,
    const std::string& sheetLayerUuid,
    const std::string& viewportUuid,
    std::size_t offset,
    std::size_t limit);

std::string ApplyOperations(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected,
    const std::vector<Operation>& operations,
    const std::string& idempotencyKey,
    const std::string& planFingerprint);

}  // namespace VectorworksMCP::Documentation
