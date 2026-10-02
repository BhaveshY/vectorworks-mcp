#pragma once

// Organisation (contract 1.9): layers, stories and class extensions.

#include "JsonParams.hpp"

#include <string>

namespace VectorworksMCP::Organization {

std::string HandleLayers(const Params& params);
std::string HandleStories(const Params& params);
// manage_classes extensions: action=set, and list with detailed:true.
// Returns empty string when the action is not handled here (legacy path).
std::string HandleClassesExtension(const Params& params);

}  // namespace VectorworksMCP::Organization
