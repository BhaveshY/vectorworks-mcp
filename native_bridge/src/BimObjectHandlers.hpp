#pragma once

#include "VectorworksSDK.h"
#include "NativeTransaction.hpp"

#include <cstddef>
#include <string>
#include <vector>

namespace VectorworksMCP::BimObjects {

struct ProfilePoint {
    double x = 0.0;
    double y = 0.0;
};

struct SlabRequest {
    std::vector<ProfilePoint> boundary;
    double elevation = 0.0;
    double thickness = 200.0;
    std::string styleName;
};

struct RoofRequest {
    std::vector<ProfilePoint> boundary;
    double slopeDegrees = 30.0;
    double projection = 0.0;
    double eaveHeight = 0.0;
    double bearingInset = 0.0;
    double thickness = 200.0;
    short miterType = 1;
    double verticalMiter = 0.0;
    bool generateGableWalls = false;
};

enum class SemanticNodeKind {
    Slab,
    Roof,
};

struct CreationReceipt {
    MCObjectHandle handle = nullptr;
    short actualNodeType = 0;
    SemanticNodeKind semanticKind = SemanticNodeKind::Slab;
    std::size_t vertexCount = 0;
    bool inputWindingReversed = false;
    double verifiedThickness = 0.0;
    double verifiedElevation = 0.0;
    std::string uuid;
    Transactions::ArtifactId transactionArtifact = 0;
};

CreationReceipt CreateTrueSlab(
    VectorWorks::ISDK& sdk,
    const SlabRequest& request,
    Transactions::NativeTransaction& transaction);
CreationReceipt CreateTrueRoof(
    VectorWorks::ISDK& sdk,
    const RoofRequest& request,
    Transactions::NativeTransaction& transaction);

}  // namespace VectorworksMCP::BimObjects
