#include "StdAfx.h"

// 0.x CAD handlers (behaviour kept for the 0.7 host). Since 1.0 the runtime
// plumbing (transport, scheduler, UI gate, ping, documents, view/capture,
// scripting, organisation) lives in separate files; see BridgeRuntime.cpp.

#include "BridgeProtocol.hpp"
#include "DocumentRegistry.hpp"
#include "JsonParams.hpp"
#include "LegacyHandlers.hpp"
#include "NativeIOHandlers.hpp"
#include "ViewDocumentHandlers.hpp"

#if defined(SDK_VERSION)
#include "Interfaces/VectorWorks/Extension/ISpaceObjectSupport.h"
#include "BimObjectHandlers.hpp"
#include "CapabilityRegistry.hpp"
#include "DocumentationHandlers.hpp"
#include "NativeObjectFactory.hpp"
#include "NativeTransaction.hpp"
#include "ParametricObjectAdapter.hpp"
#include "ResourceWorksheetHandlers.hpp"
#include "SpaceObjectHandlers.hpp"
#include "WallInserts.hpp"
#endif

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <exception>
#include <functional>
#include <iomanip>
#include <limits>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#if defined(SDK_VERSION)
#define VECTORWORKS_MCP_HAS_SDK 1
#else
#define VECTORWORKS_MCP_HAS_SDK 0
#endif

namespace VectorworksMCP {

namespace {

bool IsWhitespace(char ch) {
    return ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t';
}

#if VECTORWORKS_MCP_HAS_SDK

std::string TxToUtf8(const TXString& value) {
    return value.GetStdString();
}

constexpr std::size_t kMaxPropertyValueChars = 1024;

// 1.0 (C1): handle ids are opaque table keys {handle_id -> uuid, document};
// raw MCObjectHandle values are never parsed back from client input.
std::string HandleId(MCObjectHandle handle) {
    return Handles::IdFor(handle);
}

std::string ObjectUuidString(MCObjectHandle handle) {
    if (!handle) {
        return "";
    }
    TXString uuid;
    if (gSDK->GetObjectUuid(handle, uuid)) {
        return TxToUtf8(uuid);
    }
    return "";
}

class UnregisteredCreatedObjectGuard {
public:
    explicit UnregisteredCreatedObjectGuard(MCObjectHandle object) : object_(object) {}
    ~UnregisteredCreatedObjectGuard() {
        if (object_) {
            gSDK->DeleteObject(object_, false);
        }
    }
    void Release() { object_ = nullptr; }
    UnregisteredCreatedObjectGuard(const UnregisteredCreatedObjectGuard&) = delete;
    UnregisteredCreatedObjectGuard& operator=(const UnregisteredCreatedObjectGuard&) = delete;

private:
    MCObjectHandle object_;
};

void EndUndoEventOrThrow(const char* operation) {
    if (!gSDK->EndUndoEvent()) {
        throw std::runtime_error(
            std::string("Vectorworks failed to commit the undo event for ") + operation);
    }
}

std::string ObjectTypeName(short type) {
    switch (type) {
        case kLineNode:
            return "line";
        case kBoxNode:
            return "rect";
        case kOvalNode:
            return "oval";
        case kPolygonNode:
            return "polygon";
        case kArcNode:
            return "arc";
        case kFreehandPolygonNode:
            return "freehand";
        case kTextNode:
            return "text";
        case kGroupNode:
            return "group";
        case kSymbolNode:
            return "symbol";
        case kWorksheetNode:
            return "worksheet";
        case kPolylineNode:
            return "polyline";
        case kExtrudeNode:
            return "extrude";
        case kLayerNode:
            return "layer";
        case dimHeaderNode:
            return "dimension";
        case kWallNode:
            return "wall";
        case kSlabNode:
            return "slab";
        case kRoofContainerNode:
            return "roof";
        case kParametricNode:
            return "parametric";
        default:
            return "type_" + std::to_string(static_cast<int>(type));
    }
}

std::string ParametricPluginName(MCObjectHandle object) {
    if (!object || gSDK->GetObjectTypeN(object) != kParametricNode) {
        return "";
    }
    try {
        VWFC::VWObjects::VWParametricObj parametric(object);
        return TxToUtf8(parametric.GetParametricName());
    } catch (...) {
        return "";
    }
}

bool IsSpaceObject(MCObjectHandle object) {
    return ParametricPluginName(object) == "Space";
}

std::string SemanticObjectTypeName(MCObjectHandle object) {
    if (gSDK->GetObjectTypeN(object) == kParametricNode) {
        const std::string pluginName = ToLower(ParametricPluginName(object));
        if (pluginName == "space" || pluginName == "door" || pluginName == "window") {
            return pluginName;
        }
    }
    return ObjectTypeName(gSDK->GetObjectTypeN(object));
}

bool MatchesObjectType(MCObjectHandle object, std::string requestedType) {
    requestedType = ToLower(requestedType);
    if (requestedType.empty()) {
        return true;
    }
    if (requestedType == "rectangle" || requestedType == "box") {
        requestedType = "rect";
    } else if (requestedType == "linear_dimension") {
        requestedType = "dimension";
    }
    if (requestedType == "space" || requestedType == "door" || requestedType == "window") {
        return ToLower(ParametricPluginName(object)) == requestedType;
    }
    if (ObjectTypeName(gSDK->GetObjectTypeN(object)) == requestedType) {
        return true;
    }
    // Any parametric plug-in name ("Stair", "custom stair", "Custom_Stair"),
    // compared case-insensitively ignoring spaces, '_' and '-'.
    if (gSDK->GetObjectTypeN(object) != kParametricNode) {
        return false;
    }
    const auto squash = [](const std::string& text) {
        std::string out;
        for (char c : text) {
            if (c == ' ' || c == '_' || c == '-') continue;
            out.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
        }
        return out;
    };
    const std::string wanted = squash(requestedType);
    return !wanted.empty() && squash(ParametricPluginName(object)) == wanted;
}

bool IsUserVisibleObjectType(short type) {
    return type != kTermNode && type != kLayerNode && type != kHeaderNode && type != kUndoPlaceholderNode;
}

std::string LayerNameForObject(MCObjectHandle object) {
    MCObjectHandle layer = gSDK->SearchForAncestorType(kLayerNode, object);
    if (!layer) {
        return "";
    }
    TXString name;
    gSDK->GetObjectName(layer, name);
    return TxToUtf8(name);
}

std::string ClassNameForObject(MCObjectHandle object) {
    if (!object) {
        return "";
    }
    const InternalIndex classId = gSDK->GetObjectClass(object);
    if (!gSDK->ValidClass(classId)) {
        return "";
    }
    TXString name;
    gSDK->ClassIDToName(classId, name);
    return TxToUtf8(name);
}

std::string RgbStringFromColorRef(ColorRef colorRef) {
    RGBColor rgb = {};
    gSDK->ColorIndexToRGB(colorRef, rgb);
    return std::to_string(static_cast<unsigned int>(rgb.red)) + "," +
        std::to_string(static_cast<unsigned int>(rgb.green)) + "," +
        std::to_string(static_cast<unsigned int>(rgb.blue));
}

std::string ObjectJson(MCObjectHandle object, const std::set<std::string>* fields = nullptr) {
    const auto wants = [&](const char* key) { return !fields || fields->count(key) != 0u; };
    const short type = gSDK->GetObjectTypeN(object);
    const bool needsSemantic = wants("type") || wants("native_type") || wants("room_id") ||
    wants("height") || wants("net_area") || wants("gross_area");
    const std::string semanticType = needsSemantic ? SemanticObjectTypeName(object) : "";
    const bool isSpace = semanticType == "space";
    const std::string pluginName = wants("plugin_name") && type == kParametricNode ? ParametricPluginName(object) : "";
    TXString name;
    if (wants("name")) gSDK->GetObjectName(object, name);

    std::string json = "{";
    if (wants("handle")) json += "\"handle\":" + JsonString(HandleId(object));
    const auto uuid = wants("uuid") ? ObjectUuidString(object) : "";
    if (!uuid.empty()) {
        json += ",\"uuid\":";
        json += JsonString(uuid);
    }
    if (wants("type")) {
        json += ",\"type\":";
        json += JsonString(semanticType);
    }
    if (wants("type_id")) {
        json += ",\"type_id\":";
        json += std::to_string(static_cast<int>(type));
    }
    if (!pluginName.empty()) {
        json += ",\"plugin_name\":";
        json += JsonString(pluginName);
    }
    if (wants("native_type") && semanticType != ObjectTypeName(type)) {
        json += ",\"native_type\":";
        json += JsonString(ObjectTypeName(type));
    }
    if (wants("name")) {
        json += ",\"name\":";
        json += JsonString(TxToUtf8(name));
    }

    if (wants("parent_uuid") || wants("host")) {
        if (MCObjectHandle hostWall = WallInserts::HostWall(object)) {
            const std::string wallUuid = ObjectUuidString(hostWall);
            if (wants("parent_uuid")) {
                json += ",\"parent_uuid\":";
                json += JsonString(wallUuid);
            }
            if (wants("host")) {
                json += ",\"host\":\"wall\"";
            }
        }
    }
    if ((semanticType == "door" || semanticType == "window") && (wants("width") || wants("height"))) {
        try {
            VWFC::VWObjects::VWParametricObj parametric(object);
            const double width = parametric.GetParamReal(TXString("Width"));
            const double height = parametric.GetParamReal(TXString("Height"));
            if (wants("width") && std::isfinite(width) && width > 0.0) {
                json += ",\"width\":";
                json += JsonNumber(width);
            }
            if (wants("height") && std::isfinite(height) && height > 0.0) {
                json += ",\"height\":";
                json += JsonNumber(height);
            }
        } catch (...) {
        }
    }

    if (type == kTextNode && wants("text")) {
        json += ",\"text\":";
        json += JsonString(TxToUtf8(gSDK->GetTextChars(object)));
    }

    if (isSpace) {
        VWFC::VWObjects::VWParametricObj parametric(object);
        if (wants("room_id")) {
            json += ",\"room_id\":";
            json += JsonString(TxToUtf8(parametric.GetParamString(TXString("11_Room ID"))));
        }
        if (wants("height")) {
            json += ",\"height\":";
            json += JsonNumber(parametric.GetParamReal(TXString("11_Net Height")));
        }
        if (wants("net_area") || wants("gross_area")) {
            VCOMPtr<VectorWorks::Extension::ISpaceObjectSupport> support(
            VectorWorks::Extension::IID_VCOMSpace);
            WorldCoord netArea = 0.0;
            WorldCoord grossArea = 0.0;
            if (support && support->NetArea(object, netArea) && support->GrossArea(object, grossArea)) {
                if (wants("net_area")) {
                    json += ",\"net_area\":";
                    json += JsonNumber(static_cast<double>(netArea));
                }
                if (wants("gross_area")) {
                    json += ",\"gross_area\":";
                    json += JsonNumber(static_cast<double>(grossArea));
                }
            }
        }
    }

    const auto layerName = wants("layer") ? LayerNameForObject(object) : "";
    if (!layerName.empty()) {
        json += ",\"layer\":";
        json += JsonString(layerName);
    }
    const auto className = wants("class") || wants("class_name") ? ClassNameForObject(object) : "";
    if (!className.empty()) {
        if (wants("class")) {
            json += ",\"class\":";
            json += JsonString(className);
        }
        if (wants("class_name")) {
            json += ",\"class_name\":";
            json += JsonString(className);
        }
    }

    ObjectColorType colors = {};
    if ((wants("fillColor") || wants("penColor")) && gSDK->GetColor(object, colors)) {
        if (wants("fillColor")) {
            json += ",\"fillColor\":";
            json += JsonString(RgbStringFromColorRef(colors.fillFore));
        }
        if (wants("penColor")) {
            json += ",\"penColor\":";
            json += JsonString(RgbStringFromColorRef(colors.penFore));
        }
    }
    if (wants("lineWeight")) {
        json += ",\"lineWeight\":";
        json += std::to_string(static_cast<int>(gSDK->GetLineWeight(object)));
    }
    if (wants("fillPattern")) {
        json += ",\"fillPattern\":";
        json += std::to_string(static_cast<int>(gSDK->GetFillPat(object)));
    }
    if (wants("opacity")) {
        json += ",\"opacity\":";
        json += std::to_string(static_cast<int>(gSDK->GetOpacity(object)));
    }

    if (wants("bounds")) {
        WorldRect bounds;
        gSDK->GetObjectBounds(object, bounds);
        json += ",\"bounds\":{\"top_left\":[";
        json += JsonNumber(bounds.Left());
        json += ",";
        json += JsonNumber(bounds.Top());
        json += "],\"bottom_right\":[";
        json += JsonNumber(bounds.Right());
        json += ",";
        json += JsonNumber(bounds.Bottom());
        json += "]}";
    }
    json += "}";
    if (json.size() > 1u && json[1] == ',') json.erase(1u, 1u);
    return json;
}

std::string ObjectListJson(const std::vector<MCObjectHandle>& objects) {
    std::string json = "[";
    for (std::size_t index = 0; index < objects.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        json += ObjectJson(objects[index]);
    }
    json += "]";
    return json;
}

std::string CountMapJson(const std::map<std::string, int>& counts) {
    std::string json = "{";
    bool first = true;
    for (const auto& [key, value] : counts) {
        if (!first) {
            json += ",";
        }
        first = false;
        json += JsonString(key);
        json += ":";
        json += std::to_string(value);
    }
    json += "}";
    return json;
}

std::string NestedCountMapJson(const std::map<std::string, std::map<std::string, int>>& counts) {
    std::string json = "{";
    bool first = true;
    for (const auto& [key, nested] : counts) {
        if (!first) {
            json += ",";
        }
        first = false;
        json += JsonString(key);
        json += ":";
        json += CountMapJson(nested);
    }
    json += "}";
    return json;
}

std::vector<MCObjectHandle> CollectLayerHandles() {
    std::vector<MCObjectHandle> layers;
    gSDK->ForEachLayerN([&](MCObjectHandle layer) {
        if (layer) {
            layers.push_back(layer);
        }
    });
    return layers;
}

std::vector<std::string> CollectLayerNames() {
    std::vector<std::string> names;
    for (MCObjectHandle layer : CollectLayerHandles()) {
        TXString name;
        gSDK->GetObjectName(layer, name);
        names.push_back(TxToUtf8(name));
    }
    return names;
}

std::string HandleGetDocumentInfo() {
    std::string filename = "Untitled.vwx";
    std::string filepath;

    VectorWorks::Filing::IFileIdentifierPtr activeFile(VectorWorks::Filing::IID_FileIdentifier);
    bool saved = false;
    if (activeFile && gSDK->GetActiveDocument(&activeFile, saved)) {
        TXString name;
        TXString path;
        activeFile->GetFileName(name);
        activeFile->GetFileFullPath(path);
        const auto utf8Name = TxToUtf8(name);
        if (!utf8Name.empty()) {
            filename = utf8Name;
        }
        filepath = TxToUtf8(path);
    }

    const auto layerNames = CollectLayerNames();
    int totalObjects = 0;
    for (MCObjectHandle layer : CollectLayerHandles()) {
        // Includes doors/windows/symbols inserted in walls.
        WallInserts::ForEachLayerObject(layer, [&](MCObjectHandle object) {
            if (IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
                ++totalObjects;
            }
        });
    }

    const Documentation::DocumentBinding binding = Documentation::ReadDocumentBinding(*gSDK);
    std::string json = "{\"filename\":";
    json += JsonString(filename);
    json += ",\"filepath\":";
    json += JsonString(filepath);
    json += ",\"layers\":[";
    for (std::size_t index = 0; index < layerNames.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        json += JsonString(layerNames[index]);
    }
    json += "],\"layer_count\":";
    json += std::to_string(layerNames.size());
    json += ",\"total_objects\":";
    json += std::to_string(totalObjects);
    json += ",\"dirty\":";
    json += binding.dirty ? "true" : "false";
    json += ",\"document_fingerprint\":";
    json += JsonString(binding.documentFingerprint);
    json += ",\"document_generation\":";
    json += std::to_string(binding.documentGeneration);
    json += ",\"bridge_session_id\":";
    json += JsonString(binding.bridgeSessionId);
    json += ",\"active_layer_uuid\":";
    json += JsonString(binding.activeLayerUuid);
    json += ",\"active_layer_name\":";
    json += JsonString(binding.activeLayerName);
    json += ",\"binding\":";
    json += Documentation::DocumentBindingJson(binding);
    json += "}";
    return json;
}

std::string HandleGetLayers() {
    std::string json = "[";
    const auto layers = CollectLayerHandles();
    for (std::size_t index = 0; index < layers.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        TXString name;
        gSDK->GetObjectName(layers[index], name);
        json += "{\"name\":";
        json += JsonString(TxToUtf8(name));
        json += ",\"uuid\":";
        json += JsonString(ObjectUuidString(layers[index]));
        Sint16 layerType = kLayerDesign;
        TVariableBlock layerTypeBlock(layerType);
        if (!gSDK->GetObjectVariable(layers[index], ovLayerType, layerTypeBlock) ||
            !layerTypeBlock.GetSint16(layerType)) {
            throw std::runtime_error("Vectorworks did not return the layer kind");
        }
        json += ",\"kind\":";
        json += JsonString(layerType == kLayerSheet ? "sheet" : "design");
        json += ",\"visible\":";
        json += gSDK->IsVisible(layers[index]) ? "true" : "false";
        json += "}";
    }
    json += "]";
    return json;
}

Documentation::ExpectedTargetBinding ParseDocumentationTargetBinding(const Params& params) {
    Documentation::ExpectedTargetBinding expected;
    expected.filePath = GetStringParam(params, "expected_file_path");
    expected.documentFingerprint = GetStringParam(params, "expected_document_fingerprint");
    expected.documentGeneration = static_cast<std::uint64_t>(GetRequiredBoundedIntParam(
        params, "expected_document_generation", 1, std::numeric_limits<int>::max()));
    expected.bridgeSessionId = GetStringParam(params, "expected_bridge_session_id");
    expected.activeLayerUuid = GetStringParam(params, "expected_active_layer_uuid");
    expected.activeLayerName = GetStringParam(params, "expected_active_layer_name");
    const auto dirty = params.find("expected_dirty");
    if (dirty != params.end()) {
        expected.dirty = GetBoolParam(params, "expected_dirty", false);
        expected.hasDirty = true;
    }
    return expected;
}

std::string HandleGetSheetLayers(const Params& params) {
    return Documentation::ReadSheetLayers(
        *gSDK,
        ParseDocumentationTargetBinding(params),
        static_cast<std::size_t>(GetBoundedIntParam(params, "offset", 0, 0, 1000000)),
        static_cast<std::size_t>(GetBoundedIntParam(params, "limit", 100, 1, 500)));
}

std::string HandleGetViewports(const Params& params) {
    return Documentation::ReadViewports(
        *gSDK,
        ParseDocumentationTargetBinding(params),
        GetStringParam(params, "sheet_layer_uuid"),
        static_cast<std::size_t>(GetBoundedIntParam(params, "offset", 0, 0, 1000000)),
        static_cast<std::size_t>(GetBoundedIntParam(params, "limit", 100, 1, 500)));
}

std::string HandleGetViewportAnnotations(const Params& params) {
    return Documentation::ReadViewportAnnotations(
        *gSDK,
        ParseDocumentationTargetBinding(params),
        GetStringParam(params, "sheet_layer_uuid"),
        GetStringParam(params, "viewport_uuid"),
        static_cast<std::size_t>(GetBoundedIntParam(params, "offset", 0, 0, 1000000)),
        static_cast<std::size_t>(GetBoundedIntParam(params, "limit", 100, 1, 500)));
}

MCObjectHandle FindLayerByName(const std::string& layerName) {
    if (layerName.empty()) {
        return nullptr;
    }
    for (MCObjectHandle layer : CollectLayerHandles()) {
        TXString name;
        gSDK->GetObjectName(layer, name);
        if (TxToUtf8(name) == layerName) {
            return layer;
        }
    }
    return nullptr;
}

void CollectObjectsInLayer(
    MCObjectHandle layer,
    const std::string& objectType,
    int limit,
    std::vector<MCObjectHandle>& outObjects) {
    WallInserts::ForEachLayerObject(layer, [&](MCObjectHandle object) -> bool {
        if (static_cast<int>(outObjects.size()) >= limit) {
            return false;
        }
        const short type = gSDK->GetObjectTypeN(object);
        if (IsUserVisibleObjectType(type) && MatchesObjectType(object, objectType)) {
            outObjects.push_back(object);
        }
        return true;
    });
}

std::string HandleGetObjects(const Params& params) {
    const int limit = GetBoundedIntParam(params, "limit", 100, 1, 1000);
    const std::string layerName = GetStringParam(params, "layer");
    const std::string objectType = GetStringParam(params, "object_type");
    std::vector<MCObjectHandle> objects;
    objects.reserve(static_cast<std::size_t>(limit));

    if (!layerName.empty()) {
        MCObjectHandle layer = FindLayerByName(layerName);
        if (!layer) {
            throw std::runtime_error("Layer '" + layerName + "' not found");
        }
        CollectObjectsInLayer(layer, objectType, limit, objects);
        return ObjectListJson(objects);
    }

    for (MCObjectHandle layer : CollectLayerHandles()) {
        if (static_cast<int>(objects.size()) >= limit) {
            break;
        }
        CollectObjectsInLayer(layer, objectType, limit, objects);
    }
    return ObjectListJson(objects);
}

std::string HandleDrawingSummary(const Params& params) {
    const int scanLimit = GetBoundedIntParam(params, "scan_limit", GetBoundedIntParam(params, "limit", 1000, 1, 100000), 1, 100000);
    const bool includeExamples = GetBoolParam(params, "include_examples", true);
    const int exampleLimit = GetBoundedIntParam(params, "example_limit", 20, 0, 100);
    const std::string layerFilter = GetStringParam(params, "layer");
    const std::string objectTypeFilter = GetStringParam(params, "object_type");

    std::map<std::string, int> byType;
    std::map<std::string, int> byLayer;
    std::map<std::string, int> byClass;
    std::map<std::string, std::map<std::string, int>> byLayerType;
    std::vector<MCObjectHandle> examples;
    int scanned = 0;
    int namedCount = 0;
    bool truncated = false;
    bool hasBounds = false;
    double left = 0.0;
    double top = 0.0;
    double right = 0.0;
    double bottom = 0.0;

    for (MCObjectHandle layer : CollectLayerHandles()) {
        TXString txLayerName;
        gSDK->GetObjectName(layer, txLayerName);
        const std::string layerName = TxToUtf8(txLayerName);
        if (!layerFilter.empty() && layerName != layerFilter) {
            continue;
        }
        std::vector<MCObjectHandle> layerObjects;
        WallInserts::ForEachLayerObject(layer, [&](MCObjectHandle object) { layerObjects.push_back(object); });
        for (MCObjectHandle object : layerObjects) {
            const short type = gSDK->GetObjectTypeN(object);
            if (!IsUserVisibleObjectType(type) || !MatchesObjectType(object, objectTypeFilter)) {
                continue;
            }
            if (scanned >= scanLimit) {
                truncated = true;
                break;
            }
            ++scanned;
            const std::string objectType = SemanticObjectTypeName(object);
            ++byType[objectType];
            ++byLayer[layerName.empty() ? "unknown" : layerName];
            ++byLayerType[layerName.empty() ? "unknown" : layerName][objectType];
            const std::string className = ClassNameForObject(object);
            if (!className.empty()) {
                ++byClass[className];
            }
            TXString txName;
            gSDK->GetObjectName(object, txName);
            if (!TxToUtf8(txName).empty()) {
                ++namedCount;
            }
            if (includeExamples && static_cast<int>(examples.size()) < exampleLimit) {
                examples.push_back(object);
            }

            WorldRect bounds;
            gSDK->GetObjectBounds(object, bounds);
            const double objLeft = std::min(bounds.Left(), bounds.Right());
            const double objRight = std::max(bounds.Left(), bounds.Right());
            const double objTop = std::min(bounds.Top(), bounds.Bottom());
            const double objBottom = std::max(bounds.Top(), bounds.Bottom());
            if (!hasBounds) {
                left = objLeft;
                right = objRight;
                top = objTop;
                bottom = objBottom;
                hasBounds = true;
            } else {
                left = std::min(left, objLeft);
                right = std::max(right, objRight);
                top = std::min(top, objTop);
                bottom = std::max(bottom, objBottom);
            }
        }
        if (truncated) {
            break;
        }
    }

    const auto layers = CollectLayerHandles();
    std::string json = "{\"ok\":true,\"tool\":\"vw_drawing_summary\",\"native_summary\":true";
    json += ",\"query\":{\"layer\":";
    json += JsonString(layerFilter);
    json += ",\"object_type\":";
    json += JsonString(objectTypeFilter);
    json += ",\"scan_limit\":";
    json += std::to_string(scanLimit);
    json += ",\"include_examples\":";
    json += includeExamples ? "true" : "false";
    json += ",\"example_limit\":";
    json += std::to_string(exampleLimit);
    json += ",\"source\":\"native_drawing_summary\"}";
    json += ",\"document\":";
    json += HandleGetDocumentInfo();
    json += ",\"layers\":";
    json += HandleGetLayers();
    json += ",\"layer_count\":";
    json += std::to_string(layers.size());
    json += ",\"objects_returned\":";
    json += std::to_string(scanned);
    json += ",\"objects_scanned\":";
    json += std::to_string(scanned);
    json += ",\"possibly_truncated\":";
    json += truncated ? "true" : "false";
    json += ",\"named_objects_returned\":";
    json += std::to_string(namedCount);
    json += ",\"counts_by_type\":";
    json += CountMapJson(byType);
    json += ",\"counts_by_layer\":";
    json += CountMapJson(byLayer);
    json += ",\"counts_by_layer_type\":";
    json += NestedCountMapJson(byLayerType);
    json += ",\"counts_by_class\":";
    json += CountMapJson(byClass);
    json += ",\"bounds\":";
    if (hasBounds) {
        json += "{\"left\":";
        json += JsonNumber(left);
        json += ",\"top\":";
        json += JsonNumber(top);
        json += ",\"right\":";
        json += JsonNumber(right);
        json += ",\"bottom\":";
        json += JsonNumber(bottom);
        json += "}";
    } else {
        json += "null";
    }
    if (includeExamples) {
        json += ",\"examples\":";
        json += ObjectListJson(examples);
    }
    json += "}";
    return json;
}

std::vector<MCObjectHandle> CollectSelectedObjects() {
    std::vector<MCObjectHandle> selected;
    std::unordered_set<MCObjectHandle> seen;
    const auto visit = [&](MCObjectHandle object) {
        if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object)) && gSDK->IsSelected(object) &&
            seen.insert(object).second) {
            selected.push_back(object);
        }
    };
    gSDK->ForEachObjectN(allObjects + descendIntoAll + descendIntoViewports + descendIntoAuxLists, [&](MCObjectHandle object) {
        visit(object);
        WallInserts::ForEachInsert(object, visit);
    });
    return selected;
}

std::vector<MCObjectHandle> CollectObjectsByCriteria(const std::string& criteria, int limit = 1000) {
    std::vector<MCObjectHandle> objects;
    gSDK->ForEachObjectInCriteria(TXString(criteria.c_str()), [&](MCObjectHandle object) {
        if (static_cast<int>(objects.size()) >= limit) {
            return;
        }
        if (!object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
            return;
        }
        if (std::find(objects.begin(), objects.end(), object) == objects.end()) {
            objects.push_back(object);
        }
    });
    return objects;
}

std::optional<std::string> ExactNameFromCriteria(const std::string& criteria);

bool IsAllCriteria(const std::string& criteria) {
    std::string squashed;
    for (char c : criteria) {
        if (c == ' ' || c == '(' || c == ')') continue;
        squashed.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
    }
    return squashed == "ALL";
}

std::string HandleFindObjects(const Params& params) {
    const std::string criteria = TrimCopy(GetStringParam(params, "criteria", "ALL"));
    const std::string layer = TrimCopy(GetStringParam(params, "layer"));
    const std::string objectType = TrimCopy(GetStringParam(params, "object_type"));
    const int limit = GetBoundedIntParam(params, "limit", 100, 1, 1000);
    if (criteria.empty()) {
        throw std::invalid_argument("criteria is required");
    }
    if (!layer.empty()) {
        bool layerFound = false;
        for (MCObjectHandle layerHandle : CollectLayerHandles()) {
            TXString layerName;
            gSDK->GetObjectName(layerHandle, layerName);
            if (TxToUtf8(layerName) == layer) {
                layerFound = true;
                break;
            }
        }
        if (!layerFound) {
            throw std::invalid_argument("layer filter was not found: " + layer);
        }
    }
    std::vector<MCObjectHandle> filtered;
    filtered.reserve(static_cast<std::size_t>(limit));
    const auto collect = [&](MCObjectHandle object) {
        if (!object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object)) ||
            static_cast<int>(filtered.size()) >= limit) {
            return;
        }
        if (!layer.empty() && LayerNameForObject(object) != layer) {
            return;
        }
        if (!objectType.empty() && !MatchesObjectType(object, objectType)) {
            return;
        }
        filtered.push_back(object);
    };
    const auto exactName = ExactNameFromCriteria(criteria);
    if (exactName) {
        collect(gSDK->GetNamedObject(TXString(exactName->c_str())));
    } else if (IsAllCriteria(criteria)) {
        // "ALL" through the layer walk so wall inserts are included.
        for (MCObjectHandle layerHandle : CollectLayerHandles()) {
            WallInserts::ForEachLayerObject(layerHandle, collect);
        }
    } else {
        gSDK->ForEachObjectInCriteria(TXString(criteria.c_str()), collect);
    }
    return ObjectListJson(filtered);
}

std::optional<std::string> ExactNameFromCriteria(const std::string& criteria) {
    constexpr std::string_view prefix = "((N='";
    constexpr std::string_view suffix = "'))";
    if (criteria.size() <= prefix.size() + suffix.size()) {
        return std::nullopt;
    }
    if (criteria.compare(0, prefix.size(), prefix) != 0) {
        return std::nullopt;
    }
    if (criteria.compare(criteria.size() - suffix.size(), suffix.size(), suffix) != 0) {
        return std::nullopt;
    }
    std::string name = criteria.substr(prefix.size(), criteria.size() - prefix.size() - suffix.size());
    if (name.empty() || name.size() > 255u || name.find('\'') != std::string::npos) {
        return std::nullopt;
    }
    return name;
}

std::vector<MCObjectHandle> CollectObjectsByExactNameCriteria(const std::string& criteria) {
    const auto name = ExactNameFromCriteria(criteria);
    if (!name) {
        throw std::invalid_argument("selection delete criteria must be exact object-name criteria like ((N='Name'))");
    }
    std::vector<MCObjectHandle> objects;
    MCObjectHandle object = gSDK->GetNamedObject(TXString(name->c_str()));
    if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
        objects.push_back(object);
    }
    return objects;
}

std::string HandleQueryObjects(const Params& params) {
    const int limit = GetBoundedIntParam(params, "limit", 100, 1, 1000);
    const int offset = GetBoundedIntParam(params, "offset", 0, 0, 1000000000);
    const std::string mode = GetStringParam(params, "mode", "objects");
    const std::string layerName = GetStringParam(params, "layer");
    const std::string objectType = GetStringParam(params, "object_type");
    // v2: mode "uuids" (implicit when uuids/uuid is given) resolves exact
    // UUIDs directly with GetObjectByUuid (no document scan).
    std::vector<std::string> uuidFilter = GetStringListParam(params, "uuids");
    if (HasParam(params, "uuid")) uuidFilter.push_back(GetStringParam(params, "uuid"));
    const std::string effectiveMode = !uuidFilter.empty() ? std::string("uuids") : mode;
    if (effectiveMode != "objects" && effectiveMode != "criteria" && effectiveMode != "selection" && effectiveMode != "uuids")
        throw std::invalid_argument("query mode must be objects, criteria, selection, or uuids");
    MCObjectHandle layer = layerName.empty() ? nullptr : FindLayerByName(layerName);
    if (!layerName.empty() && !layer) throw std::invalid_argument("layer filter was not found");
    std::set<std::string> fields;
    const int fieldCount = GetBoundedIntParam(params, "field_count", 0, 0, 100);
    for (int index = 1; index <= fieldCount; ++index)
        fields.insert(GetStringParam(params, "field_" + std::to_string(index)));
    if (params.find("expected_document_fingerprint") != params.end())
        Documentation::ValidateTargetBinding(*gSDK, ParseDocumentationTargetBinding(params));
    const auto binding = Documentation::ReadDocumentBinding(*gSDK);
    std::vector<MCObjectHandle> page;
    page.reserve(static_cast<std::size_t>(limit));
    int matched = 0;
    bool hasMore = false;
    std::unordered_set<MCObjectHandle> seen;
    const auto collect = [&](MCObjectHandle object) {
        if (hasMore || !object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) return;
        if (!objectType.empty() && !MatchesObjectType(object, objectType)) return;
        if (!layerName.empty() && LayerNameForObject(object) != layerName) return;
        if (!seen.insert(object).second) return;
        if (matched++ < offset) return;
        if (page.size() == static_cast<std::size_t>(limit)) { hasMore = true; return; }
        page.push_back(object);
    };
    std::vector<std::string> missingUuids;
    if (effectiveMode == "uuids") {
        for (std::string uuid : uuidFilter) {
            uuid = TrimCopy(uuid);
            if (uuid.rfind("uuid:", 0u) == 0u) uuid = uuid.substr(5);
            if (uuid.empty()) continue;
            MCObjectHandle object = gSDK->GetObjectByUuid(TXString(uuid.c_str()));
            if (!object) {
                missingUuids.push_back(uuid);
                continue;
            }
            collect(object);
        }
    } else if (mode == "criteria") {
        const auto criteria = GetStringParam(params, "criteria", "ALL");
        const auto name = ExactNameFromCriteria(criteria);
        if (name) collect(gSDK->GetNamedObject(TXString(name->c_str())));
        else if (IsAllCriteria(criteria)) {
            const auto layers = layer ? std::vector<MCObjectHandle>{layer} : CollectLayerHandles();
            for (MCObjectHandle currentLayer : layers) {
                WallInserts::ForEachLayerObject(currentLayer, [&](MCObjectHandle object) -> bool {
                    collect(object);
                    return !hasMore;
                });
                if (hasMore) break;
            }
        } else gSDK->ForEachObjectInCriteria(TXString(criteria.c_str()), collect);
    } else if (mode == "selection") {
        const auto selected = [&](MCObjectHandle object) { if (object && gSDK->IsSelected(object)) collect(object); };
        gSDK->ForEachObjectN(allObjects + descendIntoAll + descendIntoViewports + descendIntoAuxLists,
            [&](MCObjectHandle object) {
                selected(object);
                WallInserts::ForEachInsert(object, selected);
            });
    } else {
        // Layer walk including objects inserted in walls (doors, windows,
        // symbols), each reported with parent_uuid / host:"wall".
        const auto layers = layer ? std::vector<MCObjectHandle>{layer} : CollectLayerHandles();
        for (MCObjectHandle currentLayer : layers) {
            WallInserts::ForEachLayerObject(currentLayer, [&](MCObjectHandle object) -> bool {
                collect(object);
                return !hasMore;
            });
            if (hasMore) break;
        }
    }
    const std::size_t maxBytes = static_cast<std::size_t>(
        GetClampedIntParam(params, "max_bytes", 8 * 1024 * 1024, 64 * 1024, 14 * 1024 * 1024));
    std::string json = "{\"items\":[";
    std::size_t emitted = 0;
    for (std::size_t index = 0; index < page.size(); ++index) {
        std::string item = ObjectJson(page[index], fields.empty() ? nullptr : &fields);
        if (emitted > 0 && json.size() + item.size() + 1024u > maxBytes) {
            hasMore = true;
            break;
        }
        if (emitted) json += ",";
        json += item;
        ++emitted;
    }
    json += "],\"has_more\":" + std::string(hasMore ? "true" : "false");
    json += ",\"offset\":" + std::to_string(offset);
    json += ",\"returned\":" + std::to_string(emitted);
    json += ",\"next_offset\":" + std::to_string(offset + static_cast<int>(emitted));
    if (effectiveMode == "uuids") {
        json += ",\"missing_uuids\":" + JsonStringArray(missingUuids);
    }
    json += ",\"binding\":" + Documentation::DocumentBindingJson(binding) + "}";
    return json;
}

std::string HandleSelection(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "get"));
    const int limit = GetBoundedIntParam(params, "limit", 1000, 1, 1000);
    if (action == "get") {
        const auto selected = CollectSelectedObjects();
        std::vector<MCObjectHandle> limited;
        limited.reserve(std::min(static_cast<std::size_t>(limit), selected.size()));
        for (MCObjectHandle object : selected) {
            if (static_cast<int>(limited.size()) >= limit) {
                break;
            }
            limited.push_back(object);
        }
        return ObjectListJson(limited);
    }
    if (action == "clear") {
        gSDK->DeselectAll();
        return R"({"cleared":true})";
    }
    if (action == "select") {
        const std::string criteria = GetStringParam(params, "criteria");
        if (criteria.empty()) {
            throw std::invalid_argument("criteria is required for selection select");
        }
        const auto exactName = ExactNameFromCriteria(criteria);
        int matchedCount = 0;
        int selectedCount = 0;
        gSDK->DeselectAll();
        if (exactName) {
            MCObjectHandle object = gSDK->GetNamedObject(TXString(exactName->c_str()));
            if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
                ++matchedCount;
                if (selectedCount < limit) {
                    gSDK->SelectObject(object, true);
                    ++selectedCount;
                }
            }
        } else {
            gSDK->ForEachObjectInCriteria(TXString(criteria.c_str()), [&](MCObjectHandle object) {
                if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
                    ++matchedCount;
                }
                if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object)) && selectedCount < limit) {
                    gSDK->SelectObject(object, true);
                    ++selectedCount;
                }
            });
        }
        return "{\"selected\":" + std::to_string(selectedCount)
            + ",\"matched\":" + std::to_string(matchedCount)
            + ",\"limit\":" + std::to_string(limit)
            + ",\"truncated\":" + (matchedCount > selectedCount ? "true" : "false") + "}";
    }
    if (action == "delete") {
        const std::string criteria = GetStringParam(params, "criteria");
        if (!criteria.empty()) {
            if (GetStringParam(params, "confirm") != "DELETE_EXACT_NAME") {
                throw std::invalid_argument("selection delete with criteria requires confirm='DELETE_EXACT_NAME'");
            }
        } else if (GetStringParam(params, "confirm") != "DELETE_SELECTED") {
            throw std::invalid_argument("selection delete requires confirm='DELETE_SELECTED'");
        }
        const auto targets = criteria.empty() ? CollectSelectedObjects() : CollectObjectsByExactNameCriteria(criteria);
        if (static_cast<int>(targets.size()) > limit) {
            throw std::invalid_argument("selection delete matched more objects than the requested limit");
        }
        if (targets.empty()) {
            return R"({"deleted":0})";
        }
        gSDK->SupportUndoAndRemove();
        gSDK->SetUndoMethod(kUndoSwapObjects);
        gSDK->NameUndoEvent(TXString("Vectorworks MCP delete selection"));
        int deleted = 0;
        try {
            for (MCObjectHandle object : targets) {
                // Exactly one undo registration: DeleteObject(h, true) moves the
                // object into the open undo event. (AddBeforeSwapObject +
                // DeleteObject(h, false) did not restore it on Undo.)
                gSDK->DeleteObject(object, true);
                ++deleted;
            }
            EndUndoEventOrThrow("selection deletion");
        } catch (...) {
            gSDK->UndoAndRemove();
            throw;
        }
        return "{\"deleted\":" + std::to_string(deleted) + "}";
    }
    throw std::invalid_argument("unsupported selection action: " + action);
}

class PointListJsonParser {
public:
    PointListJsonParser(std::string_view text, std::string label)
        : text_(text), label_(std::move(label)) {}

    std::vector<WorldPt> Parse() {
        std::vector<WorldPt> points;
        Expect('[', "must be an array");
        if (ConsumeIf(']')) {
            Finish();
            return points;
        }
        while (true) {
            if (points.size() >= kMaxPoints) {
                throw std::invalid_argument(label_ + " is limited to " + std::to_string(kMaxPoints) + " points");
            }
            Expect('[', "point must be a two-number array");
            const double x = ParseNumber();
            Expect(',', "point must contain x and y");
            const double y = ParseNumber();
            Expect(']', "point must contain exactly two numbers");
            points.emplace_back(x, y);
            if (ConsumeIf(']')) {
                Finish();
                return points;
            }
            Expect(',', "points must be comma-separated");
        }
    }

private:
    static constexpr std::size_t kMaxPoints = 4096u;

    bool AtEnd() const {
        return pos_ >= text_.size();
    }

    void SkipWhitespace() {
        while (!AtEnd() && IsWhitespace(text_[pos_])) {
            ++pos_;
        }
    }

    bool ConsumeIf(char expected) {
        SkipWhitespace();
        if (!AtEnd() && text_[pos_] == expected) {
            ++pos_;
            return true;
        }
        return false;
    }

    void Expect(char expected, const char* message) {
        if (!ConsumeIf(expected)) {
            throw std::invalid_argument(label_ + " " + message);
        }
    }

    double ParseNumber() {
        SkipWhitespace();
        const auto start = pos_;
        if (!AtEnd() && (text_[pos_] == '-' || text_[pos_] == '+')) {
            ++pos_;
        }
        bool hasDigits = false;
        while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
            hasDigits = true;
            ++pos_;
        }
        if (!AtEnd() && text_[pos_] == '.') {
            ++pos_;
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
                hasDigits = true;
                ++pos_;
            }
        }
        if (!hasDigits) {
            throw std::invalid_argument(label_ + " coordinates must be numbers");
        }
        if (!AtEnd() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            ++pos_;
            if (!AtEnd() && (text_[pos_] == '-' || text_[pos_] == '+')) {
                ++pos_;
            }
            const auto exponentStart = pos_;
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
                ++pos_;
            }
            if (exponentStart == pos_) {
                throw std::invalid_argument(label_ + " coordinate exponent is incomplete");
            }
        }
        const double value = std::stod(std::string(text_.substr(start, pos_ - start)));
        if (!std::isfinite(value)) {
            throw std::invalid_argument(label_ + " coordinates must be finite");
        }
        return value;
    }

    void Finish() {
        SkipWhitespace();
        if (!AtEnd()) {
            throw std::invalid_argument(label_ + " contained trailing JSON");
        }
    }

    std::string_view text_;
    std::string label_;
    std::size_t pos_ = 0u;
};

struct PrimitiveSpec {
    std::string objectType;
    double x1 = 0.0;
    double y1 = 0.0;
    double x2 = 100.0;
    double y2 = 100.0;
    double radius = 50.0;
    double startAngle = 0.0;
    double sweepAngle = 90.0;
    double height = 3000.0;
    double thickness = 200.0;
    double elevation = 0.0;
    double slope = 30.0;
    double overhang = 500.0;
    double bearingInset = 0.0;
    double verticalMiter = 0.0;
    int miterType = 1;
    bool generateGableWalls = false;
    double width = 0.0;
    double sillHeight = 0.0;
    double rotation = 0.0;
    double textSize = 0.0;
    double dimensionOffset = 300.0;
    double dimensionTextOffset = 0.0;
    double directionX = 0.0;
    double directionY = 0.0;
    int dimensionType = 1;
    bool fixedSizeText = false;
    bool wrapText = false;
    bool closed = true;
    bool hasExplicitX = false;
    bool hasExplicitY = false;
    bool hasExplicitWidth = false;
    bool hasExplicitHeight = false;
    bool hasExplicitSill = false;
    std::vector<WorldPt> points;
    std::string text;
    std::string styleName;
    std::string name;
    std::string className;
    std::string roomId;
    std::string symbolDefinitionName;
    std::string pluginName;
    std::string descriptorFingerprint;
    std::string wallUuid;
    bool requireWallHost = false;
    std::vector<ParametricValue> parametricValues;
};

struct CreatedPrimitive {
    int index = 0;
    std::string objectType;
    MCObjectHandle handle = nullptr;
    std::vector<std::string> warnings;
    std::size_t vertexCount = 0u;
    bool closed = false;
    short nativeNodeType = 0;
    bool semanticallyVerified = false;
};

MCObjectHandle EnsureWritableLayer() {
    MCObjectHandle layer = gSDK->GetActiveLayer();
    if (!layer) {
        layer = gSDK->GetCurrentLayer();
    }
    if (!layer) {
        const auto layers = CollectLayerHandles();
        if (!layers.empty()) {
            layer = layers.front();
        }
    }
    if (!layer) {
        throw std::runtime_error(
            "active Vectorworks document has no writable design layer; "
            "open a document with a design layer before running CAD operations");
    }
    // Re-selecting the already-active layer for every object can trigger a
    // costly Vectorworks layer/document refresh. Only switch when recovery
    // selected a different writable layer.
    if (gSDK->GetCurrentLayer() != layer) {
        gSDK->SetCurrentLayer(layer);
    }
    return layer;
}

std::string ParametricDescriptorJson(const ParametricDescriptor& descriptor) {
    std::string json = "{\"universal_plugin_name\":" + JsonString(descriptor.universalPluginName);
    json += ",\"localized_plugin_name\":" + JsonString(descriptor.localizedPluginName);
    json += ",\"descriptor_fingerprint\":" + JsonString(descriptor.descriptorFingerprint);
    json += ",\"parameters\":[";
    for (std::size_t index = 0; index < descriptor.parameters.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        const auto& parameter = descriptor.parameters[index];
        json += "{\"id\":" + JsonString(parameter.universalName);
        json += ",\"display_name\":" + JsonString(parameter.localizedName);
        json += ",\"field_style\":" + std::to_string(parameter.fieldStyle);
        json += ",\"value\":" + JsonString(parameter.value) + "}";
    }
    json += "]}";
    return json;
}

std::string HandleDescribeParametricSchema(const Params& params) {
    const std::string pluginName = TrimCopy(GetStringParam(params, "plugin_name"));
    if (pluginName.empty()) {
        throw std::invalid_argument("plugin_name is required for parametric schema discovery");
    }
    return ParametricDescriptorJson(DescribeParametricDefinition(pluginName));
}

std::string NativeIOLayerSnapshotJson(const NativeIO::LayerSnapshot& layer) {
    return "{\"uuid\":" + JsonString(layer.uuid) +
        ",\"name\":" + JsonString(layer.name) +
        ",\"native_node_type\":" + std::to_string(layer.actualNodeType) +
        ",\"visible\":" + (layer.visible ? "true" : "false") + "}";
}

std::string NativeIOLayerSnapshotsJson(const std::vector<NativeIO::LayerSnapshot>& layers) {
    std::string json = "[";
    for (std::size_t index = 0; index < layers.size(); ++index) {
        if (index != 0u) json += ",";
        json += NativeIOLayerSnapshotJson(layers[index]);
    }
    return json + "]";
}

std::string NativeIODocumentSnapshotJson(const NativeIO::DocumentMutationSnapshot& snapshot) {
    return "{\"document_path\":" + JsonString(snapshot.documentPath) +
        ",\"active_layer_uuid\":" + JsonString(snapshot.activeLayerUuid) +
        ",\"active_layer_name\":" + JsonString(snapshot.activeLayerName) +
        ",\"layer_count\":" + std::to_string(snapshot.layers.size()) +
        ",\"object_count\":" + std::to_string(snapshot.objectUuids.size()) + "}";
}

std::string NativeIOResultJson(const NativeIO::Result& result) {
    std::string json = "{\"operation\":" + JsonString(result.operation);
    json += ",\"path\":" + JsonString(result.canonicalPath);
    json += ",\"size_bytes\":" + std::to_string(result.sizeBytes);
    json += ",\"replaced_existing\":";
    json += result.replacedExisting ? "true" : "false";
    if (result.hasDocumentMutationReceipt) {
        const auto& mutation = result.documentMutationReceipt;
        json += ",\"document_mutation\":{\"verified\":";
        json += mutation.verified ? "true" : "false";
        json += ",\"before\":" + NativeIODocumentSnapshotJson(mutation.before);
        json += ",\"after\":" + NativeIODocumentSnapshotJson(mutation.after);
        json += ",\"created_object_uuids\":" + JsonStringArray(mutation.createdObjectUuids);
        json += ",\"deleted_object_uuids\":" + JsonStringArray(mutation.deletedObjectUuids);
        json += ",\"created_layers\":" + NativeIOLayerSnapshotsJson(mutation.createdLayers);
        json += ",\"deleted_layers\":" + NativeIOLayerSnapshotsJson(mutation.deletedLayers);
        json += ",\"changed_layers\":" + NativeIOLayerSnapshotsJson(mutation.changedLayers);
        json += ",\"active_layer_changed\":";
        json += mutation.activeLayerChanged ? "true" : "false";
        json += "}";
    }
    json += "}";
    return json;
}

NativeIO::OutputTarget ParseOutputTarget(const Params& params) {
    NativeIO::OutputTarget output;
    output.absolutePath = GetStringParam(params, "file_path", GetStringParam(params, "path"));
    const bool replace = GetBoolParam(params, "replace", false);
    output.overwritePolicy = replace
        ? NativeIO::OverwritePolicy::Replace
        : NativeIO::OverwritePolicy::FailIfExists;
    output.replaceConfirmation = GetStringParam(params, "replace_confirmation");
    return output;
}

std::string HandleNativeIO(const std::string& action, const Params& params) {
    if (action == "export_pdf") {
        NativeIO::PDFExportRequest request;
        request.output = ParseOutputTarget(params);
        request.currentViewOnly = GetBoolParam(params, "current_view_only", false);
        request.range = GetStringParam(params, "range");
        request.sheet = GetStringParam(params, "sheet");
        request.resolutionDpi = HasParam(params, "dpi")
            ? GetBoundedIntParam(params, "dpi", 300, 72, 2400)
            : GetBoundedIntParam(params, "resolution_dpi", 300, 72, 2400);
        request.updateViewports = GetBoolParam(params, "update_viewports", false);
        request.resetPlugInObjects = GetBoolParam(params, "reset_plugin_objects", false);
        request.recalculateWorksheets = GetBoolParam(params, "recalculate_worksheets", true);
        return NativeIOResultJson(NativeIO::ExportPDF(request));
    }
    if (action == "export_vectorworks_document") {
        NativeIO::VectorworksExportRequest request;
        request.output = ParseOutputTarget(params);
        request.targetFileVersion = static_cast<short>(GetBoundedIntParam(
            params, "target_file_version", 29, 1, std::numeric_limits<short>::max()));
        return NativeIOResultJson(NativeIO::ExportVectorworksDocument(request));
    }
    if (action == "import_dwg") {
        NativeIO::DWGImportRequest request;
        request.absolutePath = GetStringParam(params, "file_path", GetStringParam(params, "path"));
        return NativeIOResultJson(NativeIO::ImportDWG(request));
    }
    if (action == "export_dwg") {
        NativeIO::DWGExportRequest request;
        request.output = ParseOutputTarget(params);
        request.updateViewports = GetBoolParam(params, "update_viewports", false);
        request.resetPlugInObjects = GetBoolParam(params, "reset_plugin_objects", false);
        request.recalculateWorksheets = GetBoolParam(params, "recalculate_worksheets", true);
        return NativeIOResultJson(NativeIO::ExportDWG(request));
    }
    if (action == "export_ifc") {
        NativeIO::IFCRequest request;
        request.output = ParseOutputTarget(params);
        return NativeIOResultJson(NativeIO::ExportIFC(request));
    }
    if (action == "import_ifc") {
        NativeIO::IFCRequest request;
        request.absolutePath = GetStringParam(params, "file_path", GetStringParam(params, "path"));
        return NativeIOResultJson(NativeIO::ImportIFC(request));
    }
    throw std::invalid_argument("unsupported native I/O action: " + action);
}

struct PageSpec {
    std::size_t offset = 0;
    std::size_t limit = 5000;
};

PageSpec ParsePage(const Params& params) {
    PageSpec page;
    std::string cursor = HasParam(params, "cursor") ? GetStringParam(params, "cursor") : std::string();
    int offset = GetClampedIntParam(params, "offset", 0, 0, 100000000);
    if (!cursor.empty()) {
        try {
            offset = std::max(0, std::stoi(cursor));
        } catch (...) {
            throw std::invalid_argument("cursor must be a next_cursor value returned by a previous page");
        }
    }
    page.offset = static_cast<std::size_t>(offset);
    page.limit = static_cast<std::size_t>(GetClampedIntParam(params, "limit", 5000, 1, 20000));
    return page;
}

std::string PageTail(const PageSpec& page, std::size_t total, std::size_t returned) {
    const bool hasMore = page.offset + returned < total;
    return ",\"total\":" + std::to_string(total) + ",\"offset\":" + std::to_string(page.offset) +
        ",\"has_more\":" + (hasMore ? "true" : "false") +
        ",\"next_cursor\":" + (hasMore ? JsonString(std::to_string(page.offset + returned)) : std::string("null"));
}

std::string ResourceRecordsJson(
    const std::vector<ResourceWorksheets::ResourceRecord>& records,
    const std::string& query,
    const PageSpec& page = PageSpec{},
    std::size_t* totalOut = nullptr,
    std::size_t* returnedOut = nullptr) {
    const std::string lowerQuery = ToLower(query);
    std::string json = "[";
    std::size_t matched = 0;
    std::size_t returned = 0;
    for (const auto& record : records) {
        if (!lowerQuery.empty() && ToLower(record.name).find(lowerQuery) == std::string::npos) {
            continue;
        }
        const std::size_t position = matched++;
        if (position < page.offset || returned >= page.limit) continue;
        if (returned) json += ",";
        ++returned;
        json += "{\"name\":" + JsonString(record.name) +
            ",\"native_node_type\":" + std::to_string(record.actualNodeType) + "}";
    }
    json += "]";
    if (totalOut) *totalOut = matched;
    if (returnedOut) *returnedOut = returned;
    return json;
}

std::string WorksheetListJson(const Params& params, bool includeNodeType, std::size_t* totalOut, std::size_t* returnedOut) {
    const PageSpec page = ParsePage(params);
    const std::string query = ToLower(GetStringParam(params, "query"));
    const auto worksheets = ResourceWorksheets::ListWorksheets(*gSDK);
    std::string json = "[";
    std::size_t matched = 0;
    std::size_t returned = 0;
    for (const auto& worksheet : worksheets) {
        if (!query.empty() && ToLower(worksheet.name).find(query) == std::string::npos) continue;
        const std::size_t position = matched++;
        if (position < page.offset || returned >= page.limit) continue;
        if (returned) json += ",";
        ++returned;
        json += "{\"name\":" + JsonString(worksheet.name) +
            ",\"rows\":" + std::to_string(worksheet.rowCount) +
            ",\"columns\":" + std::to_string(worksheet.columnCount);
        if (includeNodeType) json += ",\"native_node_type\":" + std::to_string(worksheet.actualNodeType);
        json += "}";
    }
    json += "]";
    if (totalOut) *totalOut = matched;
    if (returnedOut) *returnedOut = returned;
    return json;
}

std::string HandleResources(const Params& params) {
    const PageSpec page = ParsePage(params);
    std::size_t symbolTotal = 0, symbolReturned = 0, worksheetTotal = 0, worksheetReturned = 0;
    std::string json = "{\"symbols\":" + ResourceRecordsJson(
        ResourceWorksheets::ListSymbolDefinitions(*gSDK), GetStringParam(params, "query"), page,
        &symbolTotal, &symbolReturned);
    json += ",\"worksheets\":" + WorksheetListJson(params, true, &worksheetTotal, &worksheetReturned);
    const std::size_t total = std::max(symbolTotal, worksheetTotal);
    const std::size_t returned = std::max(symbolReturned, worksheetReturned);
    json += ",\"symbols_total\":" + std::to_string(symbolTotal);
    json += ",\"worksheets_total\":" + std::to_string(worksheetTotal);
    json += PageTail(page, total, returned);
    json += "}";
    return json;
}

std::string HandleSymbol(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    if (action == "list") {
        const PageSpec page = ParsePage(params);
        std::size_t total = 0, returned = 0;
        std::string json = "{\"symbols\":" + ResourceRecordsJson(
            ResourceWorksheets::ListSymbolDefinitions(*gSDK), GetStringParam(params, "query"), page, &total, &returned);
        return json + PageTail(page, total, returned) + "}";
    }
    if (action == "insert") {
        ResourceWorksheets::SymbolInsertionRequest request;
        request.definitionName = GetStringParam(params, "definition_name");
        request.x = GetFiniteNumberParam(params, "x", 0.0);
        request.y = GetFiniteNumberParam(params, "y", 0.0);
        request.rotationDegrees = GetFiniteNumberParam(params, "rotation_deg", 0.0);
        Transactions::TransactionOptions options;
        options.expectedArtifactCount = 1u;
        options.sdkManagedRegistrationFamilies = {Transactions::ObjectFamily::Symbol};
        Transactions::NativeTransaction transaction(
            *gSDK,
            TXString("Vectorworks MCP insert symbol"),
            std::move(options));
        try {
            const auto receipt = ResourceWorksheets::InsertSymbol(*gSDK, request);
            UnregisteredCreatedObjectGuard guard(receipt.handle);
            const auto artifact = transaction.AdoptFinal(
                receipt.handle,
                Transactions::ObjectFamily::Symbol,
                kSymbolNode,
                [definitionName = receipt.definitionName](MCObjectHandle verifiedSymbol) {
                    if (!verifiedSymbol || gSDK->GetObjectTypeN(verifiedSymbol) != kSymbolNode) {
                        throw std::runtime_error("inserted symbol changed native node type before commit");
                    }
                    MCObjectHandle definition = gSDK->GetDefinition(verifiedSymbol);
                    TXString actualName;
                    if (definition) {
                        gSDK->GetObjectName(definition, actualName);
                    }
                    if (!definition || gSDK->GetObjectTypeN(definition) != kSymDefNode ||
                        TxToUtf8(actualName) != definitionName) {
                        throw std::runtime_error("inserted symbol lost its exact definition before commit");
                    }
                });
            (void) artifact;
            guard.Release();
            transaction.Commit();
            return "{\"inserted\":true,\"definition_name\":" + JsonString(receipt.definitionName) +
                ",\"native_node_type\":" + std::to_string(receipt.actualNodeType) +
                ",\"object\":" + ObjectJson(receipt.handle) + "}";
        } catch (...) {
            transaction.RollbackAndRethrow(std::current_exception());
        }
    }
    throw std::invalid_argument("symbol.action must be list or insert");
}

std::string WorksheetCellJson(const ResourceWorksheets::WorksheetCellSnapshot& cell) {
    return "{\"worksheet\":" + JsonString(cell.worksheetName) +
        ",\"row\":" + std::to_string(cell.row) +
        ",\"column\":" + std::to_string(cell.column) +
        ",\"formula\":" + JsonString(cell.formula) +
        ",\"displayed_value\":" + JsonString(cell.displayedValue) + "}";
}

std::string HandleWorksheet(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    if (action == "list") {
        const PageSpec page = ParsePage(params);
        std::size_t total = 0, returned = 0;
        std::string json = "{\"worksheets\":" + WorksheetListJson(params, false, &total, &returned);
        return json + PageTail(page, total, returned) + "}";
    }
    const std::string worksheetName = GetStringParam(params, "worksheet_name");
    const int row = GetRequiredBoundedIntParam(params, "row", 1, 32767);
    const int column = GetRequiredBoundedIntParam(params, "column", 1, 32767);
    if (action == "read") {
        return WorksheetCellJson(ResourceWorksheets::ReadWorksheetCell(
            *gSDK, worksheetName, row, column));
    }
    if (action == "write") {
        ResourceWorksheets::WorksheetCellWriteRequest request{
            worksheetName, row, column, GetStringParam(params, "formula")};
        MCObjectHandle worksheetHandle = nullptr;
        for (const auto& item : ResourceWorksheets::ListWorksheets(*gSDK)) {
            if (item.name == worksheetName) {
                if (worksheetHandle) throw std::invalid_argument("worksheet name is ambiguous");
                worksheetHandle = item.handle;
            }
        }
        if (!worksheetHandle) throw std::invalid_argument("worksheet was not found by exact name");
        gSDK->SupportUndoAndRemove();
        gSDK->SetUndoMethod(kUndoSwapObjects);
        gSDK->NameUndoEvent(TXString("Vectorworks MCP worksheet write"));
        try {
            if (!gSDK->AddBeforeSwapObject(worksheetHandle)) {
                throw std::runtime_error("Vectorworks failed to register the worksheet before mutation");
            }
            const auto receipt = ResourceWorksheets::WriteWorksheetCell(*gSDK, request);
            if (!gSDK->AddAfterSwapObject(receipt.after.worksheetHandle)) {
                throw std::runtime_error("Vectorworks failed to register the worksheet after mutation");
            }
            EndUndoEventOrThrow("worksheet write");
            return "{\"verified\":true,\"before\":" + WorksheetCellJson(receipt.before) +
                ",\"after\":" + WorksheetCellJson(receipt.after) + "}";
        } catch (...) {
            gSDK->UndoAndRemove();
            throw;
        }
    }
    throw std::invalid_argument("worksheet.action must be list, read, or write");
}

std::string CanonicalCreateObjectType(std::string objectType) {
    objectType = ToLower(objectType);
    if (objectType == "rectangle" || objectType == "box") {
        return "rect";
    }
    if (objectType == "dimension" || objectType == "linear_dimension") {
        return "linear_dimension";
    }
    if (objectType == "polyline") {
        return "polygon";
    }
    return objectType;
}

double GetFiniteNumberParamAlias(
    const Params& params,
    const std::string& preferredKey,
    const std::string& aliasKey,
    double defaultValue) {
    if (HasParam(params, preferredKey)) {
        return GetFiniteNumberParam(params, preferredKey, defaultValue);
    }
    return GetFiniteNumberParam(params, aliasKey, defaultValue);
}

double RequirePositiveNumber(double value, const std::string& label) {
    if (value <= 0.0) {
        throw std::invalid_argument(label + " must be > 0");
    }
    return value;
}

void ValidatePrimitiveSpec(const PrimitiveSpec& spec, const std::string& label) {
    if (spec.objectType.empty()) {
        throw std::invalid_argument(label + ".object_type is required");
    }
    if (spec.objectType == "rect" || spec.objectType == "oval") {
        if (spec.x1 == spec.x2 || spec.y1 == spec.y2) {
            throw std::invalid_argument(label + " " + spec.objectType + " bounds must have non-zero width and height");
        }
        return;
    }
    if (spec.objectType == "line") {
        if (spec.x1 == spec.x2 && spec.y1 == spec.y2) {
            throw std::invalid_argument(label + " line endpoints must not be identical");
        }
        return;
    }
    if (spec.objectType == "circle") {
        if (spec.radius <= 0.0) {
            throw std::invalid_argument(label + ".radius must be > 0");
        }
        return;
    }
    if (spec.objectType == "arc") {
        if (spec.radius <= 0.0) {
            throw std::invalid_argument(label + ".radius must be > 0");
        }
        if (spec.sweepAngle == 0.0) {
            throw std::invalid_argument(label + ".sweep_angle must not be 0");
        }
        return;
    }
    if (spec.objectType == "polygon" || spec.objectType == "slab" ||
        spec.objectType == "roof" || spec.objectType == "space") {
        const std::size_t minimum = spec.closed ? 3u : 2u;
        if (spec.points.size() < minimum) {
            throw std::invalid_argument(
                label + " " + (spec.closed ? "closed polygon" : "open polyline") +
                " requires at least " + std::to_string(minimum) + " points");
        }
        for (std::size_t index = 1; index < spec.points.size(); ++index) {
            if (spec.points[index - 1].x == spec.points[index].x &&
                spec.points[index - 1].y == spec.points[index].y) {
                throw std::invalid_argument(label + ".points contains consecutive duplicate points");
            }
        }
        if (spec.objectType == "slab") {
            RequirePositiveNumber(spec.thickness, label + ".thickness");
        }
        if (spec.objectType == "space") {
            RequirePositiveNumber(spec.height, label + ".height");
        }
        if (spec.objectType == "roof") {
            RequirePositiveNumber(spec.thickness, label + ".thickness");
            if (spec.slope <= 0.0 || spec.slope > 89.0) {
                throw std::invalid_argument(label + ".slope must be > 0 and <= 89 degrees");
            }
            if (spec.overhang < 0.0 || spec.bearingInset < 0.0 || spec.verticalMiter < 0.0) {
                throw std::invalid_argument(label + " roof offsets must be >= 0");
            }
            if (spec.miterType < 1 || spec.miterType > 4) {
                throw std::invalid_argument(label + ".miter_type must be between 1 and 4");
            }
        }
        return;
    }
    if (spec.objectType == "wall") {
        if (spec.x1 == spec.x2 && spec.y1 == spec.y2) {
            throw std::invalid_argument(label + " wall endpoints must not be identical");
        }
        RequirePositiveNumber(spec.thickness, label + ".thickness");
        RequirePositiveNumber(spec.height, label + ".height");
        return;
    }
    if (spec.objectType == "parametric") {
        if (spec.pluginName.empty()) {
            throw std::invalid_argument(label + ".plugin_name is required");
        }
        if (spec.requireWallHost && spec.wallUuid.empty()) {
            throw std::invalid_argument(label + ".wall_uuid is required for hosted placement");
        }
        return;
    }
    if (spec.objectType == "door" || spec.objectType == "window") {
        if (!spec.hasExplicitX || !spec.hasExplicitY) {
            throw std::invalid_argument(label + ".x1 and y1 are required for hosted placement");
        }
        if (!spec.hasExplicitWidth || !spec.hasExplicitHeight) {
            throw std::invalid_argument(label + ".width and height are required");
        }
        RequirePositiveNumber(spec.width, label + ".width");
        RequirePositiveNumber(spec.height, label + ".height");
        if (spec.wallUuid.empty()) {
            throw std::invalid_argument(label + ".wall_uuid is required for hosted placement");
        }
        if (!spec.requireWallHost) {
            throw std::invalid_argument(label + ".require_wall_host must be true");
        }
        const std::string expectedPlugin = spec.objectType == "door" ? "Door" : "Window";
        if (!spec.pluginName.empty() && spec.pluginName != expectedPlugin) {
            throw std::invalid_argument(
                label + ".plugin_name must be the exact universal name " + expectedPlugin);
        }
        if (!spec.parametricValues.empty()) {
            throw std::invalid_argument(
                label + " dedicated opening dimensions cannot be overridden by generic parameters");
        }
        if (spec.descriptorFingerprint.empty()) {
            throw std::invalid_argument(
                label + ".descriptor_fingerprint is required from parametric schema discovery");
        }
        if (spec.objectType == "window" && spec.sillHeight < 0.0) {
            throw std::invalid_argument(label + ".sill_height must be >= 0");
        }
        if (spec.objectType == "window" && !spec.hasExplicitSill) {
            throw std::invalid_argument(label + ".sill_height is required");
        }
        if (spec.objectType == "door" && spec.hasExplicitSill) {
            throw std::invalid_argument(label + ".sill_height is supported only for window");
        }
        return;
    }
    if (spec.objectType == "symbol") {
        if (spec.symbolDefinitionName.empty()) {
            throw std::invalid_argument(label + ".definition_name is required");
        }
        return;
    }
    if (spec.objectType == "text") {
        if (spec.text.empty()) {
            throw std::invalid_argument(label + ".text is required");
        }
        if (spec.width < 0.0) {
            throw std::invalid_argument(label + ".width must be >= 0");
        }
        if (spec.textSize < 0.0) {
            throw std::invalid_argument(label + ".text_size must be >= 0");
        }
        return;
    }
    if (spec.objectType == "linear_dimension") {
        if (!spec.name.empty()) {
            throw std::invalid_argument(
                label + ".name is not supported for native linear dimensions");
        }
        if (spec.x1 == spec.x2 && spec.y1 == spec.y2) {
            throw std::invalid_argument(label + " linear_dimension endpoints must not be identical");
        }
        if (spec.dimensionType < 0 || spec.dimensionType > 2) {
            throw std::invalid_argument(label + ".dimension_type must be 0, 1, or 2");
        }
        return;
    }
    throw std::invalid_argument("unsupported create object type for native bridge: " + spec.objectType);
}

PrimitiveSpec ParsePrimitiveSpec(const Params& params, const std::string& label) {
    PrimitiveSpec spec;
    spec.hasExplicitX = HasParam(params, "x1") || HasParam(params, "start_x");
    spec.hasExplicitY = HasParam(params, "y1") || HasParam(params, "start_y");
    spec.hasExplicitWidth = HasParam(params, "width");
    spec.hasExplicitHeight = HasParam(params, "height");
    spec.hasExplicitSill = HasParam(params, "sill_height") ||
        HasParam(params, "window_sill_height");
    spec.objectType = GetStringParam(params, "object_type");
    if (spec.objectType.empty()) {
        spec.objectType = GetStringParam(params, "type");
    }
    if (spec.objectType.empty()) {
        if (label == "create_wall") {
            spec.objectType = "wall";
        } else if (label == "create_text") {
            spec.objectType = "text";
        } else if (label == "create_linear_dimension") {
            spec.objectType = "linear_dimension";
        }
    }
    spec.objectType = CanonicalCreateObjectType(spec.objectType);
    spec.x1 = GetFiniteNumberParamAlias(params, "x1", "start_x", 0.0);
    spec.y1 = GetFiniteNumberParamAlias(params, "y1", "start_y", 0.0);
    spec.x2 = GetFiniteNumberParamAlias(params, "x2", "end_x", 100.0);
    spec.y2 = GetFiniteNumberParamAlias(params, "y2", "end_y", 100.0);
    spec.radius = GetFiniteNumberParam(params, "radius", 50.0);
    spec.startAngle = GetFiniteNumberParam(params, "start_angle", 0.0);
    spec.sweepAngle = GetFiniteNumberParam(params, "sweep_angle", 90.0);
    spec.height = GetFiniteNumberParam(params, "height", 3000.0);
    spec.thickness = GetFiniteNumberParam(params, "thickness", 200.0);
    spec.elevation = GetFiniteNumberParam(params, "elevation", GetFiniteNumberParam(params, "bearing_height", 0.0));
    spec.slope = GetFiniteNumberParam(params, "slope", 30.0);
    spec.overhang = GetFiniteNumberParam(params, "overhang", 500.0);
    spec.bearingInset = GetFiniteNumberParam(params, "bearing_inset", 0.0);
    spec.verticalMiter = GetFiniteNumberParam(params, "vertical_miter", 0.0);
    spec.miterType = GetBoundedIntParam(params, "miter_type", 1, 1, 4);
    spec.generateGableWalls = GetBoolParam(params, "generate_gable_walls", false);
    spec.pluginName = GetStringParam(params, "plugin_name");
    spec.descriptorFingerprint = GetStringParam(params, "descriptor_fingerprint");
    spec.wallUuid = GetStringParam(params, "wall_uuid");
    spec.requireWallHost = GetBoolParam(params, "require_wall_host", false);
    const int parameterCount = GetBoundedIntParam(params, "parameter_count", 0, 0, 256);
    spec.parametricValues.reserve(static_cast<std::size_t>(parameterCount));
    for (int parameterIndex = 1; parameterIndex <= parameterCount; ++parameterIndex) {
        const std::string prefix = "parameter_" + std::to_string(parameterIndex) + "_";
        ParametricValue value;
        value.universalName = GetStringParam(params, prefix + "name");
        const std::string kind = ToLower(GetStringParam(params, prefix + "type"));
        if (kind == "integer") {
            value.kind = ParametricValueKind::Integer;
            value.integerValue = static_cast<std::int32_t>(GetBoundedIntParam(
                params, prefix + "integer", 0, std::numeric_limits<int>::min(), std::numeric_limits<int>::max()));
        } else if (kind == "boolean") {
            value.kind = ParametricValueKind::Boolean;
            value.booleanValue = GetBoolParam(params, prefix + "boolean", false);
        } else if (kind == "real") {
            value.kind = ParametricValueKind::Real;
            value.realValue = GetFiniteNumberParam(params, prefix + "real", 0.0);
        } else if (kind == "string") {
            value.kind = ParametricValueKind::String;
            value.stringValue = GetStringParam(params, prefix + "string");
        } else {
            throw std::invalid_argument(prefix + "type must be integer, boolean, real, or string");
        }
        spec.parametricValues.push_back(std::move(value));
    }
    spec.width = GetFiniteNumberParam(params, "width", 0.0);
    spec.sillHeight = GetFiniteNumberParam(
        params,
        "sill_height",
        GetFiniteNumberParam(params, "window_sill_height", 0.0));
    spec.rotation = GetFiniteNumberParam(params, "rotation", 0.0);
    spec.textSize = GetFiniteNumberParam(params, "text_size", GetFiniteNumberParam(params, "size", 0.0));
    spec.dimensionOffset = GetFiniteNumberParam(params, "offset", GetFiniteNumberParam(params, "dimension_offset", 300.0));
    spec.dimensionTextOffset = GetFiniteNumberParam(params, "text_offset", 0.0);
    spec.directionX = GetFiniteNumberParam(params, "direction_x", 0.0);
    spec.directionY = GetFiniteNumberParam(params, "direction_y", 0.0);
    spec.dimensionType = GetBoundedIntParam(params, "dimension_type", 1, 0, 2);
    spec.fixedSizeText = GetBoolParam(params, "fixed_size", false);
    spec.wrapText = GetBoolParam(params, "wrap", false);
    spec.closed = GetBoolParam(params, "closed", true);
    if (spec.objectType == "polygon" || spec.objectType == "slab" ||
        spec.objectType == "roof" || spec.objectType == "space") {
        std::string pointsJson;
        const auto points = params.find("points");
        if (points != params.end() && points->second.type != ParamValue::Type::Null) {
            pointsJson = GetRawJsonParam(params, "points");
        } else {
            pointsJson = GetStringParam(params, "points_json");
        }
        if (pointsJson.empty()) {
            throw std::invalid_argument(label + ".points is required");
        }
        spec.points = PointListJsonParser(pointsJson, label + ".points").Parse();
        if (spec.closed && spec.points.size() > 1u &&
            spec.points.front().x == spec.points.back().x &&
            spec.points.front().y == spec.points.back().y) {
            spec.points.pop_back();
        }
    }
    spec.text = GetStringParam(params, "text");
    spec.styleName = GetStringParam(params, "style_name");
    spec.name = GetStringParam(params, "name");
    spec.className = GetStringParam(params, "class_name");
    spec.roomId = GetStringParam(params, "room_id");
    spec.symbolDefinitionName = GetStringParam(params, "definition_name");
    ValidatePrimitiveSpec(spec, label);
    return spec;
}

void ApplyObjectNameAndClass(MCObjectHandle object, const PrimitiveSpec& spec) {
    if (!object) {
        return;
    }
    if (!spec.name.empty()) {
        gSDK->SetObjectName(object, TXString(spec.name.c_str()));
    }
    if (!spec.className.empty()) {
        InternalIndex classId = gSDK->ClassNameToID(TXString(spec.className.c_str()));
        if (!gSDK->ValidClass(classId)) {
            classId = gSDK->AddClass(TXString(spec.className.c_str()));
        }
        if (gSDK->ValidClass(classId)) {
            gSDK->SetObjectClass(object, classId);
        }
    }
}

InternalIndex ResolveOrCreateClass(const std::string& className) {
    InternalIndex classId = gSDK->ClassNameToID(TXString(className.c_str()));
    if (!gSDK->ValidClass(classId)) {
        classId = gSDK->AddClass(TXString(className.c_str()));
    }
    if (!gSDK->ValidClass(classId)) {
        throw std::runtime_error("Vectorworks rejected class: " + className);
    }
    return classId;
}

MCObjectHandle CreateLinearDimensionWithClass(
    const PrimitiveSpec& spec,
    InternalIndex* expectedClassId) {
    InternalIndex requestedClassId = 0;
    const Sint32 previousClassId = static_cast<Sint32>(gSDK->GetDimensionClassID());
    bool changedPreference = false;

    if (!spec.className.empty()) {
        requestedClassId = ResolveOrCreateClass(spec.className);
        const Sint32 requestedClassRef = static_cast<Sint32>(requestedClassId);
        if (requestedClassRef != previousClassId) {
            if (!gSDK->SetProgramVariable(varDefaultDimensionClassID, &requestedClassRef)) {
                throw std::runtime_error(
                    "Vectorworks rejected the requested dimension class preference");
            }
            changedPreference = true;
        }
    }

    MCObjectHandle object = nullptr;
    std::exception_ptr creationFailure;
    try {
        object = gSDK->CreateLinearDimension(
            WorldPt(spec.x1, spec.y1),
            WorldPt(spec.x2, spec.y2),
            spec.dimensionOffset,
            spec.dimensionTextOffset,
            Vector2(spec.directionX, spec.directionY),
            static_cast<short>(spec.dimensionType));
    } catch (...) {
        creationFailure = std::current_exception();
    }

    if (changedPreference &&
        !gSDK->SetProgramVariable(varDefaultDimensionClassID, &previousClassId)) {
        if (object) {
            gSDK->DeleteObject(object, false);
        }
        throw std::runtime_error(
            "Vectorworks did not restore the previous dimension class preference");
    }
    if (creationFailure) {
        std::rethrow_exception(creationFailure);
    }
    if (object && requestedClassId != 0 &&
        gSDK->GetObjectClass(object) != requestedClassId) {
        gSDK->DeleteObject(object, false);
        throw std::runtime_error(
            "Vectorworks did not create the dimension in the requested class");
    }
    if (expectedClassId) {
        *expectedClassId = requestedClassId;
    }
    return object;
}

void ApplyWallStyleIfRequested(MCObjectHandle wall, const PrimitiveSpec& spec, std::vector<std::string>* warnings) {
    if (!wall || spec.styleName.empty()) {
        return;
    }
    InternalIndex styleIndex = 0;
    if (!gSDK->NameToInternalIndexN(TXString(spec.styleName.c_str()), styleIndex) || styleIndex == 0) {
        if (warnings) {
            warnings->push_back("wall style not found: " + spec.styleName);
        }
        return;
    }
    MCObjectHandle styleHandle = gSDK->InternalIndexToHandle(styleIndex);
    if (!styleHandle || gSDK->GetObjectTypeN(styleHandle) != kWallStyleNode) {
        if (warnings) {
            warnings->push_back("resource is not a wall style: " + spec.styleName);
        }
        return;
    }
    if (!gSDK->SetWallStyle(wall, styleIndex, 0, 0) && warnings) {
        warnings->push_back("Vectorworks rejected wall style: " + spec.styleName);
    }
}

using LocalObjectMap = std::unordered_map<std::string, MCObjectHandle>;

// Resolves the host wall of a door/window/hosted parametric: an exact UUID
// (optionally "uuid:"-prefixed) or, inside apply_operations, "$<local_ref>" of
// a wall created earlier in the same transaction (one undo step).
MCObjectHandle ResolveHostWall(const std::string& reference, const LocalObjectMap* localObjects, bool& isLocal) {
    isLocal = false;
    std::string value = TrimCopy(reference);
    MCObjectHandle wall = nullptr;
    if (!value.empty() && value.front() == '$') {
        if (!localObjects) {
            throw std::invalid_argument("wall_uuid $local_ref is only supported inside apply_operations");
        }
        const auto found = localObjects->find(value.substr(1));
        if (found == localObjects->end() || !found->second) {
            throw std::invalid_argument("wall_uuid references an unknown local_ref: " + value);
        }
        wall = found->second;
        isLocal = true;
    } else {
        if (value.rfind("uuid:", 0u) == 0u) value = value.substr(5);
        wall = gSDK->GetObjectByUuid(TXString(value.c_str()));
    }
    if (!wall || gSDK->GetObjectTypeN(wall) != kWallNode) {
        throw std::invalid_argument("host wall was not found or is not a wall: " + reference);
    }
    return wall;
}

MCObjectHandle CreatePrimitiveFromSpec(
    const PrimitiveSpec& spec,
    Transactions::NativeTransaction& transaction,
    Transactions::ArtifactId* transactionArtifact,
    std::vector<std::string>* warnings = nullptr,
    const LocalObjectMap* localObjects = nullptr) {
    EnsureWritableLayer();

    MCObjectHandle object = nullptr;
    InternalIndex expectedDimensionClassId = 0;
    std::optional<Transactions::ArtifactId> adoptedArtifact;
    std::optional<Transactions::ExternalMutationId> pendingExternalAfter;
    MCObjectHandle pendingExternalHandle = nullptr;
    if (spec.objectType == "rect") {
        object = gSDK->CreateRectangle(WorldRect(WorldPt(spec.x1, spec.y1), WorldPt(spec.x2, spec.y2)));
    } else if (spec.objectType == "oval") {
        object = gSDK->CreateOval(WorldRect(WorldPt(spec.x1, spec.y1), WorldPt(spec.x2, spec.y2)));
    } else if (spec.objectType == "circle") {
        object = gSDK->CreateOval(WorldRect(WorldPt(spec.x1, spec.y1), spec.radius));
    } else if (spec.objectType == "line") {
        object = gSDK->CreateLine(WorldPt(spec.x1, spec.y1), WorldPt(spec.x2, spec.y2));
    } else if (spec.objectType == "arc") {
        object = gSDK->CreateArcN(WorldRect(WorldPt(spec.x1, spec.y1), spec.radius), spec.startAngle, spec.sweepAngle);
    } else if (spec.objectType == "polygon") {
        object = gSDK->CreatePolyshape();
        if (object) {
            for (const auto& point : spec.points) {
                gSDK->AddVertex(object, point, nullptr, vtCorner, 0, false);
            }
            gSDK->SetPolyShapeClose(object, spec.closed);
            gSDK->ResetObject(object);
        }
    } else if (spec.objectType == "slab") {
        BimObjects::SlabRequest request;
        request.elevation = spec.elevation;
        request.thickness = spec.thickness;
        request.styleName = spec.styleName;
        request.boundary.reserve(spec.points.size());
        for (const auto& point : spec.points) {
            request.boundary.push_back({point.x, point.y});
        }
        const auto receipt = BimObjects::CreateTrueSlab(*gSDK, request, transaction);
        object = receipt.handle;
        adoptedArtifact = receipt.transactionArtifact;
    } else if (spec.objectType == "roof") {
        BimObjects::RoofRequest request;
        request.slopeDegrees = spec.slope;
        request.projection = spec.overhang;
        request.eaveHeight = spec.elevation;
        request.bearingInset = spec.bearingInset;
        request.thickness = spec.thickness;
        request.miterType = static_cast<short>(spec.miterType);
        request.verticalMiter = spec.verticalMiter;
        request.generateGableWalls = spec.generateGableWalls;
        request.boundary.reserve(spec.points.size());
        for (const auto& point : spec.points) {
            request.boundary.push_back({point.x, point.y});
        }
        const auto receipt = BimObjects::CreateTrueRoof(*gSDK, request, transaction);
        object = receipt.handle;
        adoptedArtifact = receipt.transactionArtifact;
    } else if (spec.objectType == "space") {
        SpaceCreateSpec request;
        request.height = spec.height;
        request.name = spec.name;
        request.roomId = spec.roomId;
        request.boundary.reserve(spec.points.size());
        for (const auto& point : spec.points) {
            request.boundary.push_back({point.x, point.y});
        }
        const auto receipt = CreateVerifiedSpace(request, transaction);
        object = receipt.object;
        adoptedArtifact = receipt.transactionArtifact;
    } else if (spec.objectType == "door" || spec.objectType == "window") {
        bool wallIsLocal = false;
        MCObjectHandle wall = ResolveHostWall(spec.wallUuid, localObjects, wallIsLocal);
        // A wall created earlier in this transaction is already an owned
        // artifact; its final state is registered at commit.
        std::optional<Transactions::ExternalMutationId> wallMutation;
        if (!wallIsLocal) {
            wallMutation = transaction.TrackExternalBefore(wall, Transactions::ObjectFamily::Wall);
        }
        BuiltInOpeningCreateSpec request;
        request.kind = spec.objectType == "door"
            ? BuiltInParametricKind::Door
            : BuiltInParametricKind::Window;
        request.expectedWall = wall;
        request.x = spec.x1;
        request.y = spec.y1;
        request.rotationDegrees = spec.rotation;
        request.width = spec.width;
        request.height = spec.height;
        request.windowSillHeight = spec.sillHeight;
        request.descriptorFingerprint = spec.descriptorFingerprint;
        const auto receipt = CreateVerifiedBuiltInOpening(request);
        object = receipt.object;
        UnregisteredCreatedObjectGuard openingGuard(object);
        adoptedArtifact = transaction.AdoptFinal(
            object,
            spec.objectType == "door"
                ? Transactions::ObjectFamily::Door
                : Transactions::ObjectFamily::Window,
            kParametricNode,
            [receipt](MCObjectHandle verifiedOpening) {
                VerifyBuiltInOpeningReceipt(verifiedOpening, receipt);
            });
        openingGuard.Release();
        if (wallMutation) {
            transaction.TrackExternalAfter(*wallMutation, wall);
        }
    } else if (spec.objectType == "parametric") {
        MCObjectHandle wall = nullptr;
        std::optional<Transactions::ExternalMutationId> wallMutation;
        if (spec.requireWallHost) {
            bool wallIsLocal = false;
            wall = ResolveHostWall(spec.wallUuid, localObjects, wallIsLocal);
            if (!wallIsLocal) {
                wallMutation = transaction.TrackExternalBefore(
                    wall, Transactions::ObjectFamily::Wall);
            }
        }
        ParametricCreateSpec request;
        request.universalPluginName = spec.pluginName;
        request.x = spec.x1;
        request.y = spec.y1;
        request.rotationDegrees = spec.rotation;
        request.requireWallHost = spec.requireWallHost;
        request.expectedWall = wall;
        request.descriptorFingerprint = spec.descriptorFingerprint;
        request.parameters = spec.parametricValues;
        object = CreateVerifiedParametricObject(request);
        if (wallMutation) {
            pendingExternalAfter = *wallMutation;
        }
        // The commit verifier checks the exact host wall (local or external).
        pendingExternalHandle = wall;
    } else if (spec.objectType == "symbol") {
        object = PlaceVerifiedSymbol({
            spec.symbolDefinitionName,
            spec.x1,
            spec.y1,
            spec.rotation,
        });
    } else if (spec.objectType == "wall") {
        object = gSDK->CreateWall(WorldPt(spec.x1, spec.y1), WorldPt(spec.x2, spec.y2), spec.thickness);
        if (object) {
            gSDK->SetWallWidth(object, spec.thickness);
            gSDK->SetWallCornerHeights(object, spec.height, 0, spec.height, 0);
            ApplyWallStyleIfRequested(object, spec, warnings);
        }
    } else if (spec.objectType == "text") {
        object = gSDK->CreateTextBlock(
            TXString(spec.text.c_str()),
            WorldPt(spec.x1, spec.y1),
            spec.fixedSizeText,
            spec.width);
        if (object) {
            if (spec.width > 0.0) {
                gSDK->SetTextWidth(object, spec.width);
            }
            if (spec.wrapText || spec.width > 0.0) {
                gSDK->SetTextWrap(object, true);
            }
            if (spec.rotation != 0.0) {
                gSDK->SetTextOrientationN(object, spec.rotation, 0);
            }
            if (spec.textSize > 0.0) {
                double_gs points = spec.textSize;
                const WorldCoord charSize = gSDK->PagePointsToCoordLength(points);
                gSDK->SetTextSize(object, 0, static_cast<Sint32>(spec.text.size()), charSize);
            }
        }
    } else if (spec.objectType == "linear_dimension") {
        object = CreateLinearDimensionWithClass(spec, &expectedDimensionClassId);
    }

    if (!object) {
        throw std::runtime_error("Vectorworks did not return a handle for created " + spec.objectType);
    }

    UnregisteredCreatedObjectGuard unregisteredGuard(adoptedArtifact ? nullptr : object);
    if (spec.objectType != "linear_dimension") {
        ApplyObjectNameAndClass(object, spec);
    }
    if (!adoptedArtifact) {
        const short expectedNodeType = gSDK->GetObjectTypeN(object);
        Transactions::SemanticVerifier verifier;
        if (spec.objectType == "parametric") {
            const std::string expectedPlugin = spec.pluginName;
            const std::string expectedFingerprint = spec.descriptorFingerprint;
            const bool requiresWall = spec.requireWallHost;
            MCObjectHandle expectedWall = pendingExternalHandle;
            verifier = [expectedPlugin, expectedFingerprint, requiresWall, expectedWall](
                           MCObjectHandle verifiedObject) {
                const auto descriptor = DescribeParametricObject(verifiedObject);
                if (descriptor.universalPluginName != expectedPlugin ||
                    descriptor.descriptorFingerprint != expectedFingerprint) {
                    throw std::runtime_error(
                        "parametric object failed plugin/schema verification at commit");
                }
                if (requiresWall && !IsObjectHostedByWall(verifiedObject, expectedWall)) {
                    throw std::runtime_error(
                        "parametric object failed exact wall-host verification at commit");
                }
            };
        } else if (spec.objectType == "linear_dimension" && expectedDimensionClassId != 0) {
            const InternalIndex expectedClassId = expectedDimensionClassId;
            verifier = [expectedNodeType, expectedClassId](MCObjectHandle verifiedObject) {
                if (!verifiedObject || gSDK->GetObjectTypeN(verifiedObject) != expectedNodeType ||
                    !IsUserVisibleObjectType(expectedNodeType)) {
                    throw std::runtime_error(
                        "created dimension failed semantic node-type verification");
                }
                if (gSDK->GetObjectClass(verifiedObject) != expectedClassId) {
                    throw std::runtime_error(
                        "created dimension failed class verification at commit");
                }
            };
        } else {
            verifier = [expectedNodeType](MCObjectHandle verifiedObject) {
                if (!verifiedObject || gSDK->GetObjectTypeN(verifiedObject) != expectedNodeType ||
                    !IsUserVisibleObjectType(expectedNodeType)) {
                    throw std::runtime_error(
                        "created object failed semantic node-type verification");
                }
            };
        }
        adoptedArtifact = transaction.AdoptFinal(
            object,
            spec.objectType == "wall"
                ? Transactions::ObjectFamily::Wall
                : spec.objectType == "parametric"
                    ? Transactions::ObjectFamily::Parametric
                : spec.objectType == "symbol"
                    ? Transactions::ObjectFamily::Symbol
                    : Transactions::ObjectFamily::Simple,
            expectedNodeType,
            std::move(verifier));
        unregisteredGuard.Release();
    }
    if (pendingExternalAfter) {
        transaction.TrackExternalAfter(*pendingExternalAfter, pendingExternalHandle);
    }
    if (!adoptedArtifact) {
        throw std::runtime_error("created object left the factory without transaction ownership");
    }
    if (transactionArtifact) {
        *transactionArtifact = *adoptedArtifact;
    }
    return object;
}

std::string CreatedPrimitiveJson(const CreatedPrimitive& created) {
    std::string json = "{\"index\":";
    json += std::to_string(created.index);
    json += ",\"type\":";
    json += JsonString(created.objectType);
    json += ",\"handle\":";
    json += JsonString(HandleId(created.handle));
    const auto uuid = ObjectUuidString(created.handle);
    if (!uuid.empty()) {
        json += ",\"uuid\":";
        json += JsonString(uuid);
    }
    if (!created.warnings.empty()) {
        json += ",\"warnings\":";
        json += JsonStringArray(created.warnings);
    }
    if (created.objectType == "polygon") {
        json += ",\"vertex_count\":";
        json += std::to_string(created.vertexCount);
        json += ",\"closed\":";
        json += created.closed ? "true" : "false";
    }
    json += ",\"native_node_type\":";
    json += std::to_string(created.nativeNodeType);
    json += ",\"verified\":";
    json += created.semanticallyVerified ? "true" : "false";
    json += ",\"object\":";
    json += ObjectJson(created.handle);
    json += "}";
    return json;
}

Transactions::ObjectFamily ExternalMutationFamily(MCObjectHandle object) {
    if (!object || !gSDK) {
        return Transactions::ObjectFamily::Simple;
    }
    const short nodeType = gSDK->GetObjectTypeN(object);
    if (nodeType == kSlabNode) {
        return Transactions::ObjectFamily::Slab;
    }
    if (nodeType == kRoofContainerNode) {
        return Transactions::ObjectFamily::Roof;
    }
    if (nodeType == kWallNode) {
        return Transactions::ObjectFamily::Wall;
    }
    if (nodeType != kParametricNode) {
        return Transactions::ObjectFamily::Simple;
    }
    try {
        const std::string plugin = DescribeParametricObject(object).universalPluginName;
        if (plugin == "Space") return Transactions::ObjectFamily::Space;
        if (plugin == "Slab") return Transactions::ObjectFamily::Slab;
        if (plugin == "Door") return Transactions::ObjectFamily::Door;
        if (plugin == "Window") return Transactions::ObjectFamily::Window;
        return Transactions::ObjectFamily::Parametric;
    } catch (...) {
        // An unclassifiable parametric object must retain strict explicit
        // after-state registration instead of inheriting compound BIM policy.
    }
    return Transactions::ObjectFamily::Simple;
}

std::string CreatedPrimitiveListJson(const std::vector<CreatedPrimitive>& created) {
    std::string json = "[";
    for (std::size_t index = 0; index < created.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        json += CreatedPrimitiveJson(created[index]);
    }
    json += "]";
    return json;
}

std::string CompactCreatedPrimitiveListJson(const std::vector<CreatedPrimitive>& created) {
    std::string json = "[";
    for (std::size_t index = 0; index < created.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        const auto& item = created[index];
        json += "{\"index\":";
        json += std::to_string(item.index);
        json += ",\"type\":";
        json += JsonString(item.objectType);
        json += ",\"handle\":";
        json += JsonString(HandleId(item.handle));
        json += ",\"native_node_type\":";
        json += std::to_string(item.nativeNodeType);
        json += ",\"verified\":";
        json += item.semanticallyVerified ? "true" : "false";
        if (!item.warnings.empty()) {
            json += ",\"warnings\":";
            json += JsonStringArray(item.warnings);
        }
        if (item.objectType == "polygon") {
            json += ",\"vertex_count\":";
            json += std::to_string(item.vertexCount);
            json += ",\"closed\":";
            json += item.closed ? "true" : "false";
        }
        json += "}";
    }
    json += "]";
    return json;
}

Transactions::TransactionOptions MakeSdkManagedObjectTransactionOptions(
    std::size_t expectedArtifactCount) {
    Transactions::TransactionOptions options;
    options.expectedArtifactCount = expectedArtifactCount;
    options.sdkManagedRegistrationFamilies = {
        Transactions::ObjectFamily::Symbol,
        Transactions::ObjectFamily::Wall,
        Transactions::ObjectFamily::Parametric,
        Transactions::ObjectFamily::Space,
        Transactions::ObjectFamily::Slab,
        Transactions::ObjectFamily::Roof,
        Transactions::ObjectFamily::Door,
        Transactions::ObjectFamily::Window,
    };
    return options;
}

std::string HandleCreateTypedObject(const Params& params, const std::string& label, const TXString& undoName) {
    const PrimitiveSpec spec = ParsePrimitiveSpec(params, label);

    auto options = MakeSdkManagedObjectTransactionOptions(2u);
    Transactions::NativeTransaction transaction(*gSDK, undoName, std::move(options));
    try {
        std::vector<std::string> warnings;
        Transactions::ArtifactId artifact = 0;
        MCObjectHandle object = CreatePrimitiveFromSpec(spec, transaction, &artifact, &warnings);
        const auto transactionReceipt = transaction.Commit();

        std::string json = "{\"type\":";
        json += JsonString(spec.objectType);
        json += ",\"handle\":";
        json += JsonString(HandleId(object));
        const auto uuid = ObjectUuidString(object);
        if (!uuid.empty()) {
            json += ",\"uuid\":";
            json += JsonString(uuid);
        }
        if (!warnings.empty()) {
            json += ",\"warnings\":";
            json += JsonStringArray(warnings);
        }
        if (spec.objectType == "polygon") {
            json += ",\"vertex_count\":";
            json += std::to_string(spec.points.size());
            json += ",\"closed\":";
            json += spec.closed ? "true" : "false";
        }
        json += ",\"object\":";
        json += ObjectJson(object);
        json += ",\"transaction_artifact\":";
        json += std::to_string(artifact);
        json += ",\"undo_committed\":";
        json += transactionReceipt.endUndoEventSucceeded ? "true" : "false";
        json += "}";
        return json;
    } catch (...) {
        transaction.RollbackAndRethrow(std::current_exception());
    }
}

std::string HandleCreateObject(const Params& params) {
    return HandleCreateTypedObject(params, "create_object", TXString("Vectorworks MCP create object"));
}

std::string HandleCreateWall(const Params& params) {
    return HandleCreateTypedObject(params, "create_wall", TXString("Vectorworks MCP create wall"));
}

std::string HandleCreateText(const Params& params) {
    return HandleCreateTypedObject(params, "create_text", TXString("Vectorworks MCP create text"));
}

std::string HandleCreateLinearDimension(const Params& params) {
    return HandleCreateTypedObject(params, "create_linear_dimension", TXString("Vectorworks MCP create dimension"));
}

int ParseIntegerString(const std::string& rawValue, const std::string& label, int minValue, int maxValue) {
    const std::string value = TrimCopy(rawValue);
    if (value.empty()) {
        throw std::invalid_argument(label + " is required");
    }
    std::size_t parsedChars = 0u;
    long parsed = 0;
    try {
        parsed = std::stol(value, &parsedChars, 10);
    } catch (...) {
        throw std::invalid_argument(label + " must be an integer");
    }
    if (parsedChars != value.size()) {
        throw std::invalid_argument(label + " must be an integer");
    }
    if (parsed < minValue || parsed > maxValue) {
        throw std::invalid_argument(label + " must be between " + std::to_string(minValue) + " and " + std::to_string(maxValue));
    }
    return static_cast<int>(parsed);
}

ColorRef ColorRefFromRgbString(const std::string& value) {
    std::vector<int> components;
    std::stringstream input(value);
    std::string part;
    while (std::getline(input, part, ',')) {
        components.push_back(ParseIntegerString(part, "color component", 0, 65535));
    }
    if (components.size() != 3u) {
        throw std::invalid_argument("color must be r,g,b with components in 0..65535");
    }

    RGBColor rgb = {};
    rgb.red = static_cast<unsigned short>(components[0]);
    rgb.green = static_cast<unsigned short>(components[1]);
    rgb.blue = static_cast<unsigned short>(components[2]);
    ColorRef colorRef = 0;
    gSDK->RGBToColorIndexN(rgb, colorRef, false);
    return colorRef;
}

MCObjectHandle ObjectHandleFromSessionId(const std::string& handleId) {
    // Throws BridgeError("stale_reference") for unknown, foreign-document or
    // deleted objects.
    return Handles::Resolve(handleId);
}

void ApplyObjectProperty(MCObjectHandle object, const std::string& propertyName, const std::string& value) {
    if (propertyName == "name") {
        const GSError err = gSDK->SetObjectName(object, TXString(value.c_str()));
        if (err != 0) {
            throw std::runtime_error("Vectorworks rejected object name");
        }
        return;
    }
    if (propertyName == "class") {
        if (value.empty()) {
            throw std::invalid_argument("class value is required");
        }
        InternalIndex classId = gSDK->ClassNameToID(TXString(value.c_str()));
        if (!gSDK->ValidClass(classId)) {
            classId = gSDK->AddClass(TXString(value.c_str()));
        }
        if (!gSDK->ValidClass(classId)) {
            throw std::runtime_error("Vectorworks rejected class: " + value);
        }
        gSDK->SetObjectClass(object, classId);
        return;
    }
    if (propertyName == "lineWeight") {
        const int lineWeight = ParseIntegerString(value, "lineWeight", 0, std::numeric_limits<short>::max());
        gSDK->SetLineWeight(object, static_cast<short>(lineWeight));
        return;
    }
    if (propertyName == "fillPattern") {
        const int fillPattern = ParseIntegerString(
            value,
            "fillPattern",
            0,
            std::numeric_limits<short>::max());
        gSDK->SetFillPat(object, static_cast<InternalIndex>(fillPattern));
        return;
    }
    if (propertyName == "opacity") {
        const int opacity = ParseIntegerString(value, "opacity", 0, 100);
        gSDK->SetOpacity(object, static_cast<OpacityRef>(opacity));
        return;
    }
    if (propertyName == "fillColor" || propertyName == "penColor") {
        ObjectColorType colors = {};
        if (!gSDK->GetColor(object, colors)) {
            throw std::runtime_error("Vectorworks could not read object color");
        }
        const ColorRef colorRef = ColorRefFromRgbString(value);
        if (propertyName == "fillColor") {
            colors.fillFore = colorRef;
            colors.fillBack = colorRef;
        } else {
            colors.penFore = colorRef;
            colors.penBack = colorRef;
        }
        gSDK->SetColor(object, colors);
        return;
    }
    throw std::invalid_argument("unsupported property: " + propertyName);
}

std::string HandleSetProperty(const Params& params) {
    const std::string handleId = GetStringParam(params, "handle");
    const std::string propertyName = GetStringParam(params, "property_name");
    const std::string value = GetStringParam(params, "value");
    if (propertyName.empty()) {
        throw std::invalid_argument("property_name is required");
    }
    if (value.size() > kMaxPropertyValueChars) {
        throw std::invalid_argument("property value is too long");
    }

    MCObjectHandle object = ObjectHandleFromSessionId(handleId);
    const short type = gSDK->GetObjectTypeN(object);
    if (!IsUserVisibleObjectType(type)) {
        throw std::invalid_argument("handle does not refer to a user-visible object");
    }

    const std::string before = ObjectJson(object);
    gSDK->SupportUndoAndRemove();
    gSDK->SetUndoMethod(kUndoSwapObjects);
    gSDK->NameUndoEvent(TXString("Vectorworks MCP set property"));
    try {
        if (!gSDK->AddBeforeSwapObject(object)) {
            throw std::runtime_error("Vectorworks failed to register the object before property mutation");
        }
        ApplyObjectProperty(object, propertyName, value);
        gSDK->ResetObject(object);
        if (!gSDK->AddAfterSwapObject(object)) {
            throw std::runtime_error("Vectorworks failed to register the object after property mutation");
        }
        EndUndoEventOrThrow("property mutation");
    } catch (...) {
        gSDK->UndoAndRemove();
        throw;
    }

    std::string json = "{\"changed\":true,\"handle\":";
    json += JsonString(handleId);
    json += ",\"property_name\":";
    json += JsonString(propertyName);
    json += ",\"value\":";
    json += JsonString(value);
    json += ",\"before\":";
    json += before;
    json += ",\"after\":";
    json += ObjectJson(object);
    json += "}";
    return json;
}

std::string ClassListJson() {
    std::vector<std::string> classNames;
    VWClass::ForEachClass(false, [&](const VWClass& clas) {
        const auto name = TxToUtf8(clas.GetName());
        if (!name.empty()) {
            classNames.push_back(name);
        }
    });
    std::sort(classNames.begin(), classNames.end());
    return JsonStringArray(classNames);
}

std::string HandleManageClasses(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    const std::string className = TrimCopy(GetStringParam(params, "class_name"));
    if (action == "list") {
        return ClassListJson();
    }

    if (action != "create" && action != "delete") {
        throw std::invalid_argument("unknown class action. Use: list, create, delete");
    }
    if (className.empty()) {
        throw std::invalid_argument("class_name is required");
    }
    if (className.size() > kMaxPropertyValueChars) {
        throw std::invalid_argument("class_name is too long");
    }

    const TXString txClassName(className.c_str());
    InternalIndex classId = gSDK->ClassNameToID(txClassName);
    const bool existed = gSDK->ValidClass(classId);
    if (action == "create") {
        bool created = false;
        if (!existed) {
            gSDK->SupportUndoAndRemove();
            gSDK->SetUndoMethod(kUndoSwapObjects);
            gSDK->NameUndoEvent(TXString("Vectorworks MCP create class"));
            try {
                classId = gSDK->AddClass(txClassName);
                created = gSDK->ValidClass(classId);
                if (!created) {
                    throw std::runtime_error("Vectorworks rejected class: " + className);
                }
                EndUndoEventOrThrow("class creation");
            } catch (...) {
                gSDK->UndoAndRemove();
                throw;
            }
        }
        std::string json = "{\"action\":\"create\",\"class_name\":";
        json += JsonString(className);
        json += ",\"created\":";
        json += created ? "true" : "false";
        json += ",\"existed\":";
        json += existed ? "true" : "false";
        json += "}";
        return json;
    }

    if (GetStringParam(params, "confirm") != "DELETE_CLASS") {
        throw std::invalid_argument("class deletion requires confirm='DELETE_CLASS'");
    }
    if (!existed) {
        throw std::invalid_argument("class not found: " + className);
    }
    if (className == "None") {
        throw std::invalid_argument("refusing to delete the None class");
    }

    gSDK->SupportUndoAndRemove();
    gSDK->SetUndoMethod(kUndoSwapObjects);
    gSDK->NameUndoEvent(TXString("Vectorworks MCP delete class"));
    bool stillExists = true;
    try {
        gSDK->DeleteClass(txClassName);
        stillExists = gSDK->ValidClass(gSDK->ClassNameToID(txClassName));
        if (stillExists) {
            throw std::runtime_error("Vectorworks did not delete class: " + className);
        }
        EndUndoEventOrThrow("class deletion");
    } catch (...) {
        gSDK->UndoAndRemove();
        throw;
    }
    std::string json = "{\"action\":\"delete\",\"class_name\":";
    json += JsonString(className);
    json += ",\"deleted\":";
    json += stillExists ? "false" : "true";
    json += "}";
    return json;
}

enum class ApplyOperationKind {
    Create,
    SetProperty,
    Transform,
    Reshape,
    UpdateParametric,
    Duplicate,
    Delete,
};

struct ApplyOperation {
    ApplyOperationKind kind = ApplyOperationKind::Create;
    PrimitiveSpec primitive;
    std::string localRef;
    std::string target;
    std::string propertyName;
    std::string propertyValue;
    double deltaX = 0.0;
    double deltaY = 0.0;
    double rotationDegrees = 0.0;
    double scaleX = 1.0;
    double scaleY = 1.0;
    bool hasPivot = false;
    double pivotX = 0.0;
    double pivotY = 0.0;
    double startX = 0.0;
    double startY = 0.0;
    double endX = 0.0;
    double endY = 0.0;
    std::string confirmation;
    // Create ops only: false excludes this wall from automatic joining.
    bool joinWalls = true;
};

// ---- automatic wall joins (apply_operations) ------------------------------
// Walls created in one apply_operations call are joined inside the same undo
// event, before any door/window/plug-in insertion that follows them:
//   * L join where two wall endpoints coincide (within kWallJoinTolerance),
//   * T join where an endpoint lies on another wall's centreline.
// ISDK JoinWalls(first, second, firstPt, secondPt, JoinModifierType, capped,
// showAlerts) is identical in SDK 2024 and 2025; the points pick the wall
// portion to keep, so each wall's midpoint is passed.

constexpr double kWallJoinTolerance = 1.0;  // internal units (mm)

struct WallJoinRecord {
    std::string firstUuid;
    std::string secondUuid;
    std::string kind;
    double x = 0.0;
    double y = 0.0;
    bool joined = false;
};

double PointDistance(const WorldPt& a, const WorldPt& b) {
    return std::hypot(a.x - b.x, a.y - b.y);
}

// Distance from p to segment ab and the parameter t of the projection.
double SegmentDistance(const WorldPt& p, const WorldPt& a, const WorldPt& b, double& t) {
    const double dx = b.x - a.x;
    const double dy = b.y - a.y;
    const double lengthSquared = dx * dx + dy * dy;
    if (lengthSquared <= 0.0) {
        t = 0.0;
        return PointDistance(p, a);
    }
    t = std::clamp(((p.x - a.x) * dx + (p.y - a.y) * dy) / lengthSquared, 0.0, 1.0);
    const WorldPt projection(a.x + t * dx, a.y + t * dy);
    return PointDistance(p, projection);
}

bool NearlyParallel(const WorldPt& as, const WorldPt& ae, const WorldPt& bs, const WorldPt& be) {
    const double ax = ae.x - as.x, ay = ae.y - as.y, bx = be.x - bs.x, by = be.y - bs.y;
    const double la = std::hypot(ax, ay), lb = std::hypot(bx, by);
    if (la <= 0.0 || lb <= 0.0) return true;
    return std::abs(ax * by - ay * bx) / (la * lb) < 1e-3;  // ~0.06 degrees
}

std::vector<WallJoinRecord> JoinCreatedWalls(
    const std::vector<MCObjectHandle>& walls,
    std::set<std::pair<MCObjectHandle, MCObjectHandle>>& done) {
    std::vector<WallJoinRecord> records;
    for (std::size_t i = 0; i < walls.size(); ++i) {
        for (std::size_t j = i + 1; j < walls.size(); ++j) {
            MCObjectHandle a = walls[i];
            MCObjectHandle b = walls[j];
            if (!a || !b || done.count({a, b}) != 0u) continue;
            if (gSDK->GetObjectTypeN(a) != kWallNode || gSDK->GetObjectTypeN(b) != kWallNode) continue;
            WorldPt as, ae, bs, be;
            gSDK->GetEndPoints(a, as, ae);
            gSDK->GetEndPoints(b, bs, be);
            const WorldPt midA((as.x + ae.x) / 2.0, (as.y + ae.y) / 2.0);
            const WorldPt midB((bs.x + be.x) / 2.0, (bs.y + be.y) / 2.0);
            const bool parallel = NearlyParallel(as, ae, bs, be);
            WallJoinRecord record;
            MCObjectHandle first = nullptr;
            MCObjectHandle second = nullptr;
            WorldPt firstPt, secondPt, at;
            JoinModifierType modifier = kLWallJoin;
            // L: shared endpoint (collinear continuations are left alone).
            const WorldPt endsA[2] = {as, ae};
            const WorldPt endsB[2] = {bs, be};
            for (const WorldPt& pa : endsA) {
                for (const WorldPt& pb : endsB) {
                    if (!first && !parallel && PointDistance(pa, pb) <= kWallJoinTolerance) {
                        first = a; second = b; firstPt = midA; secondPt = midB; at = pa;
                        modifier = kLWallJoin; record.kind = "L";
                    }
                }
            }
            // T: an endpoint on the other wall's centreline, away from its ends.
            if (!first && !parallel) {
                const auto onInterior = [&](const WorldPt& p, const WorldPt& s0, const WorldPt& s1) {
                    double t = 0.0;
                    return SegmentDistance(p, s0, s1, t) <= kWallJoinTolerance &&
                        PointDistance(p, s0) > kWallJoinTolerance && PointDistance(p, s1) > kWallJoinTolerance;
                };
                for (const WorldPt& pa : endsA) {
                    if (!first && onInterior(pa, bs, be)) {
                        first = a; second = b; firstPt = midA; secondPt = midB; at = pa;
                    }
                }
                for (const WorldPt& pb : endsB) {
                    if (!first && onInterior(pb, as, ae)) {
                        first = b; second = a; firstPt = midB; secondPt = midA; at = pb;
                    }
                }
                if (first) {
                    modifier = kTWallJoin;
                    record.kind = "T";
                }
            }
            if (!first) continue;
            done.insert({a, b});
            record.firstUuid = ObjectUuidString(first);
            record.secondUuid = ObjectUuidString(second);
            record.x = at.x;
            record.y = at.y;
            record.joined = gSDK->JoinWalls(first, second, firstPt, secondPt, modifier, false, false) != 0;
            records.push_back(std::move(record));
        }
    }
    return records;
}

std::string WallJoinsJson(const std::vector<WallJoinRecord>& records) {
    std::string json = "[";
    for (std::size_t index = 0; index < records.size(); ++index) {
        if (index) json += ",";
        json += "{\"first_uuid\":" + JsonString(records[index].firstUuid) +
            ",\"second_uuid\":" + JsonString(records[index].secondUuid) +
            ",\"kind\":" + JsonString(records[index].kind) +
            ",\"point\":[" + JsonNumber(records[index].x) + "," + JsonNumber(records[index].y) + "]" +
            ",\"joined\":" + (records[index].joined ? "true" : "false") + "}";
    }
    return json + "]";
}

struct ApplyOperationsCacheEntry {
    std::string idempotencyKey;
    std::string documentIdentity;
    std::uint64_t operationsFingerprint = 0u;
    std::string transactionJson;
};

constexpr std::size_t kMaxApplyOperations = 250u;
constexpr std::size_t kMaxApplyOperationsCacheEntries = 128u;
constexpr std::size_t kMaxApplyReferenceChars = 128u;
std::deque<ApplyOperationsCacheEntry> gApplyOperationsCache;

bool IsSupportedPropertyName(const std::string& propertyName) {
    return propertyName == "name" ||
        propertyName == "class" ||
        propertyName == "fillColor" ||
        propertyName == "penColor" ||
        propertyName == "fillPattern" ||
        propertyName == "lineWeight" ||
        propertyName == "opacity";
}

bool IsExplicitExternalObjectReference(const std::string& target) {
    return target.rfind("uuid:", 0u) == 0u ||
        target.rfind("name:", 0u) == 0u ||
        target.rfind("handle:", 0u) == 0u;
}

void ValidateMutationTarget(const ApplyOperation& operation, const std::string& label) {
    if (operation.target.empty()) {
        throw std::invalid_argument(label + ".target is required");
    }
    if (operation.target.size() > kMaxApplyReferenceChars) {
        throw std::invalid_argument(label + ".target is too long");
    }
    if (!IsExplicitExternalObjectReference(operation.target)) {
        throw std::invalid_argument(
            label + ".target must be an explicit uuid:, name:, or handle: object reference");
    }
}

std::string ActiveDocumentIdentity() {
    if (!gSDK || !gSDK->GetDrawingHeader()) {
        throw std::runtime_error("an active document is required for operation replay identity");
    }
    return Documentation::BridgeSessionId() + ":" + Docs::ActiveFingerprint();
}

std::uint64_t ApplyOperationsFingerprint(const Params& params) {
    constexpr std::uint64_t offsetBasis = 14695981039346656037ull;
    constexpr std::uint64_t prime = 1099511628211ull;
    std::uint64_t hash = offsetBasis;
    const int operationCount = GetRequiredBoundedIntParam(
        params,
        "operation_count",
        1,
        static_cast<int>(kMaxApplyOperations));
    auto addText = [&](const std::string& text) {
        for (const unsigned char ch : text) {
            hash ^= static_cast<std::uint64_t>(ch);
            hash *= prime;
        }
        hash ^= 0xffu;
        hash *= prime;
    };
    addText(std::to_string(operationCount));
    for (int index = 1; index <= operationCount; ++index) {
        const std::string key = "operation_" + std::to_string(index) + "_json";
        const std::string operationJson = GetStringParam(params, key);
        if (operationJson.empty()) {
            throw std::invalid_argument(key + " is required");
        }
        addText(operationJson);
    }
    return hash;
}

std::optional<std::string> FindCachedApplyOperations(
    const std::string& idempotencyKey,
    const std::string& documentIdentity,
    std::uint64_t operationsFingerprint) {
    if (idempotencyKey.empty()) {
        return std::nullopt;
    }
    for (const auto& entry : gApplyOperationsCache) {
        if (entry.idempotencyKey == idempotencyKey) {
            if (entry.operationsFingerprint != operationsFingerprint) {
                throw std::invalid_argument(
                    "idempotency_key was already used with a different operations payload");
            }
            if (entry.documentIdentity != documentIdentity) {
                throw std::invalid_argument(
                    "idempotency_key was already used in a different active document");
            }
            return entry.transactionJson;
        }
    }
    return std::nullopt;
}

void StoreCachedApplyOperations(
    const std::string& idempotencyKey,
    const std::string& documentIdentity,
    std::uint64_t operationsFingerprint,
    const std::string& transactionJson) {
    if (idempotencyKey.empty()) {
        return;
    }
    gApplyOperationsCache.erase(
        std::remove_if(
            gApplyOperationsCache.begin(),
            gApplyOperationsCache.end(),
            [&](const ApplyOperationsCacheEntry& entry) {
                return entry.idempotencyKey == idempotencyKey;
            }),
        gApplyOperationsCache.end());
    gApplyOperationsCache.push_back({
        idempotencyKey,
        documentIdentity,
        operationsFingerprint,
        transactionJson,
    });
    while (gApplyOperationsCache.size() > kMaxApplyOperationsCacheEntries) {
        gApplyOperationsCache.pop_front();
    }
}

std::string WrapApplyOperationsResult(
    const std::string& transactionJson,
    const std::string& idempotencyKey,
    bool replayed) {
    std::string json = "{\"ok\":true,\"atomic\":true,\"verified\":true,\"replayed\":";
    json += replayed ? "true" : "false";
    if (!idempotencyKey.empty()) {
        json += ",\"idempotency_key\":";
        json += JsonString(idempotencyKey);
    }
    json += ",\"transaction\":";
    json += transactionJson;
    // Convenience copy of transaction.wall_joins at the top level.
    const std::string key = "\"wall_joins\":[";
    const auto start = transactionJson.rfind(key);
    if (start != std::string::npos) {
        // Copy the array text verbatim (bracket matching outside strings).
        const std::size_t open = start + key.size() - 1u;
        int depth = 0;
        bool inString = false;
        for (std::size_t index = open; index < transactionJson.size(); ++index) {
            const char c = transactionJson[index];
            if (inString) {
                if (c == '\\') ++index;
                else if (c == '"') inString = false;
                continue;
            }
            if (c == '"') inString = true;
            else if (c == '[') ++depth;
            else if (c == ']' && --depth == 0) {
                json += ",\"wall_joins\":" + transactionJson.substr(open, index - open + 1u);
                break;
            }
        }
    }
    json += "}";
    return json;
}

std::vector<ApplyOperation> ParseApplyOperations(const Params& params) {
    const int operationCount = GetRequiredBoundedIntParam(
        params,
        "operation_count",
        1,
        static_cast<int>(kMaxApplyOperations));
    std::vector<ApplyOperation> operations;
    operations.reserve(static_cast<std::size_t>(operationCount));
    std::unordered_set<std::string> declaredLocalRefs;

    for (int index = 1; index <= operationCount; ++index) {
        const std::string key = "operation_" + std::to_string(index) + "_json";
        const std::string operationJson = GetStringParam(params, key);
        if (operationJson.empty()) {
            throw std::invalid_argument(key + " is required");
        }
        const Params operationParams = ParseParams(operationJson);
        const std::string op = ToLower(GetStringParam(operationParams, "op"));
        const std::string label = "operation_" + std::to_string(index);
        if (op == "create") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Create;
            operation.primitive = ParsePrimitiveSpec(operationParams, label);
            operation.joinWalls = GetLenientBoolParam(operationParams, "join_walls", true);
            if (!operation.primitive.wallUuid.empty() && operation.primitive.wallUuid.front() == '$' &&
                declaredLocalRefs.find(operation.primitive.wallUuid.substr(1)) == declaredLocalRefs.end()) {
                throw std::invalid_argument(
                    label + ".wall_uuid references a local_ref that is not declared earlier: " +
                    operation.primitive.wallUuid);
            }
            operation.localRef = GetStringParam(operationParams, "local_ref");
            if (operation.localRef.size() > kMaxApplyReferenceChars) {
                throw std::invalid_argument(label + ".local_ref is too long");
            }
            if (!operation.localRef.empty() && !declaredLocalRefs.insert(operation.localRef).second) {
                throw std::invalid_argument(label + ".local_ref is duplicated: " + operation.localRef);
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "set_property") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::SetProperty;
            operation.target = GetStringParam(operationParams, "target");
            operation.propertyName = GetStringParam(operationParams, "property_name");
            operation.propertyValue = GetStringParam(operationParams, "value");
            if (operation.target.empty()) {
                throw std::invalid_argument(label + ".target is required");
            }
            if (operation.target.size() > kMaxApplyReferenceChars) {
                throw std::invalid_argument(label + ".target is too long");
            }
            if (!IsSupportedPropertyName(operation.propertyName)) {
                throw std::invalid_argument(label + ".property_name is unsupported: " + operation.propertyName);
            }
            if (operation.propertyValue.size() > kMaxPropertyValueChars) {
                throw std::invalid_argument(label + ".value is too long");
            }
            if (operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.transform") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Transform;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.deltaX = GetFiniteNumberParam(operationParams, "delta_x", 0.0);
            operation.deltaY = GetFiniteNumberParam(operationParams, "delta_y", 0.0);
            operation.rotationDegrees = GetFiniteNumberParam(operationParams, "rotation_degrees", 0.0);
            operation.scaleX = GetFiniteNumberParam(operationParams, "scale_x", 1.0);
            operation.scaleY = GetFiniteNumberParam(operationParams, "scale_y", 1.0);
            if (operation.scaleX <= 0.0 || operation.scaleY <= 0.0 ||
                operation.scaleX > 1000000.0 || operation.scaleY > 1000000.0) {
                throw std::invalid_argument(label + ".scale_x and scale_y must be > 0 and <= 1000000");
            }
            if (std::abs(operation.deltaX) > 1000000000.0 ||
                std::abs(operation.deltaY) > 1000000000.0 ||
                std::abs(operation.rotationDegrees) > 360000.0) {
                throw std::invalid_argument(label + " transform values exceed supported bounds");
            }
            const bool hasPivotX = HasParam(operationParams, "pivot_x");
            const bool hasPivotY = HasParam(operationParams, "pivot_y");
            if (hasPivotX != hasPivotY) {
                throw std::invalid_argument(label + ".pivot_x and pivot_y must be provided together");
            }
            operation.hasPivot = hasPivotX;
            if (operation.hasPivot) {
                operation.pivotX = GetFiniteNumberParam(operationParams, "pivot_x", 0.0);
                operation.pivotY = GetFiniteNumberParam(operationParams, "pivot_y", 0.0);
            }
            if (operation.deltaX == 0.0 && operation.deltaY == 0.0 &&
                operation.rotationDegrees == 0.0 &&
                operation.scaleX == 1.0 && operation.scaleY == 1.0) {
                throw std::invalid_argument(label + " transform must change translation, rotation, or scale");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.reshape") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Reshape;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.startX = GetFiniteNumberParam(operationParams, "start_x", 0.0);
            operation.startY = GetFiniteNumberParam(operationParams, "start_y", 0.0);
            operation.endX = GetFiniteNumberParam(operationParams, "end_x", 0.0);
            operation.endY = GetFiniteNumberParam(operationParams, "end_y", 0.0);
            if (operation.startX == operation.endX && operation.startY == operation.endY) {
                throw std::invalid_argument(label + " reshape endpoints must not be identical");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.update_parametric") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::UpdateParametric;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.primitive = ParsePrimitiveSpec(operationParams, label);
            if (operation.primitive.objectType != "parametric" ||
                operation.primitive.parametricValues.empty()) {
                throw std::invalid_argument(
                    label + " object.update_parametric requires object_type=parametric and at least one parameter");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.duplicate") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Duplicate;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.localRef = GetStringParam(operationParams, "local_ref");
            if (operation.localRef.empty()) {
                throw std::invalid_argument(label + ".local_ref is required for object.duplicate");
            }
            if (operation.localRef.size() > kMaxApplyReferenceChars) {
                throw std::invalid_argument(label + ".local_ref is too long");
            }
            if (!declaredLocalRefs.insert(operation.localRef).second) {
                throw std::invalid_argument(label + ".local_ref is duplicated: " + operation.localRef);
            }
            operation.deltaX = GetFiniteNumberParam(operationParams, "delta_x", 0.0);
            operation.deltaY = GetFiniteNumberParam(operationParams, "delta_y", 0.0);
            if (std::abs(operation.deltaX) > 1000000000.0 ||
                std::abs(operation.deltaY) > 1000000000.0) {
                throw std::invalid_argument(label + " duplicate offsets exceed supported bounds");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.delete") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Delete;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                const auto found = declaredLocalRefs.find(localRef);
                if (found == declaredLocalRefs.end()) {
                    throw std::invalid_argument(
                        label + ".target references an unknown or deleted local_ref: " + localRef);
                }
                declaredLocalRefs.erase(found);
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.confirmation = GetStringParam(operationParams, "confirm");
            if (operation.confirmation != "DELETE_OBJECT") {
                throw std::invalid_argument(label + ".confirm must be DELETE_OBJECT");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        throw std::invalid_argument(
            label + ".op must be create, set_property, object.transform, object.reshape, "
            "object.update_parametric, object.duplicate, or object.delete");
    }
    return operations;
}

short ParseDocumentationVisibility(
    const Params& params,
    const std::string& key,
    short defaultValue = 0) {
    const int value = GetBoundedIntParam(params, key, defaultValue, -1, 2);
    if (value == 1) {
        throw std::invalid_argument(key + " must be -1 (hidden), 0 (normal), or 2 (grayed)");
    }
    return static_cast<short>(value);
}

void ValidateDocumentationReference(
    const std::string& reference,
    const std::unordered_set<std::string>& localRefs,
    const std::string& label,
    bool allowLocal = true) {
    if (reference.rfind("uuid:", 0u) == 0u && reference.size() > 5u) {
        return;
    }
    if (allowLocal && reference.rfind("$", 0u) == 0u && reference.size() > 1u &&
        localRefs.find(reference.substr(1u)) != localRefs.end()) {
        return;
    }
    throw std::invalid_argument(
        label + (allowLocal
            ? " must be an exact uuid: reference or a previously declared $operation_id"
            : " must be an exact uuid: reference"));
}

std::vector<Documentation::VisibilityEntry> ParseDocumentationVisibilityEntries(
    const Params& params,
    const std::string& prefix,
    const std::string& identitySuffix,
    int count) {
    std::vector<Documentation::VisibilityEntry> entries;
    entries.reserve(static_cast<std::size_t>(count));
    std::unordered_set<std::string> identities;
    for (int index = 1; index <= count; ++index) {
        const std::string item = prefix + "_" + std::to_string(index);
        const std::string identity = GetStringParam(params, item + identitySuffix);
        if (identity.empty()) {
            throw std::invalid_argument(item + identitySuffix + " is required");
        }
        if (!identities.insert(identity).second) {
            throw std::invalid_argument(prefix + " contains a duplicate identity: " + identity);
        }
        entries.push_back({
            identity,
            ParseDocumentationVisibility(params, item + "_visibility", 0),
        });
    }
    return entries;
}

std::vector<WorldPt> ParseDocumentationPoints(
    const Params& params,
    const std::string& prefix,
    int count) {
    std::vector<WorldPt> points;
    points.reserve(static_cast<std::size_t>(count));
    for (int index = 1; index <= count; ++index) {
        const std::string item = prefix + "_" + std::to_string(index);
        points.emplace_back(
            GetFiniteNumberParam(params, item + "_x", 0.0),
            GetFiniteNumberParam(params, item + "_y", 0.0));
    }
    return points;
}

std::vector<Documentation::Operation> ParseDocumentationOperations(const Params& params) {
    const int operationCount = GetRequiredBoundedIntParam(
        params,
        "operation_count",
        1,
        static_cast<int>(kMaxApplyOperations));
    std::vector<Documentation::Operation> operations;
    operations.reserve(static_cast<std::size_t>(operationCount));
    std::unordered_set<std::string> localRefs;

    for (int index = 1; index <= operationCount; ++index) {
        const std::string key = "operation_" + std::to_string(index) + "_json";
        const std::string operationJson = GetStringParam(params, key);
        if (operationJson.empty()) {
            throw std::invalid_argument(key + " is required");
        }
        const Params item = ParseParams(operationJson);
        const std::string op = ToLower(GetStringParam(item, "op"));
        const std::string label = "operation_" + std::to_string(index);
        Documentation::Operation operation;

        const auto parseCreateLocalRef = [&] {
            operation.localRef = GetStringParam(item, "local_ref");
            if (operation.localRef.empty() || operation.localRef.size() > kMaxApplyReferenceChars ||
                !localRefs.insert(operation.localRef).second) {
                throw std::invalid_argument(label + ".local_ref must be unique, non-empty, and at most 128 characters");
            }
        };
        const auto parseSheetFields = [&](bool creating) {
            operation.hasName = creating || HasParam(item, "name");
            operation.name = GetStringParam(item, "name");
            operation.hasTitle = creating || HasParam(item, "title");
            operation.title = GetStringParam(item, "title");
            operation.hasDescription = HasParam(item, "description");
            operation.description = GetStringParam(item, "description");
            operation.hasDpi = HasParam(item, "dpi");
            operation.dpi = static_cast<short>(GetBoundedIntParam(item, "dpi", 72, 1, 32767));
            operation.hasSheetWidthMm = HasParam(item, "sheet_width_mm");
            operation.sheetWidthMm = GetFiniteNumberParam(item, "sheet_width_mm", 0.0);
            operation.hasSheetHeightMm = HasParam(item, "sheet_height_mm");
            operation.sheetHeightMm = GetFiniteNumberParam(item, "sheet_height_mm", 0.0);
            operation.hasVisibility = HasParam(item, "visibility");
            operation.visibility = ParseDocumentationVisibility(item, "visibility", 0);
            if (creating && (operation.name.empty() || operation.title.empty())) {
                throw std::invalid_argument(label + " sheet layer create requires non-empty name and title");
            }
            if ((operation.hasSheetWidthMm && operation.sheetWidthMm <= 0.0) ||
                (operation.hasSheetHeightMm && operation.sheetHeightMm <= 0.0)) {
                throw std::invalid_argument(label + " sheet dimensions must be greater than zero millimetres");
            }
        };
        const auto parseViewportFields = [&](bool creating) {
            operation.hasName = creating || HasParam(item, "name");
            operation.name = GetStringParam(item, "name");
            operation.hasScale = creating || HasParam(item, "scale");
            operation.scale = GetFiniteNumberParam(item, "scale", 1.0);
            if (operation.hasScale && operation.scale <= 0.0) {
                throw std::invalid_argument(label + ".scale must be greater than zero");
            }
            const bool hasX = HasParam(item, "x");
            const bool hasY = HasParam(item, "y");
            if (hasX != hasY || (creating && !hasX)) {
                throw std::invalid_argument(label + ".x and .y must be supplied together");
            }
            operation.hasPlacement = hasX;
            operation.x = GetFiniteNumberParam(item, "x", 0.0);
            operation.y = GetFiniteNumberParam(item, "y", 0.0);
            operation.hasProjectionType = creating || HasParam(item, "projection_type");
            operation.projectionType = static_cast<short>(GetBoundedIntParam(
                item, "projection_type", 0, std::numeric_limits<short>::min(), std::numeric_limits<short>::max()));
            operation.hasViewType = creating || HasParam(item, "view_type");
            operation.viewType = static_cast<short>(GetBoundedIntParam(
                item, "view_type", standardViewTop, standardViewFront, standardViewBottomLeftRearIso));
            operation.hasRenderType = creating || HasParam(item, "render_type");
            operation.renderType = static_cast<short>(GetBoundedIntParam(
                item, "render_type", 0, std::numeric_limits<short>::min(), std::numeric_limits<short>::max()));
            operation.hasForegroundRenderType = creating || HasParam(item, "foreground_render_type");
            operation.foregroundRenderType = static_cast<short>(GetBoundedIntParam(
                item, "foreground_render_type", 0, std::numeric_limits<short>::min(), std::numeric_limits<short>::max()));

            operation.hasSourceLayers = HasParam(item, "source_layer_count");
            const int layerCount = GetBoundedIntParam(item, "source_layer_count", 0, 0, 500);
            if (creating && layerCount == 0) {
                throw std::invalid_argument(label + " viewport create requires at least one source layer");
            }
            operation.sourceLayers = ParseDocumentationVisibilityEntries(
                item, "source_layer", "_ref", layerCount);
            operation.replaceSourceLayers = GetBoolParam(item, "replace_source_layers", creating);

            operation.hasSourceClasses = HasParam(item, "source_class_count");
            const int classCount = GetBoundedIntParam(item, "source_class_count", 0, 0, 500);
            if (creating && classCount == 0) {
                throw std::invalid_argument(label + " viewport create requires at least one source class");
            }
            operation.sourceClasses = ParseDocumentationVisibilityEntries(
                item, "source_class", "_name", classCount);
            operation.replaceSourceClasses = GetBoolParam(item, "replace_source_classes", creating);

            operation.clearCrop = GetBoolParam(item, "clear_crop", false);
            const int cropCount = GetBoundedIntParam(item, "crop_point_count", 0, 0, 1000);
            operation.hasCrop = cropCount > 0;
            operation.cropPoints = ParseDocumentationPoints(item, "crop_point", cropCount);
            if (operation.hasCrop && cropCount < 3) {
                throw std::invalid_argument(label + " viewport crop requires at least three points");
            }
            if (operation.clearCrop && operation.hasCrop) {
                throw std::invalid_argument(label + " cannot clear and replace a viewport crop together");
            }
        };

        if (op == "sheet_layer.create") {
            operation.kind = Documentation::OperationKind::CreateSheetLayer;
            parseCreateLocalRef();
            parseSheetFields(true);
        } else if (op == "sheet_layer.update") {
            operation.kind = Documentation::OperationKind::UpdateSheetLayer;
            operation.targetRef = GetStringParam(item, "target");
            ValidateDocumentationReference(operation.targetRef, localRefs, label + ".target", false);
            parseSheetFields(false);
            if (!operation.hasName && !operation.hasTitle && !operation.hasDescription &&
                !operation.hasDpi && !operation.hasSheetWidthMm &&
                !operation.hasSheetHeightMm && !operation.hasVisibility) {
                throw std::invalid_argument(label + " sheet layer update has no changes");
            }
        } else if (op == "sheet_layer.delete") {
            operation.kind = Documentation::OperationKind::DeleteSheetLayer;
            operation.targetRef = GetStringParam(item, "target");
            ValidateDocumentationReference(operation.targetRef, localRefs, label + ".target", false);
            operation.confirmation = GetStringParam(item, "confirm");
        } else if (op == "viewport.create") {
            operation.kind = Documentation::OperationKind::CreateViewport;
            parseCreateLocalRef();
            operation.sheetLayerRef = GetStringParam(item, "sheet_layer_ref");
            ValidateDocumentationReference(operation.sheetLayerRef, localRefs, label + ".sheet_layer_ref");
            parseViewportFields(true);
        } else if (op == "viewport.update") {
            operation.kind = Documentation::OperationKind::UpdateViewport;
            operation.targetRef = GetStringParam(item, "target");
            operation.sheetLayerRef = GetStringParam(item, "sheet_layer_ref");
            ValidateDocumentationReference(operation.targetRef, localRefs, label + ".target", false);
            ValidateDocumentationReference(operation.sheetLayerRef, localRefs, label + ".sheet_layer_ref");
            parseViewportFields(false);
            if (!operation.hasName && !operation.hasScale && !operation.hasPlacement &&
                !operation.hasProjectionType && !operation.hasViewType && !operation.hasRenderType &&
                !operation.hasForegroundRenderType && !operation.hasSourceLayers &&
                !operation.hasSourceClasses && !operation.hasCrop && !operation.clearCrop) {
                throw std::invalid_argument(label + " viewport update has no changes");
            }
        } else if (op == "viewport.delete") {
            operation.kind = Documentation::OperationKind::DeleteViewport;
            operation.targetRef = GetStringParam(item, "target");
            operation.sheetLayerRef = GetStringParam(item, "sheet_layer_ref");
            ValidateDocumentationReference(operation.targetRef, localRefs, label + ".target", false);
            ValidateDocumentationReference(operation.sheetLayerRef, localRefs, label + ".sheet_layer_ref");
            operation.confirmation = GetStringParam(item, "confirm");
        } else if (op == "viewport_annotation.create") {
            operation.kind = Documentation::OperationKind::CreateViewportAnnotation;
            parseCreateLocalRef();
            operation.sheetLayerRef = GetStringParam(item, "sheet_layer_ref");
            operation.viewportRef = GetStringParam(item, "viewport_ref");
            ValidateDocumentationReference(operation.sheetLayerRef, localRefs, label + ".sheet_layer_ref");
            ValidateDocumentationReference(operation.viewportRef, localRefs, label + ".viewport_ref");
            operation.annotationKind = ToLower(GetStringParam(item, "annotation_kind"));
            operation.hasName = HasParam(item, "name");
            operation.name = GetStringParam(item, "name");
            operation.hasClassName = true;
            operation.className = GetStringParam(item, "class_name");
            operation.hasText = operation.annotationKind == "text";
            operation.text = GetStringParam(item, "text");
            operation.x1 = GetFiniteNumberParam(item, "x1", 0.0);
            operation.y1 = GetFiniteNumberParam(item, "y1", 0.0);
            operation.x2 = GetFiniteNumberParam(item, "x2", 0.0);
            operation.y2 = GetFiniteNumberParam(item, "y2", 0.0);
            operation.offset = GetFiniteNumberParam(item, "offset", 0.0);
            operation.textOffset = GetFiniteNumberParam(item, "text_offset", 0.0);
            operation.dimensionType = static_cast<short>(GetBoundedIntParam(
                item, "dimension_type", 0, std::numeric_limits<short>::min(), std::numeric_limits<short>::max()));
            operation.markerStyle = static_cast<short>(GetBoundedIntParam(item, "marker_style", 0, 0, 32767));
            operation.markerSize = static_cast<short>(GetBoundedIntParam(item, "marker_size", 0, 0, 32767));
            operation.markerAngle = static_cast<short>(GetBoundedIntParam(item, "marker_angle", 0, -360, 360));
            const int pointCount = GetBoundedIntParam(item, "point_count", 0, 0, 1000);
            operation.points = ParseDocumentationPoints(item, "point", pointCount);
            if (operation.className.empty() ||
                (operation.annotationKind != "text" && operation.annotationKind != "dimension" &&
                 operation.annotationKind != "marker" && operation.annotationKind != "redline")) {
                throw std::invalid_argument(label + " annotation create requires class_name and a supported annotation_kind");
            }
            if (operation.annotationKind == "text" && operation.text.empty()) {
                throw std::invalid_argument(label + " text annotation requires non-empty text");
            }
            if ((operation.annotationKind == "dimension" || operation.annotationKind == "marker") &&
                operation.x1 == operation.x2 && operation.y1 == operation.y2) {
                throw std::invalid_argument(label + " annotation endpoints must not be identical");
            }
            if (operation.annotationKind == "redline" && pointCount < 3) {
                throw std::invalid_argument(label + " redline annotation requires at least three points");
            }
        } else if (op == "viewport_annotation.update") {
            operation.kind = Documentation::OperationKind::UpdateViewportAnnotation;
            operation.targetRef = GetStringParam(item, "target");
            operation.sheetLayerRef = GetStringParam(item, "sheet_layer_ref");
            operation.viewportRef = GetStringParam(item, "viewport_ref");
            ValidateDocumentationReference(operation.targetRef, localRefs, label + ".target", false);
            ValidateDocumentationReference(operation.sheetLayerRef, localRefs, label + ".sheet_layer_ref");
            ValidateDocumentationReference(operation.viewportRef, localRefs, label + ".viewport_ref");
            operation.hasName = HasParam(item, "name");
            operation.name = GetStringParam(item, "name");
            operation.hasClassName = HasParam(item, "class_name");
            operation.className = GetStringParam(item, "class_name");
            operation.hasText = HasParam(item, "text");
            operation.text = GetStringParam(item, "text");
            const bool hasDx = HasParam(item, "delta_x");
            const bool hasDy = HasParam(item, "delta_y");
            if (hasDx != hasDy) {
                throw std::invalid_argument(label + ".delta_x and .delta_y must be supplied together");
            }
            operation.hasDelta = hasDx;
            operation.deltaX = GetFiniteNumberParam(item, "delta_x", 0.0);
            operation.deltaY = GetFiniteNumberParam(item, "delta_y", 0.0);
            if (!operation.hasName && !operation.hasClassName && !operation.hasText && !operation.hasDelta) {
                throw std::invalid_argument(label + " annotation update has no changes");
            }
        } else if (op == "viewport_annotation.delete") {
            operation.kind = Documentation::OperationKind::DeleteViewportAnnotation;
            operation.targetRef = GetStringParam(item, "target");
            operation.sheetLayerRef = GetStringParam(item, "sheet_layer_ref");
            operation.viewportRef = GetStringParam(item, "viewport_ref");
            ValidateDocumentationReference(operation.targetRef, localRefs, label + ".target", false);
            ValidateDocumentationReference(operation.sheetLayerRef, localRefs, label + ".sheet_layer_ref");
            ValidateDocumentationReference(operation.viewportRef, localRefs, label + ".viewport_ref");
            operation.confirmation = GetStringParam(item, "confirm");
        } else {
            throw std::invalid_argument(label + ".op is not a supported documentation operation");
        }
        operations.push_back(std::move(operation));
    }
    return operations;
}

std::string HandleApplyDocumentationOperations(const Params& params) {
    if (!kDocumentationWritesEnabled) {
        throw std::runtime_error("documentation writes are unavailable pending native Undo/Redo acceptance; no writes started");
    }
    return Documentation::ApplyOperations(
        *gSDK,
        ParseDocumentationTargetBinding(params),
        ParseDocumentationOperations(params),
        GetStringParam(params, "idempotency_key"),
        GetStringParam(params, "plan_hash"));
}

using NameIndex = std::unordered_map<std::string, std::vector<MCObjectHandle>>;

// Builds a name -> objects index with one document traversal; used when an
// apply_operations batch references several name: targets (M5).
NameIndex BuildNameIndex() {
    NameIndex index;
    gSDK->ForEachObjectN(
        allObjects + descendIntoAll + descendIntoViewports + descendIntoAuxLists,
        [&](MCObjectHandle object) {
            const auto add = [&](MCObjectHandle candidate) {
                if (!candidate || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(candidate))) {
                    return;
                }
                TXString objectName;
                gSDK->GetObjectName(candidate, objectName);
                if (!objectName.IsEmpty()) {
                    auto& bucket = index[TxToUtf8(objectName)];
                    if (std::find(bucket.begin(), bucket.end(), candidate) == bucket.end()) {
                        bucket.push_back(candidate);
                    }
                }
            };
            add(object);
            WallInserts::ForEachInsert(object, add);
        });
    return index;
}

MCObjectHandle ResolveApplyOperationTarget(
    const std::string& target,
    const std::unordered_map<std::string, MCObjectHandle>& localObjects,
    const NameIndex* nameIndex = nullptr) {
    if (!target.empty() && target.front() == '$') {
        const auto found = localObjects.find(target.substr(1));
        if (found == localObjects.end() || !found->second) {
            throw std::invalid_argument("local operation target could not be resolved: " + target);
        }
        return found->second;
    }
    constexpr std::string_view uuidPrefix = "uuid:";
    if (target.compare(0, uuidPrefix.size(), uuidPrefix) == 0) {
        const std::string uuid = target.substr(uuidPrefix.size());
        if (uuid.empty()) {
            throw std::invalid_argument("uuid operation target is empty");
        }
        MCObjectHandle object = gSDK->GetObjectByUuid(TXString(uuid.c_str()));
        if (!object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
            throw std::invalid_argument("uuid operation target was not found: " + uuid);
        }
        return object;
    }
    constexpr std::string_view namePrefix = "name:";
    if (target.compare(0, namePrefix.size(), namePrefix) == 0) {
        const std::string name = target.substr(namePrefix.size());
        if (name.empty()) {
            throw std::invalid_argument("name operation target is empty");
        }
        std::vector<MCObjectHandle> matches;
        if (nameIndex) {
            const auto found = nameIndex->find(name);
            if (found != nameIndex->end()) matches = found->second;
        } else {
            MCObjectHandle named = gSDK->GetNamedObject(TXString(name.c_str()));
            if (named && IsUserVisibleObjectType(gSDK->GetObjectTypeN(named))) {
                matches.push_back(named);
            }
        }
        if (matches.empty()) {
            throw std::invalid_argument("name operation target was not found: " + name);
        }
        if (matches.size() != 1u) {
            throw std::invalid_argument("name operation target is ambiguous: " + name);
        }
        return matches.front();
    }
    constexpr std::string_view handlePrefix = "handle:";
    const std::string handleId = target.compare(0, handlePrefix.size(), handlePrefix) == 0
        ? target.substr(handlePrefix.size())
        : target;
    return ObjectHandleFromSessionId(handleId);
}

struct PreparedApplyOperationTargets {
    std::vector<MCObjectHandle> handles;
    std::vector<std::string> deleteUuids;
};

PreparedApplyOperationTargets PrepareApplyOperationTargets(
    const std::vector<ApplyOperation>& operations) {
    PreparedApplyOperationTargets prepared;
    prepared.handles.resize(operations.size(), nullptr);
    prepared.deleteUuids.resize(operations.size());
    const std::unordered_map<std::string, MCObjectHandle> noLocalObjects;
    std::unordered_set<MCObjectHandle> deletedTargets;
    std::optional<NameIndex> nameIndex;
    std::size_t nameTargets = 0;
    for (const auto& operation : operations) {
        if (operation.target.rfind("name:", 0u) == 0u) ++nameTargets;
    }
    if (nameTargets > 1u) {
        nameIndex = BuildNameIndex();
    }

    for (std::size_t index = 0; index < operations.size(); ++index) {
        const auto& operation = operations[index];
        if (operation.kind == ApplyOperationKind::Create ||
            ((operation.kind == ApplyOperationKind::SetProperty ||
              operation.kind == ApplyOperationKind::Transform ||
              operation.kind == ApplyOperationKind::Reshape ||
              operation.kind == ApplyOperationKind::UpdateParametric ||
              operation.kind == ApplyOperationKind::Duplicate ||
              operation.kind == ApplyOperationKind::Delete) &&
             !operation.target.empty() && operation.target.front() == '$')) {
            continue;
        }
        MCObjectHandle object = ResolveApplyOperationTarget(
            operation.target, noLocalObjects, nameIndex ? &*nameIndex : nullptr);
        if (!object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
            throw std::invalid_argument(
                "operation_" + std::to_string(index + 1u) + ".target is not a user-visible object");
        }
        if (deletedTargets.find(object) != deletedTargets.end()) {
            throw std::invalid_argument(
                "operation_" + std::to_string(index + 1u) +
                ".target refers to an object deleted earlier in the same transaction");
        }
        prepared.handles[index] = object;
        if (operation.kind == ApplyOperationKind::Delete) {
            TXString uuid;
            if (!gSDK->GetObjectUuid(object, uuid) || uuid.IsEmpty()) {
                throw std::invalid_argument(
                    "operation_" + std::to_string(index + 1u) +
                    ".target does not expose a UUID required for verified deletion");
            }
            prepared.deleteUuids[index] = TxToUtf8(uuid);
            deletedTargets.insert(object);
        }
    }
    return prepared;
}

std::string HandleApplyOperations(const Params& params) {
    const std::string idempotencyKey = GetStringParam(params, "idempotency_key");
    if (idempotencyKey.size() > kMaxApplyReferenceChars) {
        throw std::invalid_argument("idempotency_key is too long");
    }
    const auto operations = ParseApplyOperations(params);
    const std::uint64_t operationsFingerprint = ApplyOperationsFingerprint(params);
    if (const auto cached = FindCachedApplyOperations(
            idempotencyKey,
            ActiveDocumentIdentity(),
            operationsFingerprint)) {
        if (params.find("expected_document_fingerprint") != params.end()) {
            auto expected = ParseDocumentationTargetBinding(params);
            expected.hasDirty = false;
            Documentation::ValidateTargetBinding(*gSDK, expected);
        }
        return WrapApplyOperationsResult(*cached, idempotencyKey, true);
    }
    if (params.find("expected_document_fingerprint") != params.end())
        Documentation::ValidateTargetBinding(*gSDK, ParseDocumentationTargetBinding(params));
    PreparedApplyOperationTargets preparedTargets = PrepareApplyOperationTargets(operations);

    std::unordered_map<std::string, MCObjectHandle> localObjects;
    std::unordered_map<std::string, Transactions::ArtifactId> localArtifacts;
    std::unordered_set<MCObjectHandle> createdHandles;
    std::unordered_set<MCObjectHandle> dirtyHandles;
    std::unordered_set<std::string> deletedExternalUuids;
    std::unordered_map<std::string, std::string> externalBefore;
    std::unordered_map<std::string, MCObjectHandle> externalFinalHandles;
    std::unordered_map<std::string, Transactions::ExternalMutationId> externalMutationIds;
    std::vector<CreatedPrimitive> created;
    std::vector<std::string> operationResults;
    Transactions::TransactionReceipt transactionReceipt;
    created.reserve(operations.size());
    operationResults.reserve(operations.size());

    const bool joinWallsEnabled = GetLenientBoolParam(params, "join_walls", true);
    std::vector<MCObjectHandle> joinableWalls;
    std::size_t joinedWallCount = 0;
    std::set<std::pair<MCObjectHandle, MCObjectHandle>> joinedPairs;
    std::vector<WallJoinRecord> wallJoins;
    auto transactionOptions = MakeSdkManagedObjectTransactionOptions(operations.size() * 2u);
    Transactions::NativeTransaction transaction(
        *gSDK,
        TXString("Vectorworks MCP apply operations"),
        std::move(transactionOptions));
    try {
        const auto trackExternalBefore = [&](MCObjectHandle object) -> std::string {
            const std::string uuid = ObjectUuidString(object);
            if (uuid.empty()) {
                throw std::runtime_error(
                    "external mutation target does not expose a stable Vectorworks UUID");
            }
            if (externalMutationIds.find(uuid) == externalMutationIds.end()) {
                externalBefore.emplace(uuid, ObjectJson(object));
                externalFinalHandles.emplace(uuid, object);
                externalMutationIds.emplace(
                    uuid,
                    transaction.TrackExternalBefore(object, ExternalMutationFamily(object)));
            } else {
                externalFinalHandles[uuid] = object;
            }
            return uuid;
        };

        const auto runPendingWallJoins = [&]() {
            if (!joinWallsEnabled || joinableWalls.size() < 2u || joinableWalls.size() == joinedWallCount) return;
            auto records = JoinCreatedWalls(joinableWalls, joinedPairs);
            joinedWallCount = joinableWalls.size();
            for (auto& record : records) wallJoins.push_back(std::move(record));
        };
        for (std::size_t index = 0; index < operations.size(); ++index) {
            const auto& operation = operations[index];
            const bool isWallCreate = operation.kind == ApplyOperationKind::Create &&
                ToLower(operation.primitive.objectType) == "wall";
            if (!isWallCreate) {
                // Join the walls created so far before anything is inserted
                // into them (doors/windows) or they are edited.
                runPendingWallJoins();
            }
            if (operation.kind == ApplyOperationKind::Create) {
                std::vector<std::string> warnings;
                Transactions::ArtifactId artifact = 0;
                MCObjectHandle object = CreatePrimitiveFromSpec(
                    operation.primitive, transaction, &artifact, &warnings, &localObjects);
                createdHandles.insert(object);
                if (isWallCreate && operation.joinWalls && gSDK->GetObjectTypeN(object) == kWallNode) {
                    joinableWalls.push_back(object);
                }
                if (!operation.localRef.empty()) {
                    localObjects.emplace(operation.localRef, object);
                    localArtifacts.emplace(operation.localRef, artifact);
                }
                created.push_back({
                    static_cast<int>(index + 1u),
                    operation.primitive.objectType,
                    object,
                    warnings,
                    operation.primitive.points.size(),
                    operation.primitive.closed,
                    gSDK->GetObjectTypeN(object),
                    true,
                });
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"create\",\"handle\":" + JsonString(HandleId(object)) +
                    ",\"uuid\":" + JsonString(transaction.Uuid(artifact));
                if (MCObjectHandle hostWall = WallInserts::HostWall(object)) {
                    result += ",\"parent_uuid\":" + JsonString(ObjectUuidString(hostWall)) + ",\"host\":\"wall\"";
                }
                if (!operation.localRef.empty()) {
                    result += ",\"local_ref\":";
                    result += JsonString(operation.localRef);
                }
                result += "}";
                operationResults.push_back(std::move(result));
                continue;
            }

            MCObjectHandle object = preparedTargets.handles[index];
            if (!object) {
                object = ResolveApplyOperationTarget(operation.target, localObjects);
            }
            std::string externalUuid;
            if (operation.kind == ApplyOperationKind::Delete &&
                createdHandles.find(object) == createdHandles.end()) {
                // Deletion of an existing object: identity only; the SDK
                // registers the removal itself (DeleteObject(h, true)).
                externalUuid = ObjectUuidString(object);
                if (externalUuid.empty()) {
                    throw std::runtime_error(
                        "external deletion target does not expose a stable Vectorworks UUID");
                }
                if (externalMutationIds.find(externalUuid) != externalMutationIds.end()) {
                    throw std::invalid_argument(
                        "operation_" + std::to_string(index + 1u) +
                        " deletes an object that an earlier operation in the same transaction changed "
                        "(or hosts an insert created in it); delete it in a separate apply_operations call");
                }
                externalBefore.emplace(externalUuid, ObjectJson(object));
                externalMutationIds.emplace(
                    externalUuid,
                    transaction.TrackExternalDeleteTarget(object, ExternalMutationFamily(object)));
            } else if (operation.kind != ApplyOperationKind::Duplicate &&
                createdHandles.find(object) == createdHandles.end()) {
                externalUuid = trackExternalBefore(object);
            }
            if (operation.kind == ApplyOperationKind::SetProperty) {
                ApplyObjectProperty(object, operation.propertyName, operation.propertyValue);
                dirtyHandles.insert(object);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"set_property\",\"target\":" + JsonString(operation.target) +
                    ",\"property_name\":" + JsonString(operation.propertyName) +
                    ",\"value\":" + JsonString(operation.propertyValue) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::Transform) {
                const short semanticTypeBefore = gSDK->GetObjectTypeN(object);
                const std::string before = ObjectJson(object);
                WorldRect bounds;
                gSDK->GetObjectBounds(object, bounds);
                const WorldPt pivot = operation.hasPivot
                    ? WorldPt(operation.pivotX, operation.pivotY)
                    : WorldPt(
                        (bounds.Left() + bounds.Right()) / 2.0,
                        (bounds.Top() + bounds.Bottom()) / 2.0);
                if (operation.scaleX != 1.0 || operation.scaleY != 1.0) {
                    gSDK->ScaleObjectN(object, pivot, operation.scaleX, operation.scaleY);
                }
                if (operation.rotationDegrees != 0.0) {
                    MCObjectHandle rotatedObject = object;
                    gSDK->RotateObjectN(rotatedObject, pivot, operation.rotationDegrees);
                    if (!rotatedObject) {
                        throw std::runtime_error("Vectorworks returned a null handle after object.transform rotation");
                    }
                    if (rotatedObject != object) {
                        for (auto& preparedHandle : preparedTargets.handles) {
                            if (preparedHandle == object) {
                                preparedHandle = rotatedObject;
                            }
                        }
                        if (dirtyHandles.erase(object) != 0u) {
                            dirtyHandles.insert(rotatedObject);
                        }
                        for (auto& localEntry : localObjects) {
                            if (localEntry.second == object) {
                                localEntry.second = rotatedObject;
                            }
                        }
                        if (createdHandles.erase(object) != 0u) {
                            createdHandles.insert(rotatedObject);
                        }
                        if (!externalUuid.empty()) {
                            externalFinalHandles[externalUuid] = rotatedObject;
                        }
                        object = rotatedObject;
                    }
                }
                if (operation.deltaX != 0.0 || operation.deltaY != 0.0) {
                    gSDK->MoveObject(object, operation.deltaX, operation.deltaY);
                }
                gSDK->ResetObject(object);
                if (gSDK->GetObjectTypeN(object) != semanticTypeBefore) {
                    throw std::runtime_error("object.transform changed the object's semantic node type");
                }
                dirtyHandles.insert(object);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.transform\",\"target\":" + JsonString(operation.target) +
                    ",\"applied\":{\"delta_x\":" + JsonNumber(operation.deltaX) +
                    ",\"delta_y\":" + JsonNumber(operation.deltaY) +
                    ",\"rotation_degrees\":" + JsonNumber(operation.rotationDegrees) +
                    ",\"scale_x\":" + JsonNumber(operation.scaleX) +
                    ",\"scale_y\":" + JsonNumber(operation.scaleY) +
                    ",\"pivot\":[" + JsonNumber(pivot.x) + "," + JsonNumber(pivot.y) + "]}" +
                    ",\"before\":" + before + ",\"after\":" + ObjectJson(object) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::Reshape) {
                const short semanticTypeBefore = gSDK->GetObjectTypeN(object);
                if (semanticTypeBefore != kWallNode && semanticTypeBefore != kLineNode) {
                    throw std::invalid_argument(
                        "object.reshape currently supports native wall and line targets only");
                }
                WorldPt priorStart;
                WorldPt priorEnd;
                gSDK->GetEndPoints(object, priorStart, priorEnd);
                const WorldPt requestedStart(operation.startX, operation.startY);
                const WorldPt requestedEnd(operation.endX, operation.endY);
                gSDK->SetEndPoints(object, requestedStart, requestedEnd);
                gSDK->ResetObject(object);
                WorldPt actualStart;
                WorldPt actualEnd;
                gSDK->GetEndPoints(object, actualStart, actualEnd);
                if (gSDK->GetObjectTypeN(object) != semanticTypeBefore ||
                    !WorldCoordsAreNearlyEqual(actualStart.x, requestedStart.x) ||
                    !WorldCoordsAreNearlyEqual(actualStart.y, requestedStart.y) ||
                    !WorldCoordsAreNearlyEqual(actualEnd.x, requestedEnd.x) ||
                    !WorldCoordsAreNearlyEqual(actualEnd.y, requestedEnd.y)) {
                    throw std::runtime_error("object.reshape endpoint readback mismatch");
                }
                dirtyHandles.insert(object);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.reshape\",\"target\":" + JsonString(operation.target) +
                    ",\"before_endpoints\":[[" + JsonNumber(priorStart.x) + "," + JsonNumber(priorStart.y) +
                    "],[" + JsonNumber(priorEnd.x) + "," + JsonNumber(priorEnd.y) + "]]" +
                    ",\"after_endpoints\":[[" + JsonNumber(actualStart.x) + "," + JsonNumber(actualStart.y) +
                    "],[" + JsonNumber(actualEnd.x) + "," + JsonNumber(actualEnd.y) + "]]}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::UpdateParametric) {
                if (gSDK->GetObjectTypeN(object) != kParametricNode) {
                    throw std::invalid_argument(
                        "object.update_parametric target is not a native parametric object");
                }
                MCObjectHandle parentBefore = gSDK->ParentObject(object);
                const bool hostedByWall = parentBefore && gSDK->GetObjectTypeN(parentBefore) == kWallNode;
                const std::string before = ObjectJson(object);
                UpdateVerifiedParametricObject(
                    object,
                    operation.primitive.pluginName,
                    operation.primitive.descriptorFingerprint,
                    operation.primitive.parametricValues);
                if (hostedByWall && !IsObjectHostedByWall(object, parentBefore)) {
                    throw std::runtime_error(
                        "object.update_parametric changed the exact wall host");
                }
                dirtyHandles.insert(object);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.update_parametric\",\"target\":" + JsonString(operation.target) +
                    ",\"plugin_name\":" + JsonString(operation.primitive.pluginName) +
                    ",\"parameter_count\":" +
                    std::to_string(operation.primitive.parametricValues.size()) +
                    ",\"wall_host_preserved\":" + (hostedByWall ? "true" : "false") +
                    ",\"before\":" + before + ",\"after\":" + ObjectJson(object) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::Duplicate) {
                const short sourceType = gSDK->GetObjectTypeN(object);
                MCObjectHandle duplicate = gSDK->DuplicateObject(object);
                if (!duplicate) {
                    throw std::runtime_error("Vectorworks failed to duplicate the explicit object target");
                }
                UnregisteredCreatedObjectGuard duplicateGuard(duplicate);
                if (!gSDK->InsertObjectAfter(duplicate, object)) {
                    throw std::runtime_error("Vectorworks failed to duplicate and insert the explicit object target");
                }
                if (gSDK->GetObjectTypeN(duplicate) != sourceType) {
                    throw std::runtime_error("object.duplicate changed the object's semantic node type");
                }
                if (operation.deltaX != 0.0 || operation.deltaY != 0.0) {
                    gSDK->MoveObject(duplicate, operation.deltaX, operation.deltaY);
                    gSDK->ResetObject(duplicate);
                }
                TXString sourceUuid;
                TXString duplicateUuid;
                if (!gSDK->GetObjectUuid(object, sourceUuid) || sourceUuid.IsEmpty() ||
                    !gSDK->GetObjectUuid(duplicate, duplicateUuid) || duplicateUuid.IsEmpty() ||
                    sourceUuid == duplicateUuid ||
                    gSDK->ParentObject(duplicate) != gSDK->ParentObject(object)) {
                    throw std::runtime_error("object.duplicate failed semantic identity or container readback");
                }
                const auto artifact = transaction.AdoptFinal(
                    duplicate,
                    Transactions::ObjectFamily::Simple,
                    sourceType,
                    [sourceType](MCObjectHandle verifiedDuplicate) {
                        if (!verifiedDuplicate ||
                            gSDK->GetObjectTypeN(verifiedDuplicate) != sourceType) {
                            throw std::runtime_error(
                                "object.duplicate failed semantic verification at commit");
                        }
                    });
                duplicateGuard.Release();
                createdHandles.insert(duplicate);
                localObjects.emplace(operation.localRef, duplicate);
                localArtifacts.emplace(operation.localRef, artifact);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.duplicate\",\"target\":" + JsonString(operation.target) +
                    ",\"local_ref\":" + JsonString(operation.localRef) +
                    ",\"delta_x\":" + JsonNumber(operation.deltaX) +
                    ",\"delta_y\":" + JsonNumber(operation.deltaY) +
                    ",\"source\":" + ObjectJson(object) +
                    ",\"duplicate\":" + ObjectJson(duplicate) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::Delete) {
                const std::string before = ObjectJson(object);
                std::string uuid;
                std::vector<std::string> cascaded;
                if (!operation.target.empty() && operation.target.front() == '$') {
                    const std::string localRef = operation.target.substr(1);
                    const auto artifactEntry = localArtifacts.find(localRef);
                    if (artifactEntry == localArtifacts.end()) {
                        throw std::runtime_error(
                            "local deletion target has no transaction artifact: " + localRef);
                    }
                    uuid = transaction.Uuid(artifactEntry->second);
                    transaction.DisposeFinal(artifactEntry->second);
                    createdHandles.erase(object);
                    dirtyHandles.erase(object);
                    localObjects.erase(localRef);
                    localArtifacts.erase(artifactEntry);
                } else {
                    uuid = preparedTargets.deleteUuids[index];
                    cascaded = WallInserts::InsertUuids(object);
                    // Exactly one undo registration: DeleteObject(h, true) moves
                    // the object into the open undo event (no AddBefore, see
                    // NativeTransaction::TrackExternalDeleteTarget).
                    gSDK->DeleteObject(object, true);
                    if (gSDK->GetObjectByUuid(TXString(uuid.c_str())) != nullptr) {
                        throw std::runtime_error("Vectorworks did not delete the explicit object target");
                    }
                    const auto mutation = externalMutationIds.find(uuid);
                    if (mutation == externalMutationIds.end()) {
                        throw std::runtime_error(
                            "external deletion target was not registered before mutation");
                    }
                    transaction.TrackExternalDeleted(mutation->second);
                    deletedExternalUuids.insert(uuid);
                    dirtyHandles.erase(object);
                }
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.delete\",\"target\":" + JsonString(operation.target) +
                    ",\"before\":" + before + ",\"deleted\":true,\"uuid\":" + JsonString(uuid) +
                    ",\"cascaded_inserts\":" + JsonStringArray(cascaded) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            throw std::runtime_error("unsupported prepared apply operation kind");
        }

        runPendingWallJoins();
        for (MCObjectHandle object : dirtyHandles) {
            gSDK->ResetObject(object);
        }
        for (const auto& entry : externalMutationIds) {
            if (deletedExternalUuids.find(entry.first) != deletedExternalUuids.end()) {
                continue;
            }
            const auto finalHandle = externalFinalHandles.find(entry.first);
            if (finalHandle == externalFinalHandles.end() || !finalHandle->second) {
                throw std::runtime_error(
                    "external mutation target lost its final handle before commit");
            }
            transaction.TrackExternalAfter(entry.second, finalHandle->second);
        }
        transactionReceipt = transaction.Commit();
    } catch (...) {
        transaction.RollbackAndRethrow(std::current_exception());
    }

    std::string transactionJson = "{\"committed\":true,\"verified\":true,\"operation_count\":";
    transactionJson += std::to_string(operations.size());
    transactionJson += ",\"applied_count\":";
    transactionJson += std::to_string(operations.size());
    transactionJson += ",\"operations\":[";
    for (std::size_t index = 0; index < operationResults.size(); ++index) {
        if (index != 0u) {
            transactionJson += ",";
        }
        transactionJson += operationResults[index];
    }
    transactionJson += "],\"created\":";
    // apply_operations already returns one receipt per wire operation. Keep
    // its created summary handle-based so the fast path does not perform
    // duplicate UUID, color, class, bounds, and object snapshot queries.
    transactionJson += CompactCreatedPrimitiveListJson(created);
    transactionJson += ",\"changed\":[";
    bool firstChanged = true;
    for (const auto& entry : externalBefore) {
        if (deletedExternalUuids.find(entry.first) != deletedExternalUuids.end()) {
            continue;
        }
        if (!firstChanged) {
            transactionJson += ",";
        }
        firstChanged = false;
        transactionJson += "{\"before\":";
        transactionJson += entry.second;
        transactionJson += ",\"after\":";
        const auto finalHandle = externalFinalHandles.find(entry.first);
        transactionJson += finalHandle == externalFinalHandles.end()
            ? "null"
            : ObjectJson(finalHandle->second);
        transactionJson += "}";
    }
    transactionJson += "],\"wall_joins\":" + WallJoinsJson(wallJoins);
    transactionJson += ",\"undo_committed\":";
    transactionJson += transactionReceipt.endUndoEventSucceeded ? "true" : "false";
    transactionJson += "}";

    StoreCachedApplyOperations(
        idempotencyKey,
        ActiveDocumentIdentity(),
        operationsFingerprint,
        transactionJson);
    return WrapApplyOperationsResult(transactionJson, idempotencyKey, false);
}

std::string HandleBatchCreateObjects(const Params& params) {
    const int objectCount = GetRequiredBoundedIntParam(params, "object_count", 1, 250);
    std::vector<PrimitiveSpec> specs;
    specs.reserve(static_cast<std::size_t>(objectCount));
    for (int index = 1; index <= objectCount; ++index) {
        const std::string key = "object_" + std::to_string(index) + "_json";
        const std::string objectJson = GetStringParam(params, key);
        if (objectJson.empty()) {
            throw std::invalid_argument(key + " is required");
        }
        specs.push_back(ParsePrimitiveSpec(ParseParams(objectJson), key));
    }

    std::vector<CreatedPrimitive> created;
    created.reserve(specs.size());
    auto options = MakeSdkManagedObjectTransactionOptions(specs.size() * 2u);
    Transactions::NativeTransaction transaction(
        *gSDK,
        TXString("Vectorworks MCP atomic batch create objects"),
        std::move(options));
    try {
        for (std::size_t index = 0; index < specs.size(); ++index) {
            std::vector<std::string> warnings;
            Transactions::ArtifactId artifact = 0;
            MCObjectHandle object = CreatePrimitiveFromSpec(
                specs[index], transaction, &artifact, &warnings);
            created.push_back({
                static_cast<int>(index + 1u),
                specs[index].objectType,
                object,
                warnings,
                specs[index].points.size(),
                specs[index].closed,
                gSDK->GetObjectTypeN(object),
                true,
            });
        }
        transaction.Commit();
    } catch (...) {
        transaction.RollbackAndRethrow(std::current_exception());
    }

    std::string json = "{\"atomic\":true,\"rollback_on_error\":true,\"created_count\":";
    json += std::to_string(created.size());
    json += ",\"created\":";
    json += CreatedPrimitiveListJson(created);
    json += "}";
    return json;
}

std::optional<std::string> DispatchLegacy(const std::string& action, const Params& params) {
    if (action == "query_objects") return HandleQueryObjects(params);
    if (action == "transaction_status") {
        if (params.find("expected_document_fingerprint") != params.end()) {
            auto expected = ParseDocumentationTargetBinding(params);
            expected.hasDirty = false;
            Documentation::ValidateTargetBinding(*gSDK, expected);
        }
        const auto key = GetStringParam(params, "idempotency_key");
        if (key.empty() || key.size() > kMaxApplyReferenceChars)
            throw std::invalid_argument("idempotency_key is required and must be at most 128 characters");
        const auto identity = ActiveDocumentIdentity();
        for (const auto& entry : gApplyOperationsCache) {
            if (entry.idempotencyKey == key && entry.documentIdentity == identity)
                return "{\"state\":\"committed\",\"recorded_result\":" +
                    entry.transactionJson + ",\"current_objects_verified\":false}";
        }
        return std::string(R"({"state":"unknown","retry_safe":false,"reason":"No retained receipt for this key in the active document and bridge session."})");
    }
    if (action == "get_document_info") return HandleGetDocumentInfo();
    if (action == "describe_parametric_schema") return HandleDescribeParametricSchema(params);
    if (action == "export_pdf" || action == "export_vectorworks_document" ||
        action == "import_dwg" || action == "export_dwg" ||
        action == "export_ifc" || action == "import_ifc") {
        const bool hasTargetBinding = params.find("expected_document_fingerprint") != params.end();
        if (hasTargetBinding) {
            Documentation::ValidateTargetBinding(*gSDK, ParseDocumentationTargetBinding(params));
        }
        std::string result = HandleNativeIO(action, params);
        if (hasTargetBinding) {
            Documentation::ValidateTargetBinding(*gSDK, ParseDocumentationTargetBinding(params));
        }
        return result;
    }
    if (action == "resources") return HandleResources(params);
    if (action == "symbol") return HandleSymbol(params);
    if (action == "worksheet") return HandleWorksheet(params);
    if (action == "get_layers") return HandleGetLayers();
    if (action == "get_sheet_layers") return HandleGetSheetLayers(params);
    if (action == "get_viewports") return HandleGetViewports(params);
    if (action == "get_viewport_annotations") return HandleGetViewportAnnotations(params);
    if (action == "get_objects") return HandleGetObjects(params);
    if (action == "drawing_summary") return HandleDrawingSummary(params);
    if (action == "apply_operations") return HandleApplyOperations(params);
    if (action == "apply_documentation_operations") return HandleApplyDocumentationOperations(params);
    if (action == "find_objects") return HandleFindObjects(params);
    if (action == "selection") return HandleSelection(params);
    if (action == "create_object") return HandleCreateObject(params);
    if (action == "batch_create_objects") return HandleBatchCreateObjects(params);
    if (action == "create_wall") return HandleCreateWall(params);
    if (action == "create_text") return HandleCreateText(params);
    if (action == "create_linear_dimension") return HandleCreateLinearDimension(params);
    if (action == "set_property") return HandleSetProperty(params);
    if (action == "manage_classes") return HandleManageClasses(params);
    return std::nullopt;
}

#endif

}  // namespace

namespace Legacy {

std::optional<std::string> Dispatch(const std::string& action, const Params& params) {
#if VECTORWORKS_MCP_HAS_SDK
    return DispatchLegacy(action, params);
#else
    (void) action;
    (void) params;
    return std::nullopt;
#endif
}

void ValidateBindingIfPresent(const Params& params) {
#if VECTORWORKS_MCP_HAS_SDK
    if (params.find("expected_document_fingerprint") != params.end()) {
        Documentation::ValidateTargetBinding(*gSDK, ParseDocumentationTargetBinding(params));
    }
#else
    (void) params;
#endif
}

void ResetCaches() {
#if VECTORWORKS_MCP_HAS_SDK
    gApplyOperationsCache.clear();
#endif
}

}  // namespace Legacy

}  // namespace VectorworksMCP
