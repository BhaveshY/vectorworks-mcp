#include "StdAfx.h"

#include "NativeObjectFactory.hpp"

#include <cmath>
#include <stdexcept>

namespace VectorworksMCP {

MCObjectHandle PlaceVerifiedSymbol(const SymbolPlacementSpec& spec) {
    if (spec.definitionName.empty()) {
        throw std::invalid_argument("symbol definition name is required");
    }
    if (!std::isfinite(spec.x) || !std::isfinite(spec.y) ||
        !std::isfinite(spec.rotationDegrees)) {
        throw std::invalid_argument("symbol placement values must be finite");
    }
    MCObjectHandle definition = gSDK->GetNamedObject(TXString(spec.definitionName.c_str()));
    if (!definition || gSDK->GetObjectTypeN(definition) != kSymDefNode) {
        throw std::invalid_argument("unique symbol definition was not found: " + spec.definitionName);
    }
    MCObjectHandle instance = gSDK->PlaceSymbolByNameN(
        TXString(spec.definitionName.c_str()),
        WorldPt(spec.x, spec.y),
        spec.rotationDegrees);
    if (!instance || gSDK->GetObjectTypeN(instance) != kSymbolNode) {
        throw std::runtime_error("Vectorworks did not create a true symbol instance");
    }
    return instance;
}

}  // namespace VectorworksMCP
