#include "StdAfx.h"

#include "NativeObjectFactory.hpp"

#include <cmath>
#include <stdexcept>

namespace VectorworksMCP {

namespace {

class CreatedSymbolGuard {
public:
    explicit CreatedSymbolGuard(MCObjectHandle object) : object_(object) {}
    ~CreatedSymbolGuard() {
        if (object_ && gSDK) {
            gSDK->DeleteObject(object_, false);
        }
    }
    CreatedSymbolGuard(const CreatedSymbolGuard&) = delete;
    CreatedSymbolGuard& operator=(const CreatedSymbolGuard&) = delete;
    void Release() noexcept { object_ = nullptr; }

private:
    MCObjectHandle object_ = nullptr;
};

MCObjectHandle RequireWritableLayer() {
    MCObjectHandle layer = gSDK->GetActiveLayer();
    if (!layer) {
        layer = gSDK->GetCurrentLayer();
    }
    if (!layer) {
        throw std::runtime_error(
            "active Vectorworks document has no writable layer for symbol placement");
    }
    return layer;
}

void EnsureSymbolIsInDocument(MCObjectHandle instance) {
    if (gSDK->ParentObject(instance)) {
        return;
    }
    MCObjectHandle layer = RequireWritableLayer();
    if (!gSDK->AddObjectToContainer(instance, layer) ||
        gSDK->ParentObject(instance) != layer) {
        throw std::runtime_error(
            "Vectorworks created a detached symbol instance that could not be inserted on the active layer");
    }
}

}  // namespace

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
    MCObjectHandle instance = gSDK->PlaceSymbolN(
        definition,
        WorldPt(spec.x, spec.y),
        spec.rotationDegrees);
    CreatedSymbolGuard guard(instance);
    if (!instance || gSDK->GetObjectTypeN(instance) != kSymbolNode) {
        throw std::runtime_error("Vectorworks did not create a true symbol instance");
    }
    EnsureSymbolIsInDocument(instance);
    if (gSDK->GetDefinition(instance) != definition) {
        throw std::runtime_error("Vectorworks symbol instance lost its exact resolved definition");
    }
    guard.Release();
    return instance;
}

}  // namespace VectorworksMCP
