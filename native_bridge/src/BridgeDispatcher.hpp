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

constexpr std::array<ActionSpec, 5> kPhaseOneActions = {{
    {"get_document_info", ExecutionContext::VectorworksMainPluginContext, false, false},
    {"get_layers", ExecutionContext::VectorworksMainPluginContext, false, false},
    {"get_objects", ExecutionContext::VectorworksMainPluginContext, false, false},
    {"selection", ExecutionContext::VectorworksMainPluginContext, true, true},
    {"create_object", ExecutionContext::VectorworksMainPluginContext, true, false},
}};

inline bool RequiresCadMainContext(std::string_view action) {
    for (const auto& spec : kPhaseOneActions) {
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
    return nullptr;
}

}  // namespace VectorworksMCP
