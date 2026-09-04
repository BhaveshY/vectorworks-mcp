#include "StdAfx.h"

#include "DocumentationHandlers.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <deque>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

#if defined(_WINDOWS)
#include <Windows.h>
#endif

namespace VectorworksMCP::Documentation {
namespace {

constexpr double kMillimetresPerInch = 25.4;
constexpr std::size_t kMaxCachedReceipts = 128u;

struct CachedReceipt {
    std::string documentFingerprint;
    std::string idempotencyKey;
    std::string planFingerprint;
    std::string resultJson;
};

std::deque<CachedReceipt> gCachedReceipts;
std::string gObservedDocumentFingerprint;
std::uint64_t gDocumentGeneration = 0;
std::mutex gBindingMutex;

std::string JsonString(const std::string& value) {
    std::string out;
    out.reserve(value.size() + 2u);
    out.push_back('"');
    for (const unsigned char ch : value) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch < 0x20u) {
                    std::ostringstream escaped;
                    escaped << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                            << static_cast<unsigned int>(ch);
                    out += escaped.str();
                } else {
                    out.push_back(static_cast<char>(ch));
                }
        }
    }
    out.push_back('"');
    return out;
}

std::string JsonNumber(double value) {
    if (!std::isfinite(value)) {
        throw std::runtime_error("Vectorworks returned a non-finite documentation value");
    }
    std::ostringstream out;
    out << std::setprecision(15) << value;
    return out.str();
}

std::string HashText(const std::string& text) {
    constexpr std::uint64_t offset = 14695981039346656037ull;
    constexpr std::uint64_t prime = 1099511628211ull;
    std::uint64_t hash = offset;
    for (const unsigned char ch : text) {
        hash ^= static_cast<std::uint64_t>(ch);
        hash *= prime;
    }
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << hash;
    return out.str();
}

std::string NormalisePath(std::string value) {
    std::replace(value.begin(), value.end(), '/', '\\');
#if defined(_WINDOWS)
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
#endif
    while (value.size() > 3u && value.back() == '\\') {
        value.pop_back();
    }
    return value;
}

std::string ObjectUuid(VectorWorks::ISDK& sdk, MCObjectHandle object) {
    TXString uuid;
    return object && sdk.GetObjectUuid(object, uuid) && !uuid.IsEmpty()
        ? uuid.GetStdString()
        : std::string();
}

std::string ObjectName(VectorWorks::ISDK& sdk, MCObjectHandle object) {
    TXString name;
    if (object) {
        sdk.GetObjectName(object, name);
    }
    return name.GetStdString();
}

short GetShortVariable(VectorWorks::ISDK& sdk, MCObjectHandle object, short selector) {
    Sint16 value = 0;
    TVariableBlock block(value);
    if (!sdk.GetObjectVariable(object, selector, block) || !block.GetSint16(value)) {
        throw std::runtime_error("Vectorworks did not return a required short object variable");
    }
    return static_cast<short>(value);
}

double GetDoubleVariable(VectorWorks::ISDK& sdk, MCObjectHandle object, short selector) {
    double value = 0.0;
    TVariableBlock block(value);
    if (!sdk.GetObjectVariable(object, selector, block) || !block.GetReal64(value)) {
        throw std::runtime_error("Vectorworks did not return a required real object variable");
    }
    return value;
}

bool GetBoolVariable(VectorWorks::ISDK& sdk, MCObjectHandle object, short selector) {
    Boolean value = false;
    TVariableBlock block(value);
    if (!sdk.GetObjectVariable(object, selector, block) || !block.GetBoolean(value)) {
        throw std::runtime_error("Vectorworks did not return a required Boolean object variable");
    }
    return value != false;
}

std::string GetStringVariable(VectorWorks::ISDK& sdk, MCObjectHandle object, short selector) {
    TVariableBlock block;
    if (!sdk.GetObjectVariable(object, selector, block)) {
        throw std::runtime_error("Vectorworks did not return a required string object variable");
    }
    TXString value;
    if (!block.GetTXString(value)) {
        throw std::runtime_error("Vectorworks returned an invalid string object variable");
    }
    return value.GetStdString();
}

void SetShortVariable(VectorWorks::ISDK& sdk, MCObjectHandle object, short selector, short value) {
    TVariableBlock block(static_cast<Sint16>(value));
    if (!sdk.SetObjectVariable(object, selector, block)) {
        throw std::runtime_error("Vectorworks rejected a documentation short object variable");
    }
}

void SetDoubleVariable(VectorWorks::ISDK& sdk, MCObjectHandle object, short selector, double value) {
    TVariableBlock block(value);
    if (!sdk.SetObjectVariable(object, selector, block)) {
        throw std::runtime_error("Vectorworks rejected a documentation real object variable");
    }
}

void SetStringVariable(
    VectorWorks::ISDK& sdk,
    MCObjectHandle object,
    short selector,
    const std::string& value) {
    TVariableBlock block(TXString(value.c_str()));
    if (!sdk.SetObjectVariable(object, selector, block)) {
        throw std::runtime_error("Vectorworks rejected a documentation string object variable");
    }
}

const char* VisibilityName(short visibility) {
    switch (visibility) {
        case -1: return "hidden";
        case 0: return "normal";
        case 2: return "grayed";
        default: return "unknown";
    }
}

std::vector<MCObjectHandle> Layers(VectorWorks::ISDK& sdk) {
    std::vector<MCObjectHandle> result;
    sdk.ForEachLayerN([&](MCObjectHandle layer) {
        if (layer) {
            result.push_back(layer);
        }
    });
    return result;
}

bool IsLayerKind(VectorWorks::ISDK& sdk, MCObjectHandle layer, short kind) {
    return layer && sdk.GetObjectTypeN(layer) == kLayerNode &&
        GetShortVariable(sdk, layer, ovLayerType) == kind;
}

MCObjectHandle RequireUuidObject(
    VectorWorks::ISDK& sdk,
    const std::string& uuid,
    short expectedType,
    const std::string& label) {
    if (uuid.empty()) {
        throw std::invalid_argument(label + " UUID is required");
    }
    MCObjectHandle object = sdk.GetObjectByUuid(TXString(uuid.c_str()));
    if (!object || sdk.GetObjectTypeN(object) != expectedType) {
        throw std::invalid_argument(label + " UUID was not found or has the wrong native type");
    }
    return object;
}

MCObjectHandle RequireAnyUuidObject(
    VectorWorks::ISDK& sdk,
    const std::string& uuid,
    const std::string& label) {
    if (uuid.empty()) {
        throw std::invalid_argument(label + " UUID is required");
    }
    MCObjectHandle object = sdk.GetObjectByUuid(TXString(uuid.c_str()));
    if (!object) {
        throw std::invalid_argument(label + " UUID was not found");
    }
    return object;
}

MCObjectHandle RequireSheetLayer(
    VectorWorks::ISDK& sdk,
    const std::string& uuid,
    const std::string& label) {
    MCObjectHandle layer = RequireUuidObject(sdk, uuid, kLayerNode, label);
    if (!IsLayerKind(sdk, layer, kLayerSheet)) {
        throw std::invalid_argument(label + " UUID does not identify a sheet layer");
    }
    return layer;
}

std::string StripUuidPrefix(const std::string& reference) {
    return reference.rfind("uuid:", 0u) == 0u ? reference.substr(5u) : reference;
}

MCObjectHandle ResolveReference(
    VectorWorks::ISDK& sdk,
    const std::string& reference,
    const std::unordered_map<std::string, MCObjectHandle>& localObjects,
    short expectedType,
    const std::string& label) {
    if (reference.rfind("$", 0u) == 0u) {
        const auto found = localObjects.find(reference.substr(1u));
        if (found == localObjects.end() || !found->second ||
            sdk.GetObjectTypeN(found->second) != expectedType) {
            throw std::invalid_argument(label + " local reference was not resolved to the expected native type");
        }
        return found->second;
    }
    return RequireUuidObject(sdk, StripUuidPrefix(reference), expectedType, label);
}

MCObjectHandle ResolveSheetReference(
    VectorWorks::ISDK& sdk,
    const std::string& reference,
    const std::unordered_map<std::string, MCObjectHandle>& localObjects,
    const std::string& label) {
    MCObjectHandle layer = ResolveReference(sdk, reference, localObjects, kLayerNode, label);
    if (!IsLayerKind(sdk, layer, kLayerSheet)) {
        throw std::invalid_argument(label + " does not identify a sheet layer");
    }
    return layer;
}

std::string SheetTitleFromExpandedName(
    const std::string& layerName,
    const std::string& expandedName) {
    const std::string prefix = layerName + " [";
    if (expandedName.size() >= prefix.size() + 1u &&
        expandedName.compare(0u, prefix.size(), prefix) == 0 &&
        expandedName.back() == ']') {
        return expandedName.substr(prefix.size(), expandedName.size() - prefix.size() - 1u);
    }
    return std::string();
}

std::size_t CountViewports(VectorWorks::ISDK& sdk, MCObjectHandle sheetLayer) {
    std::size_t count = 0u;
    for (MCObjectHandle object = sdk.FirstMemberObj(sheetLayer);
         object && sdk.GetObjectTypeN(object) != kTermNode;
         object = sdk.NextObject(object)) {
        if (sdk.GetObjectTypeN(object) == kViewportNode) {
            ++count;
        }
    }
    return count;
}

std::string BoundsJson(VectorWorks::ISDK& sdk, MCObjectHandle object) {
    WorldRect bounds;
    sdk.GetObjectBounds(object, bounds);
    return std::string("{\"left\":") + JsonNumber(bounds.Left()) +
        ",\"top\":" + JsonNumber(bounds.Top()) +
        ",\"right\":" + JsonNumber(bounds.Right()) +
        ",\"bottom\":" + JsonNumber(bounds.Bottom()) + "}";
}

std::string LayerJson(VectorWorks::ISDK& sdk, MCObjectHandle layer) {
    const short layerType = GetShortVariable(sdk, layer, ovLayerType);
    const short visibility = GetShortVariable(sdk, layer, ovLayerVisibility);
    const std::string uuid = ObjectUuid(sdk, layer);
    if (uuid.empty()) {
        throw std::runtime_error("Vectorworks layer has no stable UUID");
    }
    const std::string name = ObjectName(sdk, layer);
    std::string json = "{\"uuid\":" + JsonString(uuid) +
        ",\"name\":" + JsonString(name) +
        ",\"kind\":" + JsonString(layerType == kLayerSheet ? "sheet" : "design") +
        ",\"active\":" + (sdk.GetActiveLayer() == layer ? "true" : "false") +
        ",\"visibility\":" + JsonString(VisibilityName(visibility));
    if (layerType == kLayerSheet) {
        const std::string expanded = GetStringVariable(sdk, layer, ovLayerExpandedSheetName);
        json += ",\"sheet\":{\"title\":" +
            JsonString(SheetTitleFromExpandedName(name, expanded));
        json += ",\"expanded_name\":" + JsonString(expanded);
        json += ",\"description\":" + JsonString(GetStringVariable(sdk, layer, ovLayerDescription));
        json += ",\"dpi\":" + std::to_string(GetShortVariable(sdk, layer, ovLayerDPI));
        const double widthInches = GetDoubleVariable(sdk, layer, ovLayerSheetWidth);
        const double heightInches = GetDoubleVariable(sdk, layer, ovLayerSheetHeight);
        json += ",\"width_mm\":" + JsonNumber(widthInches * kMillimetresPerInch);
        json += ",\"height_mm\":" + JsonNumber(heightInches * kMillimetresPerInch);
        json += ",\"viewport_count\":" + std::to_string(CountViewports(sdk, layer));
        json += "}";
    }
    json += "}";
    return json;
}

std::string ClassName(VectorWorks::ISDK& sdk, InternalIndex classIndex) {
    MCObjectHandle classHandle = sdk.InternalIndexToHandle(classIndex);
    return ObjectName(sdk, classHandle);
}

std::string ViewportJson(
    VectorWorks::ISDK& sdk,
    MCObjectHandle viewport,
    MCObjectHandle expectedSheet) {
    if (sdk.ParentObject(viewport) != expectedSheet) {
        throw std::runtime_error("viewport parentage does not match the requested sheet layer");
    }
    const std::string uuid = ObjectUuid(sdk, viewport);
    if (uuid.empty()) {
        throw std::runtime_error("Vectorworks viewport has no stable UUID");
    }
    std::string json = "{\"uuid\":" + JsonString(uuid) +
        ",\"name\":" + JsonString(ObjectName(sdk, viewport)) +
        ",\"sheet_layer_uuid\":" + JsonString(ObjectUuid(sdk, expectedSheet)) +
        ",\"parent_verified\":true";
    json += ",\"scale\":" + JsonNumber(GetDoubleVariable(sdk, viewport, ovViewportScale));
    json += ",\"placement_mm\":{\"x\":" +
        JsonNumber(GetDoubleVariable(sdk, viewport, ovViewportXPosition)) +
        ",\"y\":" + JsonNumber(GetDoubleVariable(sdk, viewport, ovViewportYPosition)) + "}";
    json += ",\"view\":{\"projection_type\":" +
        std::to_string(GetShortVariable(sdk, viewport, ovViewportProjectionType)) +
        ",\"view_type\":" + std::to_string(GetShortVariable(sdk, viewport, ovViewportViewType)) +
        ",\"render_type\":" + std::to_string(GetShortVariable(sdk, viewport, ovViewportRenderType)) +
        ",\"foreground_render_type\":" +
        std::to_string(GetShortVariable(sdk, viewport, ovViewportForegroundRenderType)) +
        ",\"dirty\":" + (GetBoolVariable(sdk, viewport, ovViewportDirty) ? "true" : "false") + "}";

    MCObjectHandle crop = sdk.GetViewportCropObject(viewport);
    json += ",\"crop\":";
    if (crop) {
        json += "{\"uuid\":" + JsonString(ObjectUuid(sdk, crop)) +
            ",\"native_type\":" + std::to_string(sdk.GetObjectTypeN(crop)) +
            ",\"bounds_mm\":" + BoundsJson(sdk, crop) + "}";
    } else {
        json += "null";
    }

    json += ",\"source_layers\":[";
    bool first = true;
    for (MCObjectHandle layer : Layers(sdk)) {
        if (!IsLayerKind(sdk, layer, kLayerDesign)) {
            continue;
        }
        short visibility = -1;
        if (!sdk.GetViewportLayerVisibility(viewport, layer, visibility)) {
            throw std::runtime_error("Vectorworks did not return viewport layer visibility");
        }
        if (!first) {
            json += ",";
        }
        first = false;
        json += "{\"uuid\":" + JsonString(ObjectUuid(sdk, layer)) +
            ",\"name\":" + JsonString(ObjectName(sdk, layer)) +
            ",\"visibility\":" + JsonString(VisibilityName(visibility)) + "}";
    }
    json += "]";

    json += ",\"source_classes\":[";
    first = true;
    sdk.ForEachClass(false, [&](MCObjectHandle classHandle) {
        const InternalIndex classIndex = sdk.GetObjectInternalIndex(classHandle);
        short visibility = -1;
        if (!sdk.GetViewportClassVisibility(viewport, classIndex, visibility)) {
            throw std::runtime_error("Vectorworks did not return viewport class visibility");
        }
        if (!first) {
            json += ",";
        }
        first = false;
        json += "{\"name\":" + JsonString(ObjectName(sdk, classHandle)) +
            ",\"visibility\":" + JsonString(VisibilityName(visibility)) + "}";
    });
    json += "]}";
    return json;
}

const char* AnnotationKind(VectorWorks::ISDK& sdk, MCObjectHandle annotation) {
    switch (sdk.GetObjectTypeN(annotation)) {
        case kTextNode: return "text";
        case dimHeaderNode: return "dimension";
        case kLineNode: return "marker";
        case kPolygonNode: return "redline";
        default: return "other";
    }
}

std::string AnnotationJson(
    VectorWorks::ISDK& sdk,
    MCObjectHandle annotation,
    MCObjectHandle viewport,
    MCObjectHandle sheetLayer) {
    MCObjectHandle annotationGroup = sdk.GetViewportGroup(viewport, kViewportGroupAnnotation);
    if (!annotationGroup || sdk.ParentObject(annotation) != annotationGroup) {
        throw std::runtime_error("viewport annotation parentage verification failed");
    }
    const std::string uuid = ObjectUuid(sdk, annotation);
    if (uuid.empty()) {
        throw std::runtime_error("Vectorworks viewport annotation has no stable UUID");
    }
    const InternalIndex classIndex = sdk.GetObjectClass(annotation);
    std::string json = "{\"uuid\":" + JsonString(uuid) +
        ",\"kind\":" + JsonString(AnnotationKind(sdk, annotation)) +
        ",\"native_type\":" + std::to_string(sdk.GetObjectTypeN(annotation)) +
        ",\"name\":" + JsonString(ObjectName(sdk, annotation)) +
        ",\"class_name\":" + JsonString(ClassName(sdk, classIndex)) +
        ",\"sheet_layer_uuid\":" + JsonString(ObjectUuid(sdk, sheetLayer)) +
        ",\"viewport_uuid\":" + JsonString(ObjectUuid(sdk, viewport)) +
        ",\"parent_verified\":true,\"coordinate_system\":\"viewport_annotation_mm\"";
    if (sdk.GetObjectTypeN(annotation) == kTextNode) {
        json += ",\"text\":" + JsonString(sdk.GetTextChars(annotation).GetStdString());
    }
    if (sdk.GetObjectTypeN(annotation) == kLineNode) {
        MarkerType style = static_cast<MarkerType>(0);
        short size = 0;
        short angle = 0;
        Boolean start = false;
        Boolean end = false;
        sdk.GetMarker(annotation, style, size, angle, start, end);
        json += ",\"marker\":{\"style\":" + std::to_string(static_cast<int>(style)) +
            ",\"size\":" + std::to_string(size) +
            ",\"angle\":" + std::to_string(angle) +
            ",\"start\":" + (start ? "true" : "false") +
            ",\"end\":" + (end ? "true" : "false") + "}";
    }
    json += ",\"bounds_mm\":" + BoundsJson(sdk, annotation) + "}";
    return json;
}

std::string PageJson(
    std::size_t offset,
    std::size_t limit,
    std::size_t returned,
    std::size_t total) {
    std::string json = "{\"offset\":" + std::to_string(offset) +
        ",\"limit\":" + std::to_string(limit) +
        ",\"returned\":" + std::to_string(returned) +
        ",\"total\":" + std::to_string(total) +
        ",\"next_cursor\":";
    if (offset + returned < total) {
        json += JsonString(std::to_string(offset + returned));
    } else {
        json += "null";
    }
    json += "}";
    return json;
}

class CreatedObjectGuard {
public:
    CreatedObjectGuard(VectorWorks::ISDK& sdk, MCObjectHandle object)
        : sdk_(sdk), object_(object) {}
    ~CreatedObjectGuard() {
        if (object_) {
            sdk_.DeleteObject(object_, false);
        }
    }
    void Release() { object_ = nullptr; }

private:
    VectorWorks::ISDK& sdk_;
    MCObjectHandle object_ = nullptr;
};

class ActiveLayerRestorer {
public:
    explicit ActiveLayerRestorer(VectorWorks::ISDK& sdk)
        : sdk_(sdk), original_(sdk.GetActiveLayer()) {}
    ~ActiveLayerRestorer() {
        if (original_ && sdk_.GetActiveLayer() != original_) {
            sdk_.SetCurrentLayer(original_);
        }
    }
    void RestoreAndVerify() {
        if (original_ && sdk_.GetActiveLayer() != original_) {
            sdk_.SetCurrentLayer(original_);
        }
        if (original_ && sdk_.GetActiveLayer() != original_) {
            throw std::runtime_error("Vectorworks did not restore the original active layer");
        }
    }

private:
    VectorWorks::ISDK& sdk_;
    MCObjectHandle original_ = nullptr;
};

void RequireUniqueLayerName(VectorWorks::ISDK& sdk, const std::string& name, MCObjectHandle except = nullptr) {
    if (name.empty()) {
        throw std::invalid_argument("sheet layer name is required");
    }
    for (MCObjectHandle layer : Layers(sdk)) {
        if (layer != except && ObjectName(sdk, layer) == name) {
            throw std::invalid_argument("layer name already exists: " + name);
        }
    }
}

void ApplySheetLayerFields(VectorWorks::ISDK& sdk, MCObjectHandle layer, const Operation& operation) {
    if (operation.hasName) {
        RequireUniqueLayerName(sdk, operation.name, layer);
        if (!sdk.SetLayerName(layer, TXString(operation.name.c_str()), true)) {
            throw std::runtime_error("Vectorworks rejected the sheet layer name");
        }
    }
    if (operation.hasTitle && !sdk.SetSheetLayerTitle(layer, TXString(operation.title.c_str()))) {
        throw std::runtime_error("Vectorworks rejected the sheet layer title");
    }
    if (operation.hasDescription) {
        SetStringVariable(sdk, layer, ovLayerDescription, operation.description);
    }
    if (operation.hasDpi) {
        SetShortVariable(sdk, layer, ovLayerDPI, operation.dpi);
    }
    if (operation.hasSheetWidthMm) {
        SetDoubleVariable(sdk, layer, ovLayerSheetWidth, operation.sheetWidthMm / kMillimetresPerInch);
    }
    if (operation.hasSheetHeightMm) {
        SetDoubleVariable(sdk, layer, ovLayerSheetHeight, operation.sheetHeightMm / kMillimetresPerInch);
    }
    if (operation.hasVisibility) {
        SetShortVariable(sdk, layer, ovLayerVisibility, operation.visibility);
    }
}

void VerifySheetLayerFields(VectorWorks::ISDK& sdk, MCObjectHandle layer, const Operation& operation) {
    if (!IsLayerKind(sdk, layer, kLayerSheet)) {
        throw std::runtime_error("sheet layer changed native kind during semantic verification");
    }
    if (operation.hasName && ObjectName(sdk, layer) != operation.name) {
        throw std::runtime_error("sheet layer name readback mismatch");
    }
    if (operation.hasTitle) {
        const std::string name = ObjectName(sdk, layer);
        const std::string expanded = GetStringVariable(sdk, layer, ovLayerExpandedSheetName);
        if (SheetTitleFromExpandedName(name, expanded) != operation.title) {
            throw std::runtime_error("sheet layer title readback mismatch");
        }
    }
    if (operation.hasDescription &&
        GetStringVariable(sdk, layer, ovLayerDescription) != operation.description) {
        throw std::runtime_error("sheet layer description readback mismatch");
    }
    if (operation.hasDpi && GetShortVariable(sdk, layer, ovLayerDPI) != operation.dpi) {
        throw std::runtime_error("sheet layer DPI readback mismatch");
    }
    if (operation.hasSheetWidthMm &&
        std::abs(GetDoubleVariable(sdk, layer, ovLayerSheetWidth) * kMillimetresPerInch -
                 operation.sheetWidthMm) > 0.01) {
        throw std::runtime_error("sheet layer width readback mismatch");
    }
    if (operation.hasSheetHeightMm &&
        std::abs(GetDoubleVariable(sdk, layer, ovLayerSheetHeight) * kMillimetresPerInch -
                 operation.sheetHeightMm) > 0.01) {
        throw std::runtime_error("sheet layer height readback mismatch");
    }
    if (operation.hasVisibility &&
        GetShortVariable(sdk, layer, ovLayerVisibility) != operation.visibility) {
        throw std::runtime_error("sheet layer visibility readback mismatch");
    }
}

void ApplyViewportSourceLayers(VectorWorks::ISDK& sdk, MCObjectHandle viewport, const Operation& operation) {
    if (!operation.hasSourceLayers) {
        return;
    }
    std::unordered_set<std::string> requested;
    for (const VisibilityEntry& entry : operation.sourceLayers) {
        const std::string uuid = StripUuidPrefix(entry.identity);
        MCObjectHandle layer = RequireUuidObject(sdk, uuid, kLayerNode, "viewport source layer");
        if (!IsLayerKind(sdk, layer, kLayerDesign)) {
            throw std::invalid_argument("viewport source layer must be a design layer UUID");
        }
        requested.insert(uuid);
    }
    if (operation.replaceSourceLayers) {
        for (MCObjectHandle layer : Layers(sdk)) {
            if (IsLayerKind(sdk, layer, kLayerDesign) &&
                !sdk.SetViewportLayerVisibility(viewport, layer, -1)) {
                throw std::runtime_error("Vectorworks rejected replacement viewport layer visibility");
            }
        }
    }
    for (const VisibilityEntry& entry : operation.sourceLayers) {
        MCObjectHandle layer = RequireUuidObject(
            sdk, StripUuidPrefix(entry.identity), kLayerNode, "viewport source layer");
        if (!sdk.SetViewportLayerVisibility(viewport, layer, entry.visibility)) {
            throw std::runtime_error("Vectorworks rejected viewport source layer visibility");
        }
    }
}

void ApplyViewportSourceClasses(VectorWorks::ISDK& sdk, MCObjectHandle viewport, const Operation& operation) {
    if (!operation.hasSourceClasses) {
        return;
    }
    std::vector<std::pair<InternalIndex, short>> requested;
    requested.reserve(operation.sourceClasses.size());
    for (const VisibilityEntry& entry : operation.sourceClasses) {
        const InternalIndex classIndex = sdk.ClassNameToID(TXString(entry.identity.c_str()));
        if (!sdk.ValidClass(classIndex)) {
            throw std::invalid_argument("viewport source class was not found: " + entry.identity);
        }
        requested.push_back({classIndex, entry.visibility});
    }
    if (operation.replaceSourceClasses) {
        sdk.ForEachClass(false, [&](MCObjectHandle classHandle) {
            const InternalIndex classIndex = sdk.GetObjectInternalIndex(classHandle);
            if (!sdk.SetViewportClassVisibility(viewport, classIndex, -1)) {
                throw std::runtime_error("Vectorworks rejected replacement viewport class visibility");
            }
        });
    }
    for (const auto& entry : requested) {
        if (!sdk.SetViewportClassVisibility(viewport, entry.first, entry.second)) {
            throw std::runtime_error("Vectorworks rejected viewport source class visibility");
        }
    }
}

MCObjectHandle CreateCropPolygon(VectorWorks::ISDK& sdk, const std::vector<WorldPt>& points) {
    if (points.size() < 3u) {
        throw std::invalid_argument("viewport crop requires at least three points");
    }
    MCObjectHandle crop = sdk.CreatePolyshape();
    if (!crop) {
        throw std::runtime_error("Vectorworks did not create the viewport crop polygon");
    }
    CreatedObjectGuard guard(sdk, crop);
    for (const WorldPt& point : points) {
        sdk.AddVertex(crop, point, nullptr, vtCorner, 0, false);
    }
    sdk.SetPolyShapeClose(crop, true);
    sdk.ResetObject(crop);
    guard.Release();
    return crop;
}

void ApplyViewportFields(VectorWorks::ISDK& sdk, MCObjectHandle viewport, const Operation& operation) {
    if (operation.hasName && sdk.SetObjectName(viewport, TXString(operation.name.c_str())) != noError) {
        throw std::runtime_error("Vectorworks rejected the viewport name");
    }
    if (operation.hasScale) {
        SetDoubleVariable(sdk, viewport, ovViewportScale, operation.scale);
    }
    if (operation.hasProjectionType) {
        SetShortVariable(sdk, viewport, ovViewportProjectionType, operation.projectionType);
    }
    if (operation.hasViewType) {
        SetShortVariable(sdk, viewport, ovViewportViewType, operation.viewType);
    }
    if (operation.hasRenderType) {
        SetShortVariable(sdk, viewport, ovViewportRenderType, operation.renderType);
    }
    if (operation.hasForegroundRenderType) {
        SetShortVariable(sdk, viewport, ovViewportForegroundRenderType, operation.foregroundRenderType);
    }
    ApplyViewportSourceLayers(sdk, viewport, operation);
    ApplyViewportSourceClasses(sdk, viewport, operation);
    if (operation.clearCrop) {
        if (!sdk.SetViewportCropObject(viewport, nullptr) || sdk.GetViewportCropObject(viewport)) {
            throw std::runtime_error("Vectorworks did not clear the exact viewport crop");
        }
    } else if (operation.hasCrop) {
        MCObjectHandle crop = CreateCropPolygon(sdk, operation.cropPoints);
        CreatedObjectGuard cropGuard(sdk, crop);
        if (!sdk.SetViewportCropObject(viewport, crop) || sdk.GetViewportCropObject(viewport) != crop) {
            throw std::runtime_error("Vectorworks did not attach the exact viewport crop");
        }
        cropGuard.Release();
    }
    if (operation.hasPlacement) {
        const double currentX = GetDoubleVariable(sdk, viewport, ovViewportXPosition);
        const double currentY = GetDoubleVariable(sdk, viewport, ovViewportYPosition);
        sdk.MoveObject(viewport, operation.x - currentX, operation.y - currentY);
    }
    sdk.UpdateViewport(viewport);
}

void VerifyViewportFields(
    VectorWorks::ISDK& sdk,
    MCObjectHandle viewport,
    MCObjectHandle sheetLayer,
    const Operation& operation) {
    if (!viewport || sdk.GetObjectTypeN(viewport) != kViewportNode ||
        sdk.ParentObject(viewport) != sheetLayer) {
        throw std::runtime_error("viewport native type or exact sheet-layer parent readback mismatch");
    }
    if (operation.hasName && ObjectName(sdk, viewport) != operation.name) {
        throw std::runtime_error("viewport name readback mismatch");
    }
    if (operation.hasScale &&
        std::abs(GetDoubleVariable(sdk, viewport, ovViewportScale) - operation.scale) > 0.000001) {
        throw std::runtime_error("viewport scale readback mismatch");
    }
    if (operation.hasPlacement &&
        (std::abs(GetDoubleVariable(sdk, viewport, ovViewportXPosition) - operation.x) > 0.01 ||
         std::abs(GetDoubleVariable(sdk, viewport, ovViewportYPosition) - operation.y) > 0.01)) {
        throw std::runtime_error("viewport placement readback mismatch");
    }
    if (operation.hasProjectionType &&
        GetShortVariable(sdk, viewport, ovViewportProjectionType) != operation.projectionType) {
        throw std::runtime_error("viewport projection readback mismatch");
    }
    if (operation.hasViewType &&
        GetShortVariable(sdk, viewport, ovViewportViewType) != operation.viewType) {
        throw std::runtime_error("viewport view type readback mismatch");
    }
    if (operation.hasRenderType &&
        GetShortVariable(sdk, viewport, ovViewportRenderType) != operation.renderType) {
        throw std::runtime_error("viewport render type readback mismatch");
    }
    if (operation.hasForegroundRenderType &&
        GetShortVariable(sdk, viewport, ovViewportForegroundRenderType) != operation.foregroundRenderType) {
        throw std::runtime_error("viewport foreground render type readback mismatch");
    }
    if (operation.clearCrop && sdk.GetViewportCropObject(viewport)) {
        throw std::runtime_error("viewport crop clear readback mismatch");
    }
    if (operation.hasCrop && !sdk.GetViewportCropObject(viewport)) {
        throw std::runtime_error("viewport crop readback mismatch");
    }
    for (const VisibilityEntry& entry : operation.sourceLayers) {
        MCObjectHandle layer = RequireUuidObject(
            sdk, StripUuidPrefix(entry.identity), kLayerNode, "viewport source layer");
        short actual = -9;
        if (!sdk.GetViewportLayerVisibility(viewport, layer, actual) || actual != entry.visibility) {
            throw std::runtime_error("viewport source layer visibility readback mismatch");
        }
    }
    for (const VisibilityEntry& entry : operation.sourceClasses) {
        const InternalIndex classIndex = sdk.ClassNameToID(TXString(entry.identity.c_str()));
        short actual = -9;
        if (!sdk.ValidClass(classIndex) ||
            !sdk.GetViewportClassVisibility(viewport, classIndex, actual) || actual != entry.visibility) {
            throw std::runtime_error("viewport source class visibility readback mismatch");
        }
    }
}

InternalIndex ResolveClass(VectorWorks::ISDK& sdk, const std::string& className) {
    if (className.empty()) {
        throw std::invalid_argument("viewport annotation class_name is required");
    }
    const InternalIndex classIndex = sdk.ClassNameToID(TXString(className.c_str()));
    if (!sdk.ValidClass(classIndex)) {
        throw std::invalid_argument("viewport annotation class was not found: " + className);
    }
    return classIndex;
}

MCObjectHandle CreateAnnotationObject(VectorWorks::ISDK& sdk, const Operation& operation) {
    MCObjectHandle object = nullptr;
    if (operation.annotationKind == "text") {
        if (operation.text.empty()) {
            throw std::invalid_argument("viewport annotation text must be non-empty");
        }
        object = sdk.CreateTextBlock(TXString(operation.text.c_str()), WorldPt(operation.x1, operation.y1), false, 0.0);
    } else if (operation.annotationKind == "dimension") {
        object = sdk.CreateLinearDimension(
            WorldPt(operation.x1, operation.y1),
            WorldPt(operation.x2, operation.y2),
            operation.offset,
            operation.textOffset,
            Vector2(0.0, 1.0),
            operation.dimensionType);
    } else if (operation.annotationKind == "marker") {
        object = sdk.CreateLine(WorldPt(operation.x1, operation.y1), WorldPt(operation.x2, operation.y2));
        if (object) {
            sdk.SetMarker(
                object,
                static_cast<MarkerType>(operation.markerStyle),
                operation.markerSize,
                operation.markerAngle,
                false,
                true);
        }
    } else if (operation.annotationKind == "redline") {
        object = sdk.CreatePolyshape();
        if (object) {
            for (const WorldPt& point : operation.points) {
                sdk.AddVertex(object, point, nullptr, vtCorner, 0, false);
            }
            sdk.SetPolyShapeClose(object, true);
            sdk.ResetObject(object);
        }
    } else {
        throw std::invalid_argument("unsupported viewport annotation kind");
    }
    if (!object) {
        throw std::runtime_error("Vectorworks did not create the viewport annotation object");
    }
    return object;
}

void SetTextContent(VectorWorks::ISDK& sdk, MCObjectHandle object, const std::string& text) {
    if (text.empty()) {
        throw std::invalid_argument("viewport annotation text must be non-empty");
    }
    const Sint32 originalLength = sdk.GetTextLength(object);
    TXString replacement(text.c_str());
    if (!sdk.AddTextFromBuffer(
            object,
            originalLength,
            (const UCChar*) replacement,
            static_cast<Sint32>(replacement.GetLength()))) {
        throw std::runtime_error("Vectorworks rejected viewport annotation text replacement");
    }
    if (originalLength > 0) {
        sdk.DeleteText(object, 0, originalLength);
    }
    if (sdk.GetTextChars(object).GetStdString() != text) {
        throw std::runtime_error("viewport annotation text readback mismatch");
    }
}

void ApplyAnnotationFields(VectorWorks::ISDK& sdk, MCObjectHandle annotation, const Operation& operation) {
    if (operation.hasName && sdk.SetObjectName(annotation, TXString(operation.name.c_str())) != noError) {
        throw std::runtime_error("Vectorworks rejected the viewport annotation name");
    }
    if (operation.hasClassName) {
        sdk.SetObjectClass(annotation, ResolveClass(sdk, operation.className));
    }
    if (operation.hasText) {
        if (sdk.GetObjectTypeN(annotation) != kTextNode) {
            throw std::invalid_argument("text can only update a native text viewport annotation");
        }
        SetTextContent(sdk, annotation, operation.text);
    }
    if (operation.hasDelta) {
        sdk.MoveObject(annotation, operation.deltaX, operation.deltaY);
    }
    sdk.ResetObject(annotation);
}

void VerifyAnnotation(
    VectorWorks::ISDK& sdk,
    MCObjectHandle annotation,
    MCObjectHandle viewport,
    MCObjectHandle sheetLayer,
    const Operation& operation) {
    if (sdk.ParentObject(viewport) != sheetLayer) {
        throw std::runtime_error("viewport parent changed during annotation mutation");
    }
    MCObjectHandle group = sdk.GetViewportGroup(viewport, kViewportGroupAnnotation);
    if (!group || sdk.ParentObject(annotation) != group) {
        throw std::runtime_error("viewport annotation exact parent-container readback mismatch");
    }
    if (operation.hasName && ObjectName(sdk, annotation) != operation.name) {
        throw std::runtime_error("viewport annotation name readback mismatch");
    }
    if (operation.hasClassName &&
        sdk.GetObjectClass(annotation) != ResolveClass(sdk, operation.className)) {
        throw std::runtime_error("viewport annotation class readback mismatch");
    }
    if (operation.hasText && sdk.GetTextChars(annotation).GetStdString() != operation.text) {
        throw std::runtime_error("viewport annotation text readback mismatch");
    }
}

std::string OperationKindName(OperationKind kind) {
    switch (kind) {
        case OperationKind::CreateSheetLayer: return "sheet_layer.create";
        case OperationKind::UpdateSheetLayer: return "sheet_layer.update";
        case OperationKind::DeleteSheetLayer: return "sheet_layer.delete";
        case OperationKind::CreateViewport: return "viewport.create";
        case OperationKind::UpdateViewport: return "viewport.update";
        case OperationKind::DeleteViewport: return "viewport.delete";
        case OperationKind::CreateViewportAnnotation: return "viewport_annotation.create";
        case OperationKind::UpdateViewportAnnotation: return "viewport_annotation.update";
        case OperationKind::DeleteViewportAnnotation: return "viewport_annotation.delete";
    }
    throw std::logic_error("unknown documentation operation kind");
}

}  // namespace

std::string BridgeSessionId() {
    static const std::string value = [] {
        std::ostringstream seed;
#if defined(_WINDOWS)
        seed << GetCurrentProcessId();
#else
        seed << reinterpret_cast<std::uintptr_t>(&gDocumentGeneration);
#endif
        seed << ':' << std::chrono::high_resolution_clock::now().time_since_epoch().count();
        return std::string("vw-session-") + HashText(seed.str());
    }();
    return value;
}

DocumentBinding ReadDocumentBinding(VectorWorks::ISDK& sdk) {
    DocumentBinding binding;
    binding.bridgeSessionId = BridgeSessionId();
    binding.dirty = sdk.IsActiveFileChangedAfterLastSave();

    VectorWorks::Filing::IFileIdentifierPtr activeFile(VectorWorks::Filing::IID_FileIdentifier);
    bool saved = false;
    if (activeFile && sdk.GetActiveDocument(&activeFile, saved) && saved) {
        TXString fileName;
        TXString filePath;
        activeFile->GetFileName(fileName);
        activeFile->GetFileFullPath(filePath);
        binding.fileName = fileName.GetStdString();
        binding.filePath = filePath.GetStdString();
    }

    MCObjectHandle activeLayer = sdk.GetActiveLayer();
    binding.activeLayerUuid = ObjectUuid(sdk, activeLayer);
    binding.activeLayerName = ObjectName(sdk, activeLayer);

    MCObjectHandle header = sdk.GetDrawingHeader();
    std::string headerUuid = ObjectUuid(sdk, header);
    std::ostringstream documentInstance;
    documentInstance << reinterpret_cast<std::uintptr_t>(header);
    binding.documentFingerprint = "vw-doc-" +
        HashText(NormalisePath(binding.filePath) + "|" + headerUuid + "|" + documentInstance.str());

    {
        std::lock_guard<std::mutex> lock(gBindingMutex);
        if (gObservedDocumentFingerprint != binding.documentFingerprint) {
            gObservedDocumentFingerprint = binding.documentFingerprint;
            ++gDocumentGeneration;
        }
        binding.documentGeneration = gDocumentGeneration;
    }
    return binding;
}

std::string DocumentBindingJson(const DocumentBinding& binding) {
    return std::string("{\"file_path\":") + JsonString(binding.filePath) +
        ",\"file_name\":" + JsonString(binding.fileName) +
        ",\"document_fingerprint\":" + JsonString(binding.documentFingerprint) +
        ",\"document_generation\":" + std::to_string(binding.documentGeneration) +
        ",\"bridge_session_id\":" + JsonString(binding.bridgeSessionId) +
        ",\"dirty\":" + (binding.dirty ? "true" : "false") +
        ",\"active_layer_uuid\":" + JsonString(binding.activeLayerUuid) +
        ",\"active_layer_name\":" + JsonString(binding.activeLayerName) + "}";
}

void ValidateTargetBinding(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected) {
    if (expected.filePath.empty() || expected.documentFingerprint.empty() ||
        expected.documentGeneration == 0u || expected.bridgeSessionId.empty() ||
        expected.activeLayerUuid.empty() || expected.activeLayerName.empty()) {
        throw std::invalid_argument(
            "saved file_path, document_fingerprint, document_generation, bridge_session_id, "
            "active_layer_uuid, and active_layer_name are required");
    }
    const DocumentBinding actual = ReadDocumentBinding(sdk);
    if (NormalisePath(actual.filePath) != NormalisePath(expected.filePath)) {
        throw std::runtime_error("target binding mismatch: active Vectorworks file path changed");
    }
    if (actual.documentFingerprint != expected.documentFingerprint) {
        throw std::runtime_error("target binding mismatch: active Vectorworks document fingerprint changed");
    }
    if (actual.documentGeneration != expected.documentGeneration) {
        throw std::runtime_error("target binding mismatch: active Vectorworks document generation changed");
    }
    if (actual.bridgeSessionId != expected.bridgeSessionId) {
        throw std::runtime_error("target binding mismatch: native bridge session changed");
    }
    if (actual.activeLayerUuid != expected.activeLayerUuid ||
        actual.activeLayerName != expected.activeLayerName) {
        throw std::runtime_error("target binding mismatch: active Vectorworks layer changed");
    }
    if (expected.hasDirty && actual.dirty != expected.dirty) {
        throw std::runtime_error("target binding mismatch: dirty-document state changed");
    }
}

std::string ReadSheetLayers(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected,
    std::size_t offset,
    std::size_t limit) {
    ValidateTargetBinding(sdk, expected);
    std::vector<MCObjectHandle> sheets;
    for (MCObjectHandle layer : Layers(sdk)) {
        if (IsLayerKind(sdk, layer, kLayerSheet)) {
            sheets.push_back(layer);
        }
    }
    const std::size_t begin = std::min(offset, sheets.size());
    const std::size_t end = std::min(begin + limit, sheets.size());
    std::string json = "{\"coordinate_units\":\"mm\",\"binding\":" +
        DocumentBindingJson(ReadDocumentBinding(sdk)) + ",\"items\":[";
    for (std::size_t index = begin; index < end; ++index) {
        if (index != begin) {
            json += ",";
        }
        json += LayerJson(sdk, sheets[index]);
    }
    json += "],\"page\":" + PageJson(offset, limit, end - begin, sheets.size()) + "}";
    return json;
}

std::string ReadViewports(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected,
    const std::string& sheetLayerUuid,
    std::size_t offset,
    std::size_t limit) {
    ValidateTargetBinding(sdk, expected);
    MCObjectHandle sheet = RequireSheetLayer(sdk, StripUuidPrefix(sheetLayerUuid), "sheet layer");
    std::vector<MCObjectHandle> viewports;
    for (MCObjectHandle object = sdk.FirstMemberObj(sheet);
         object && sdk.GetObjectTypeN(object) != kTermNode;
         object = sdk.NextObject(object)) {
        if (sdk.GetObjectTypeN(object) == kViewportNode) {
            viewports.push_back(object);
        }
    }
    const std::size_t begin = std::min(offset, viewports.size());
    const std::size_t end = std::min(begin + limit, viewports.size());
    std::string json = "{\"coordinate_units\":\"mm\",\"binding\":" +
        DocumentBindingJson(ReadDocumentBinding(sdk)) +
        ",\"sheet_layer\":" + LayerJson(sdk, sheet) + ",\"items\":[";
    for (std::size_t index = begin; index < end; ++index) {
        if (index != begin) {
            json += ",";
        }
        json += ViewportJson(sdk, viewports[index], sheet);
    }
    json += "],\"page\":" + PageJson(offset, limit, end - begin, viewports.size()) + "}";
    return json;
}

std::string ReadViewportAnnotations(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected,
    const std::string& sheetLayerUuid,
    const std::string& viewportUuid,
    std::size_t offset,
    std::size_t limit) {
    ValidateTargetBinding(sdk, expected);
    MCObjectHandle sheet = RequireSheetLayer(sdk, StripUuidPrefix(sheetLayerUuid), "sheet layer");
    MCObjectHandle viewport = RequireUuidObject(
        sdk, StripUuidPrefix(viewportUuid), kViewportNode, "viewport");
    if (sdk.ParentObject(viewport) != sheet) {
        throw std::invalid_argument("viewport is not parented by the exact requested sheet layer");
    }
    MCObjectHandle group = sdk.GetViewportGroup(viewport, kViewportGroupAnnotation);
    if (!group) {
        throw std::runtime_error("Vectorworks did not return the viewport annotation container");
    }
    std::vector<MCObjectHandle> annotations;
    for (MCObjectHandle object = sdk.FirstMemberObj(group);
         object && sdk.GetObjectTypeN(object) != kTermNode;
         object = sdk.NextObject(object)) {
        annotations.push_back(object);
    }
    const std::size_t begin = std::min(offset, annotations.size());
    const std::size_t end = std::min(begin + limit, annotations.size());
    std::string json = "{\"coordinate_units\":\"mm\",\"coordinate_system\":\"viewport_annotation_mm\",\"binding\":" +
        DocumentBindingJson(ReadDocumentBinding(sdk)) +
        ",\"sheet_layer_uuid\":" + JsonString(ObjectUuid(sdk, sheet)) +
        ",\"viewport_uuid\":" + JsonString(ObjectUuid(sdk, viewport)) +
        ",\"annotation_container_verified\":true,\"items\":[";
    for (std::size_t index = begin; index < end; ++index) {
        if (index != begin) {
            json += ",";
        }
        json += AnnotationJson(sdk, annotations[index], viewport, sheet);
    }
    json += "],\"page\":" + PageJson(offset, limit, end - begin, annotations.size()) + "}";
    return json;
}

std::string ApplyOperations(
    VectorWorks::ISDK& sdk,
    const ExpectedTargetBinding& expected,
    const std::vector<Operation>& operations,
    const std::string& idempotencyKey,
    const std::string& planFingerprint) {
    if (operations.empty()) {
        throw std::invalid_argument("documentation operation plan must not be empty");
    }
    if (idempotencyKey.empty() || planFingerprint.empty()) {
        throw std::invalid_argument("documentation writes require idempotency_key and plan_hash");
    }
    const DocumentBinding initialBinding = ReadDocumentBinding(sdk);
    for (const CachedReceipt& cached : gCachedReceipts) {
        if (cached.documentFingerprint == initialBinding.documentFingerprint &&
            cached.idempotencyKey == idempotencyKey) {
            if (cached.planFingerprint != planFingerprint) {
                throw std::invalid_argument(
                    "idempotency_key was already used for a different documentation plan");
            }
            ExpectedTargetBinding replayExpected = expected;
            replayExpected.hasDirty = false;
            ValidateTargetBinding(sdk, replayExpected);
            return cached.resultJson;
        }
    }
    ValidateTargetBinding(sdk, expected);

    ActiveLayerRestorer activeLayerRestorer(sdk);
    Transactions::NativeTransaction transaction(
        sdk,
        TXString("Vectorworks MCP documentation workflow"),
        Transactions::TransactionOptions{operations.size(), {}});
    std::unordered_map<std::string, MCObjectHandle> localObjects;
    std::vector<std::string> receiptUuids;
    receiptUuids.reserve(operations.size());

    try {
        for (const Operation& operation : operations) {
            MCObjectHandle receiptObject = nullptr;
            switch (operation.kind) {
                case OperationKind::CreateSheetLayer: {
                    RequireUniqueLayerName(sdk, operation.name);
                    MCObjectHandle layer = sdk.CreateLayer(TXString(operation.name.c_str()), kLayerSheet);
                    if (!layer) {
                        throw std::runtime_error("Vectorworks did not create the sheet layer");
                    }
                    CreatedObjectGuard guard(sdk, layer);
                    ApplySheetLayerFields(sdk, layer, operation);
                    VerifySheetLayerFields(sdk, layer, operation);
                    const Operation expectedFields = operation;
                    transaction.AdoptFinal(
                        layer,
                        Transactions::ObjectFamily::SheetLayer,
                        kLayerNode,
                        [&sdk, expectedFields](MCObjectHandle verified) {
                            VerifySheetLayerFields(sdk, verified, expectedFields);
                        });
                    guard.Release();
                    receiptObject = layer;
                    break;
                }
                case OperationKind::UpdateSheetLayer: {
                    MCObjectHandle layer = ResolveSheetReference(
                        sdk, operation.targetRef, localObjects, "sheet layer target");
                    if (operation.targetRef.rfind("$", 0u) == 0u) {
                        throw std::invalid_argument("sheet layer updates require an external UUID target");
                    }
                    const auto mutation = transaction.TrackExternalBefore(
                        layer, Transactions::ObjectFamily::SheetLayer);
                    ApplySheetLayerFields(sdk, layer, operation);
                    VerifySheetLayerFields(sdk, layer, operation);
                    transaction.TrackExternalAfter(mutation, layer);
                    receiptObject = layer;
                    break;
                }
                case OperationKind::DeleteSheetLayer: {
                    if (operation.confirmation != "DELETE_SHEET_LAYER_AND_CONTENTS") {
                        throw std::invalid_argument(
                            "sheet layer deletion requires confirm=DELETE_SHEET_LAYER_AND_CONTENTS");
                    }
                    MCObjectHandle layer = ResolveSheetReference(
                        sdk, operation.targetRef, localObjects, "sheet layer target");
                    if (operation.targetRef.rfind("$", 0u) == 0u) {
                        throw std::invalid_argument("sheet layer deletion requires an external UUID target");
                    }
                    if (sdk.GetActiveLayer() == layer) {
                        throw std::invalid_argument("refusing to delete the active sheet layer");
                    }
                    const std::string uuid = ObjectUuid(sdk, layer);
                    const auto mutation = transaction.TrackExternalBefore(
                        layer, Transactions::ObjectFamily::SheetLayer);
                    sdk.DeleteObject(layer, false);
                    if (sdk.GetObjectByUuid(TXString(uuid.c_str()))) {
                        throw std::runtime_error("Vectorworks did not delete the exact sheet layer UUID");
                    }
                    transaction.TrackExternalDeleted(mutation);
                    receiptUuids.push_back(uuid);
                    continue;
                }
                case OperationKind::CreateViewport: {
                    MCObjectHandle sheet = ResolveSheetReference(
                        sdk, operation.sheetLayerRef, localObjects, "viewport sheet layer");
                    MCObjectHandle viewport = sdk.CreateViewport(sheet);
                    if (!viewport) {
                        throw std::runtime_error("Vectorworks did not create the viewport");
                    }
                    CreatedObjectGuard guard(sdk, viewport);
                    ApplyViewportFields(sdk, viewport, operation);
                    VerifyViewportFields(sdk, viewport, sheet, operation);
                    const Operation expectedFields = operation;
                    const std::string expectedSheetUuid = ObjectUuid(sdk, sheet);
                    transaction.AdoptFinal(
                        viewport,
                        Transactions::ObjectFamily::Viewport,
                        kViewportNode,
                        [&sdk, expectedFields, expectedSheetUuid](MCObjectHandle verified) {
                            MCObjectHandle verifiedSheet = RequireSheetLayer(
                                sdk, expectedSheetUuid, "viewport sheet layer");
                            VerifyViewportFields(sdk, verified, verifiedSheet, expectedFields);
                        });
                    guard.Release();
                    receiptObject = viewport;
                    break;
                }
                case OperationKind::UpdateViewport: {
                    MCObjectHandle sheet = ResolveSheetReference(
                        sdk, operation.sheetLayerRef, localObjects, "viewport sheet layer");
                    MCObjectHandle viewport = ResolveReference(
                        sdk, operation.targetRef, localObjects, kViewportNode, "viewport target");
                    if (operation.targetRef.rfind("$", 0u) == 0u) {
                        throw std::invalid_argument("viewport updates require an external UUID target");
                    }
                    if (sdk.ParentObject(viewport) != sheet) {
                        throw std::invalid_argument("viewport target is not on the exact requested sheet layer");
                    }
                    const auto mutation = transaction.TrackExternalBefore(
                        viewport, Transactions::ObjectFamily::Viewport);
                    ApplyViewportFields(sdk, viewport, operation);
                    VerifyViewportFields(sdk, viewport, sheet, operation);
                    transaction.TrackExternalAfter(mutation, viewport);
                    receiptObject = viewport;
                    break;
                }
                case OperationKind::DeleteViewport: {
                    if (operation.confirmation != "DELETE_VIEWPORT_AND_ANNOTATIONS") {
                        throw std::invalid_argument(
                            "viewport deletion requires confirm=DELETE_VIEWPORT_AND_ANNOTATIONS");
                    }
                    MCObjectHandle sheet = ResolveSheetReference(
                        sdk, operation.sheetLayerRef, localObjects, "viewport sheet layer");
                    MCObjectHandle viewport = ResolveReference(
                        sdk, operation.targetRef, localObjects, kViewportNode, "viewport target");
                    if (operation.targetRef.rfind("$", 0u) == 0u) {
                        throw std::invalid_argument("viewport deletion requires an external UUID target");
                    }
                    if (sdk.ParentObject(viewport) != sheet) {
                        throw std::invalid_argument("viewport target is not on the exact requested sheet layer");
                    }
                    const std::string uuid = ObjectUuid(sdk, viewport);
                    const auto mutation = transaction.TrackExternalBefore(
                        viewport, Transactions::ObjectFamily::Viewport);
                    sdk.DeleteObject(viewport, false);
                    if (sdk.GetObjectByUuid(TXString(uuid.c_str()))) {
                        throw std::runtime_error("Vectorworks did not delete the exact viewport UUID");
                    }
                    transaction.TrackExternalDeleted(mutation);
                    receiptUuids.push_back(uuid);
                    continue;
                }
                case OperationKind::CreateViewportAnnotation: {
                    MCObjectHandle sheet = ResolveSheetReference(
                        sdk, operation.sheetLayerRef, localObjects, "annotation sheet layer");
                    MCObjectHandle viewport = ResolveReference(
                        sdk, operation.viewportRef, localObjects, kViewportNode, "annotation viewport");
                    if (sdk.ParentObject(viewport) != sheet) {
                        throw std::invalid_argument("annotation viewport is not on the exact requested sheet layer");
                    }
                    MCObjectHandle annotation = CreateAnnotationObject(sdk, operation);
                    CreatedObjectGuard guard(sdk, annotation);
                    if (operation.hasName &&
                        sdk.SetObjectName(annotation, TXString(operation.name.c_str())) != noError) {
                        throw std::runtime_error("Vectorworks rejected the viewport annotation name");
                    }
                    if (operation.hasClassName) {
                        sdk.SetObjectClass(annotation, ResolveClass(sdk, operation.className));
                    }
                    if (!sdk.AddViewportAnnotationObject(viewport, annotation)) {
                        throw std::runtime_error("Vectorworks rejected the viewport annotation container insertion");
                    }
                    VerifyAnnotation(sdk, annotation, viewport, sheet, operation);
                    const Operation expectedFields = operation;
                    const std::string expectedViewportUuid = ObjectUuid(sdk, viewport);
                    const std::string expectedSheetUuid = ObjectUuid(sdk, sheet);
                    transaction.AdoptFinal(
                        annotation,
                        Transactions::ObjectFamily::ViewportAnnotation,
                        sdk.GetObjectTypeN(annotation),
                        [&sdk, expectedFields, expectedViewportUuid, expectedSheetUuid](MCObjectHandle verified) {
                            MCObjectHandle verifiedSheet = RequireSheetLayer(
                                sdk, expectedSheetUuid, "annotation sheet layer");
                            MCObjectHandle verifiedViewport = RequireUuidObject(
                                sdk, expectedViewportUuid, kViewportNode, "annotation viewport");
                            VerifyAnnotation(sdk, verified, verifiedViewport, verifiedSheet, expectedFields);
                        });
                    guard.Release();
                    receiptObject = annotation;
                    break;
                }
                case OperationKind::UpdateViewportAnnotation: {
                    MCObjectHandle sheet = ResolveSheetReference(
                        sdk, operation.sheetLayerRef, localObjects, "annotation sheet layer");
                    MCObjectHandle viewport = ResolveReference(
                        sdk, operation.viewportRef, localObjects, kViewportNode, "annotation viewport");
                    if (operation.targetRef.rfind("$", 0u) == 0u) {
                        throw std::invalid_argument("viewport annotation updates require an external UUID target");
                    }
                    MCObjectHandle annotation = RequireAnyUuidObject(
                        sdk,
                        StripUuidPrefix(operation.targetRef),
                        "viewport annotation target");
                    MCObjectHandle group = sdk.GetViewportGroup(viewport, kViewportGroupAnnotation);
                    if (sdk.ParentObject(viewport) != sheet || !group || sdk.ParentObject(annotation) != group) {
                        throw std::invalid_argument("annotation target is not in the exact viewport annotation container");
                    }
                    const auto mutation = transaction.TrackExternalBefore(
                        annotation, Transactions::ObjectFamily::ViewportAnnotation);
                    ApplyAnnotationFields(sdk, annotation, operation);
                    VerifyAnnotation(sdk, annotation, viewport, sheet, operation);
                    transaction.TrackExternalAfter(mutation, annotation);
                    receiptObject = annotation;
                    break;
                }
                case OperationKind::DeleteViewportAnnotation: {
                    if (operation.confirmation != "DELETE_VIEWPORT_ANNOTATION") {
                        throw std::invalid_argument(
                            "viewport annotation deletion requires confirm=DELETE_VIEWPORT_ANNOTATION");
                    }
                    MCObjectHandle sheet = ResolveSheetReference(
                        sdk, operation.sheetLayerRef, localObjects, "annotation sheet layer");
                    MCObjectHandle viewport = ResolveReference(
                        sdk, operation.viewportRef, localObjects, kViewportNode, "annotation viewport");
                    const std::string targetUuid = StripUuidPrefix(operation.targetRef);
                    MCObjectHandle annotation = sdk.GetObjectByUuid(TXString(targetUuid.c_str()));
                    if (!annotation) {
                        throw std::invalid_argument("viewport annotation target UUID was not found");
                    }
                    MCObjectHandle group = sdk.GetViewportGroup(viewport, kViewportGroupAnnotation);
                    if (sdk.ParentObject(viewport) != sheet || !group || sdk.ParentObject(annotation) != group) {
                        throw std::invalid_argument("annotation target is not in the exact viewport annotation container");
                    }
                    const auto mutation = transaction.TrackExternalBefore(
                        annotation, Transactions::ObjectFamily::ViewportAnnotation);
                    sdk.DeleteObject(annotation, false);
                    if (sdk.GetObjectByUuid(TXString(targetUuid.c_str()))) {
                        throw std::runtime_error("Vectorworks did not delete the exact viewport annotation UUID");
                    }
                    transaction.TrackExternalDeleted(mutation);
                    receiptUuids.push_back(targetUuid);
                    continue;
                }
            }

            if (!receiptObject) {
                throw std::logic_error("documentation operation did not produce a receipt object");
            }
            const std::string receiptUuid = ObjectUuid(sdk, receiptObject);
            receiptUuids.push_back(receiptUuid);
            if (!operation.localRef.empty()) {
                if (!localObjects.emplace(operation.localRef, receiptObject).second) {
                    throw std::invalid_argument("duplicate documentation local_ref: " + operation.localRef);
                }
            }
        }

        activeLayerRestorer.RestoreAndVerify();
        const Transactions::TransactionReceipt transactionReceipt = transaction.Commit();
        const DocumentBinding finalBinding = ReadDocumentBinding(sdk);
        std::string json = "{\"transaction\":{\"committed\":" +
            std::string(transactionReceipt.committed ? "true" : "false") +
            ",\"end_undo_event_succeeded\":" +
            std::string(transactionReceipt.endUndoEventSucceeded ? "true" : "false") +
            ",\"operation_count\":" + std::to_string(operations.size()) +
            ",\"active_layer_restored\":true,\"operations\":[";
        for (std::size_t index = 0; index < operations.size(); ++index) {
            if (index != 0u) {
                json += ",";
            }
            json += "{\"index\":" + std::to_string(index + 1u) +
                ",\"op\":" + JsonString(OperationKindName(operations[index].kind)) +
                ",\"uuid\":" + JsonString(receiptUuids[index]) +
                ",\"verified\":true";
            if (!operations[index].localRef.empty()) {
                json += ",\"local_ref\":" + JsonString(operations[index].localRef);
            }
            json += "}";
        }
        json += "]},\"binding\":" + DocumentBindingJson(finalBinding) +
            ",\"coordinate_units\":\"mm\",\"idempotency_key\":" +
            JsonString(idempotencyKey) + ",\"plan_hash\":" + JsonString(planFingerprint) + "}";
        gCachedReceipts.push_back({
            initialBinding.documentFingerprint,
            idempotencyKey,
            planFingerprint,
            json,
        });
        while (gCachedReceipts.size() > kMaxCachedReceipts) {
            gCachedReceipts.pop_front();
        }
        return json;
    } catch (...) {
        transaction.RollbackAndRethrow(std::current_exception());
    }
}

}  // namespace VectorworksMCP::Documentation
