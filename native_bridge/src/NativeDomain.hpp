#pragma once

#include <cstdint>
#include <string_view>

namespace VectorworksMCP {

enum class ExecutionContext {
    TransportThread,
    VectorworksMainPluginContext,
};

struct ActionSpec {
    std::string_view action;
    ExecutionContext context;
    std::uint8_t nativePhase;
    bool mayWriteDocument;
    bool destructive;
};

struct ObjectKindSpec {
    std::string_view objectKind;
    std::string_view canonicalObjectKind;
    std::string_view semanticNodeType;
    std::string_view availability;
    std::string_view inputSchemaJson;
    std::string_view verifier;
};

}  // namespace VectorworksMCP
