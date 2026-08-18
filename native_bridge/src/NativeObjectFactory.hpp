#pragma once

#include "ParametricObjectAdapter.hpp"

#include <string>

namespace VectorworksMCP {

struct SymbolPlacementSpec {
    std::string definitionName;
    double x = 0.0;
    double y = 0.0;
    double rotationDegrees = 0.0;
};

MCObjectHandle PlaceVerifiedSymbol(const SymbolPlacementSpec& spec);

}  // namespace VectorworksMCP
