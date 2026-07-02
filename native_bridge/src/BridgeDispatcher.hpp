#pragma once

#include <array>
#include <string_view>

namespace VectorworksMCP {

enum class ExecutionContext {
    TransportThread,
    VectorworksMainPluginContext,
};

struct ActionSpec {
    std::string_view action;
    ExecutionContext context;
    bool mayWriteDocument;
    bool destructive;
};

constexpr std::array<ActionSpec, 2> kPhaseZeroActions = {{
    {"ping", ExecutionContext::TransportThread, false, false},
    {"stop", ExecutionContext::TransportThread, true, false},
}};

constexpr std::array<ActionSpec, 6> kPhaseOneActions = {{
    {"get_document_info", ExecutionContext::VectorworksMainPluginContext, false, false},
    {"get_layers", ExecutionContext::VectorworksMainPluginContext, false, false},
    {"get_objects", ExecutionContext::VectorworksMainPluginContext, false, false},
    {"selection", ExecutionContext::VectorworksMainPluginContext, true, true},
    {"create_object", ExecutionContext::VectorworksMainPluginContext, true, false},
    {"batch_create_objects", ExecutionContext::VectorworksMainPluginContext, true, false},
}};

constexpr std::array<ActionSpec, 5> kPhaseTwoActions = {{
    {"create_wall", ExecutionContext::VectorworksMainPluginContext, true, false},
    {"create_text", ExecutionContext::VectorworksMainPluginContext, true, false},
    {"create_linear_dimension", ExecutionContext::VectorworksMainPluginContext, true, false},
    {"set_property", ExecutionContext::VectorworksMainPluginContext, true, false},
    {"manage_classes", ExecutionContext::VectorworksMainPluginContext, true, false},
}};

constexpr std::array<ActionSpec, 2> kPhaseThreeActions = {{
    {"find_objects", ExecutionContext::VectorworksMainPluginContext, false, false},
    {"drawing_summary", ExecutionContext::VectorworksMainPluginContext, false, false},
}};

inline bool RequiresCadMainContext(std::string_view action) {
    for (const auto& spec : kPhaseOneActions) {
        if (spec.action == action) {
            return true;
        }
    }
    for (const auto& spec : kPhaseTwoActions) {
        if (spec.action == action) {
            return true;
        }
    }
    for (const auto& spec : kPhaseThreeActions) {
        if (spec.action == action) {
            return true;
        }
    }
    return false;
}

inline const ActionSpec* FindActionSpec(std::string_view action) {
    for (const auto& spec : kPhaseZeroActions) {
        if (spec.action == action) {
            return &spec;
        }
    }
    for (const auto& spec : kPhaseOneActions) {
        if (spec.action == action) {
            return &spec;
        }
    }
    for (const auto& spec : kPhaseTwoActions) {
        if (spec.action == action) {
            return &spec;
        }
    }
    for (const auto& spec : kPhaseThreeActions) {
        if (spec.action == action) {
            return &spec;
        }
    }
    return nullptr;
}

}  // namespace VectorworksMCP
