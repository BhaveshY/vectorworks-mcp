#pragma once

// 0.x handlers kept verbatim in behaviour (the 0.7 host still calls them),
// implemented in VectorworksMCPBridge.cpp. The 1.0 runtime (BridgeRuntime.cpp)
// calls them on the Vectorworks main thread after the UI gate, document
// pre-checks and expect_document_fingerprint validation.

#include "JsonParams.hpp"

#include <optional>
#include <string>

namespace VectorworksMCP::Legacy {

// Maximum operation_count of one apply_operations call (ping/capabilities
// `max_operations`).
constexpr int kMaxApplyOperationCount = 2000;

// Returns the JSON result of a 0.x action or std::nullopt when the action is
// not a legacy action. Throws on failure (std::invalid_argument for invalid
// input, ViewDocument::Error / NativeIO::Error / BridgeError / runtime_error).
std::optional<std::string> Dispatch(const std::string& action, const Params& params);

// Validates the 0.x documentation target binding (expected_document_*)
// when present in params.
void ValidateBindingIfPresent(const Params& params);

void ResetCaches();

}  // namespace VectorworksMCP::Legacy
