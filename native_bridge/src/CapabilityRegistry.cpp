#include "StdAfx.h"

#include "CapabilityRegistry.hpp"

#include <array>
#include <iomanip>
#include <sstream>

namespace VectorworksMCP {
namespace {

constexpr std::array<ActionSpec, 35> kActionRegistry = {{
    {"ping", ExecutionContext::TransportThread, 0u, false, false},
    {"stop", ExecutionContext::TransportThread, 0u, false, false},
    {"capabilities", ExecutionContext::TransportThread, 0u, false, false},
    {"describe_parametric_schema", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"export_image", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"capture_view", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"export_pdf", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"export_vectorworks_document", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"import_dwg", ExecutionContext::VectorworksMainPluginContext, 4u, true, false},
    {"export_dwg", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"resources", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"symbol", ExecutionContext::VectorworksMainPluginContext, 4u, true, false},
    {"worksheet", ExecutionContext::VectorworksMainPluginContext, 4u, true, false},
    {"get_view", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"set_view", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"save_document", ExecutionContext::VectorworksMainPluginContext, 4u, true, false},
    {"open_document", ExecutionContext::VectorworksMainPluginContext, 4u, true, true},
    {"get_document_info", ExecutionContext::VectorworksMainPluginContext, 1u, false, false},
    {"get_layers", ExecutionContext::VectorworksMainPluginContext, 1u, false, false},
    {"get_sheet_layers", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"get_viewports", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"get_viewport_annotations", ExecutionContext::VectorworksMainPluginContext, 4u, false, false},
    {"get_objects", ExecutionContext::VectorworksMainPluginContext, 1u, false, false},
    {"selection", ExecutionContext::VectorworksMainPluginContext, 1u, true, true},
    {"create_object", ExecutionContext::VectorworksMainPluginContext, 1u, true, false},
    {"batch_create_objects", ExecutionContext::VectorworksMainPluginContext, 1u, true, false},
    {"create_wall", ExecutionContext::VectorworksMainPluginContext, 2u, true, false},
    {"create_text", ExecutionContext::VectorworksMainPluginContext, 2u, true, false},
    {"create_linear_dimension", ExecutionContext::VectorworksMainPluginContext, 2u, true, false},
    {"set_property", ExecutionContext::VectorworksMainPluginContext, 2u, true, false},
    {"manage_classes", ExecutionContext::VectorworksMainPluginContext, 2u, true, false},
    {"find_objects", ExecutionContext::VectorworksMainPluginContext, 3u, false, false},
    {"drawing_summary", ExecutionContext::VectorworksMainPluginContext, 3u, false, false},
    {"apply_operations", ExecutionContext::VectorworksMainPluginContext, 4u, true, false},
    {"apply_documentation_operations", ExecutionContext::VectorworksMainPluginContext, 4u, true, true},
}};

constexpr std::array<ObjectKindSpec, 20> kObjectKindRegistry = {{
    {"arc", "arc", "kArcNode", "available", R"({"required":[],"optional":["x1","y1","radius","start_angle","sweep_angle","name","class_name"]})", "arc node type and bounds readback"},
    {"box", "rect", "kRectangleNode", "available", R"({"required":[],"optional":["x1","y1","x2","y2","name","class_name"]})", "rectangle node type and bounds readback"},
    {"circle", "circle", "kOvalNode", "available", R"({"required":[],"optional":["x1","y1","radius","name","class_name"]})", "oval node type and equal-radius bounds readback"},
    {"line", "line", "kLineNode", "available", R"({"required":[],"optional":["x1","y1","x2","y2","name","class_name"]})", "line node type and endpoint readback"},
    {"oval", "oval", "kOvalNode", "available", R"({"required":[],"optional":["x1","y1","x2","y2","name","class_name"]})", "oval node type and bounds readback"},
    {"polygon", "polygon", "kPolygonNode", "available", R"({"required":["points"],"optional":["closed","name","class_name"]})", "polygon node type, vertex count, vertices, and closure readback"},
    {"polyline", "polygon", "kPolygonNode", "available", R"({"required":["points","closed"],"constraints":{"closed":false},"optional":["name","class_name"]})", "open kPolygonNode vertex and closure readback"},
    {"rect", "rect", "kRectangleNode", "available", R"({"required":[],"optional":["x1","y1","x2","y2","name","class_name"]})", "rectangle node type and bounds readback"},
    {"rectangle", "rect", "kRectangleNode", "available", R"({"required":[],"optional":["x1","y1","x2","y2","name","class_name"]})", "rectangle node type and bounds readback"},
    {"wall", "wall", "kWallNode", "available", R"({"required":[],"optional":["x1","y1","x2","y2","thickness","height","style_name","name","class_name"]})", "wall node type, endpoints, width, and corner heights readback"},
    {"door", "door", "kParametricNode", "requires_runtime_schema", R"({"required":["wall_uuid","x1","y1","width","height","descriptor_fingerprint"],"optional":["rotation","name","class_name"]})", "exact universal Door plugin, runtime schema fingerprint, width and height semantic readback, and exact wall host verification"},
    {"window", "window", "kParametricNode", "requires_runtime_schema", R"({"required":["wall_uuid","x1","y1","width","height","sill_height","descriptor_fingerprint"],"optional":["rotation","name","class_name"]})", "exact universal Window plugin, runtime schema fingerprint, width, height, and sill elevation semantic readback, and exact wall host verification"},
    {"text", "text", "kTextNode", "available", R"({"required":["x1","y1","text"],"optional":["width","rotation","text_size","fixed_size","wrap","name","class_name"]})", "text node type, content, placement, and style readback"},
    {"dimension", "linear_dimension", "kDimensionNode", "available", R"({"required":[],"optional":["x1","y1","x2","y2","offset","text_offset","direction_x","direction_y","dimension_type","class_name"]})", "dimension node type, creation-time class, and linear geometry readback"},
    {"linear_dimension", "linear_dimension", "kDimensionNode", "available", R"({"required":[],"optional":["x1","y1","x2","y2","offset","text_offset","direction_x","direction_y","dimension_type","class_name"]})", "dimension node type, creation-time class, and linear geometry readback"},
    {"slab", "slab", "kSlabNode", "available", R"({"required":["points"],"optional":["thickness","elevation","style_name","name","class_name"]})", "true slab node, boundary, thickness, elevation, and style readback"},
    {"roof", "roof", "kRoofContainerNode", "available", R"({"required":["points"],"optional":["slope","thickness","elevation","overhang","bearing_inset","vertical_miter","miter_type","generate_gable_walls","name","class_name"]})", "true roof container, faces, slope, thickness, and bearing readback"},
    {"space", "space", "kParametricNode", "available", R"({"required":["points"],"optional":["height","name","room_id","class_name"]})", "ISpaceObjectSupport NetPoly/GrossPoly geometry, name, room id, height, and area readback"},
    {"parametric", "parametric", "kParametricNode", "requires_runtime_schema", R"({"required":["plugin_name","descriptor_fingerprint"],"optional":["x1","y1","rotation","require_wall_host","wall_uuid","parameter_count","parameter_N_name","parameter_N_type","parameter_N_value","name","class_name"]})", "exact universal plugin name, descriptor fingerprint, parameter values, and optional wall host readback"},
    {"symbol", "symbol", "kSymbolNode", "requires_resource", R"({"required":["definition_name"],"optional":["x1","y1","rotation","name","class_name"]})", "exact symbol definition handle, node type, placement, and rotation readback"},
}};

bool IsImplemented(const ActionSpec& spec, bool cadHandlersImplemented) {
    return spec.nativePhase == 0u || cadHandlersImplemented;
}

const char* ExecutionContextName(ExecutionContext context) {
    return context == ExecutionContext::TransportThread
        ? "transport_thread"
        : "vectorworks_main_plugin_context";
}

void AppendJsonString(std::string& json, std::string_view value) {
    json.push_back('"');
    json.append(value.data(), value.size());
    json.push_back('"');
}

void AppendJsonBool(std::string& json, bool value) {
    json += value ? "true" : "false";
}

}  // namespace

const ActionSpec* FindActionSpec(std::string_view action) {
    for (const auto& spec : kActionRegistry) {
        if (spec.action == action) {
            return &spec;
        }
    }
    return nullptr;
}

bool RequiresCadMainContext(std::string_view action) {
    const ActionSpec* spec = FindActionSpec(action);
    return spec != nullptr && spec->context == ExecutionContext::VectorworksMainPluginContext;
}

std::size_t RegisteredActionCount() {
    return kActionRegistry.size();
}

std::size_t ImplementedActionCount(bool cadHandlersImplemented) {
    std::size_t count = 0u;
    for (const auto& spec : kActionRegistry) {
        if (IsImplemented(spec, cadHandlersImplemented)) {
            ++count;
        }
    }
    return count;
}

std::string ImplementedActionsJson(bool cadHandlersImplemented) {
    std::string json = "[";
    bool first = true;
    for (const auto& spec : kActionRegistry) {
        if (!IsImplemented(spec, cadHandlersImplemented)) {
            continue;
        }
        if (!first) {
            json.push_back(',');
        }
        first = false;
        AppendJsonString(json, spec.action);
    }
    json.push_back(']');
    return json;
}

std::string CapabilityDescriptorsJson(bool cadHandlersImplemented) {
    std::string json = "[";
    bool first = true;
    for (const auto& spec : kActionRegistry) {
        if (!first) {
            json.push_back(',');
        }
        first = false;
        json += "{\"action\":";
        AppendJsonString(json, spec.action);
        json += ",\"native_phase\":";
        json += std::to_string(spec.nativePhase);
        json += ",\"availability\":";
        AppendJsonString(json, IsImplemented(spec, cadHandlersImplemented) ? "available" : "unavailable");
        json += ",\"execution_context\":";
        AppendJsonString(json, ExecutionContextName(spec.context));
        json += ",\"may_write_document\":";
        AppendJsonBool(json, spec.mayWriteDocument);
        json += ",\"destructive\":";
        AppendJsonBool(json, spec.destructive);
        json.push_back('}');
    }
    json.push_back(']');
    return json;
}

std::string CreateObjectTypesJson(bool cadHandlersImplemented) {
    if (!cadHandlersImplemented) {
        return "[]";
    }
    std::string json = "[";
    bool first = true;
    for (const auto& spec : kObjectKindRegistry) {
        if (!first) {
            json.push_back(',');
        }
        first = false;
        AppendJsonString(json, spec.objectKind);
    }
    json.push_back(']');
    return json;
}

std::string ObjectKindDescriptorsJson(bool cadHandlersImplemented) {
    std::string json = "[";
    bool first = true;
    for (const auto& spec : kObjectKindRegistry) {
        if (!first) {
            json.push_back(',');
        }
        first = false;
        json += "{\"object_kind\":";
        AppendJsonString(json, spec.objectKind);
        json += ",\"canonical_object_kind\":";
        AppendJsonString(json, spec.canonicalObjectKind);
        json += ",\"is_alias\":";
        AppendJsonBool(json, spec.objectKind != spec.canonicalObjectKind);
        json += ",\"semantic_node_type\":";
        AppendJsonString(json, spec.semanticNodeType);
        json += ",\"availability\":";
        AppendJsonString(json, cadHandlersImplemented ? spec.availability : "unavailable");
        json += ",\"input_schema\":";
        json.append(spec.inputSchemaJson.data(), spec.inputSchemaJson.size());
        json += ",\"verifier\":";
        AppendJsonString(json, spec.verifier);
        json.push_back('}');
    }
    json.push_back(']');
    return json;
}

std::string CapabilityFingerprint(bool cadHandlersImplemented) {
    std::string canonical = std::to_string(kCapabilityRevision);
    canonical.push_back('|');
    canonical += cadHandlersImplemented ? "4" : "0";
    canonical.push_back('|');
    canonical += ImplementedActionsJson(cadHandlersImplemented);
    canonical.push_back('|');
    canonical += CapabilityDescriptorsJson(cadHandlersImplemented);
    canonical.push_back('|');
    canonical += CreateObjectTypesJson(cadHandlersImplemented);
    canonical.push_back('|');
    canonical += ObjectKindDescriptorsJson(cadHandlersImplemented);

    constexpr std::uint64_t kOffset = 14695981039346656037ull;
    constexpr std::uint64_t kPrime = 1099511628211ull;
    std::uint64_t hash = kOffset;
    for (const unsigned char value : canonical) {
        hash ^= static_cast<std::uint64_t>(value);
        hash *= kPrime;
    }
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << hash;
    return out.str();
}

std::string CapabilitiesResultJson(bool cadHandlersImplemented) {
    std::string json = "{\"capability_revision\":";
    json += std::to_string(kCapabilityRevision);
    json += ",\"capability_fingerprint\":";
    AppendJsonString(json, CapabilityFingerprint(cadHandlersImplemented));
    json += ",\"native_phase\":";
    json += cadHandlersImplemented ? "4" : "0";
    json += ",\"implemented_actions\":";
    json += ImplementedActionsJson(cadHandlersImplemented);
    json += ",\"descriptors\":";
    json += CapabilityDescriptorsJson(cadHandlersImplemented);
    json += ",\"create_object_types\":";
    json += CreateObjectTypesJson(cadHandlersImplemented);
    json += ",\"object_kind_descriptors\":";
    json += ObjectKindDescriptorsJson(cadHandlersImplemented);
    json.push_back('}');
    return json;
}

}  // namespace VectorworksMCP
