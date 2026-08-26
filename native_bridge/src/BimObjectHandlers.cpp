#include "StdAfx.h"

#include "BimObjectHandlers.hpp"
#include "ParametricObjectAdapter.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <string>
#include <utility>

namespace VectorworksMCP::BimObjects {
namespace {

constexpr std::size_t kMinProfileVertices = 3;
constexpr std::size_t kMaxProfileVertices = 1024;
constexpr long double kGeometryEpsilon = 1.0e-9L;
constexpr double kMaxRoofSlopeDegrees = 89.0;
constexpr const char* kSlabUniversalPluginName = "Slab";

class CreatedObjectGuard {
public:
    CreatedObjectGuard(VectorWorks::ISDK& sdk, MCObjectHandle handle)
        : sdk_(sdk), handle_(handle) {}

    ~CreatedObjectGuard() {
        if (handle_) {
            sdk_.DeleteObject(handle_, false);
        }
    }

    CreatedObjectGuard(const CreatedObjectGuard&) = delete;
    CreatedObjectGuard& operator=(const CreatedObjectGuard&) = delete;

    void Release() {
        handle_ = nullptr;
    }

private:
    VectorWorks::ISDK& sdk_;
    MCObjectHandle handle_;
};

bool IsTrueSlabObject(VectorWorks::ISDK& sdk, MCObjectHandle object) {
    if (!object) {
        return false;
    }
    const short nodeType = sdk.GetObjectTypeN(object);
    if (nodeType == kSlabNode) {
        return true;
    }
    if (nodeType != kParametricNode) {
        return false;
    }
    return DescribeParametricObject(object).universalPluginName == kSlabUniversalPluginName;
}

bool SamePoint(const ProfilePoint& left, const ProfilePoint& right) {
    return left.x == right.x && left.y == right.y;
}

long double Cross(
    const ProfilePoint& start,
    const ProfilePoint& end,
    const ProfilePoint& point) {
    return
        (static_cast<long double>(end.x) - static_cast<long double>(start.x)) *
            (static_cast<long double>(point.y) - static_cast<long double>(start.y)) -
        (static_cast<long double>(end.y) - static_cast<long double>(start.y)) *
            (static_cast<long double>(point.x) - static_cast<long double>(start.x));
}

bool OnSegment(
    const ProfilePoint& start,
    const ProfilePoint& end,
    const ProfilePoint& point) {
    if (std::abs(Cross(start, end, point)) > kGeometryEpsilon) {
        return false;
    }
    return point.x >= std::min(start.x, end.x) &&
           point.x <= std::max(start.x, end.x) &&
           point.y >= std::min(start.y, end.y) &&
           point.y <= std::max(start.y, end.y);
}

int Orientation(
    const ProfilePoint& start,
    const ProfilePoint& end,
    const ProfilePoint& point) {
    const long double value = Cross(start, end, point);
    if (value > kGeometryEpsilon) {
        return 1;
    }
    if (value < -kGeometryEpsilon) {
        return -1;
    }
    return 0;
}

bool SegmentsIntersect(
    const ProfilePoint& aStart,
    const ProfilePoint& aEnd,
    const ProfilePoint& bStart,
    const ProfilePoint& bEnd) {
    const int a = Orientation(aStart, aEnd, bStart);
    const int b = Orientation(aStart, aEnd, bEnd);
    const int c = Orientation(bStart, bEnd, aStart);
    const int d = Orientation(bStart, bEnd, aEnd);

    if (a != b && c != d) {
        return true;
    }
    return (a == 0 && OnSegment(aStart, aEnd, bStart)) ||
           (b == 0 && OnSegment(aStart, aEnd, bEnd)) ||
           (c == 0 && OnSegment(bStart, bEnd, aStart)) ||
           (d == 0 && OnSegment(bStart, bEnd, aEnd));
}

bool EdgesAreAdjacent(std::size_t left, std::size_t right, std::size_t count) {
    return left == right ||
           (left + 1) % count == right ||
           (right + 1) % count == left;
}

std::pair<std::vector<ProfilePoint>, bool> ValidateAndNormalizeProfile(
    const std::vector<ProfilePoint>& boundary,
    const char* label) {
    if (boundary.size() < kMinProfileVertices || boundary.size() > kMaxProfileVertices) {
        throw std::invalid_argument(
            std::string(label) + " boundary must contain between 3 and 1024 vertices");
    }

    for (std::size_t index = 0; index < boundary.size(); ++index) {
        const ProfilePoint& point = boundary[index];
        if (!std::isfinite(point.x) || !std::isfinite(point.y)) {
            throw std::invalid_argument(
                std::string(label) + " boundary contains a non-finite coordinate");
        }
        const ProfilePoint& next = boundary[(index + 1) % boundary.size()];
        if (SamePoint(point, next)) {
            throw std::invalid_argument(
                std::string(label) + " boundary contains a zero-length edge");
        }
    }

    for (std::size_t left = 0; left < boundary.size(); ++left) {
        const std::size_t leftEnd = (left + 1) % boundary.size();
        for (std::size_t right = left + 1; right < boundary.size(); ++right) {
            if (EdgesAreAdjacent(left, right, boundary.size())) {
                continue;
            }
            const std::size_t rightEnd = (right + 1) % boundary.size();
            if (SegmentsIntersect(
                    boundary[left],
                    boundary[leftEnd],
                    boundary[right],
                    boundary[rightEnd])) {
                throw std::invalid_argument(
                    std::string(label) + " boundary must be a simple non-self-intersecting polygon");
            }
        }
    }

    long double signedDoubleArea = 0.0L;
    for (std::size_t index = 0; index < boundary.size(); ++index) {
        const ProfilePoint& point = boundary[index];
        const ProfilePoint& next = boundary[(index + 1) % boundary.size()];
        signedDoubleArea +=
            static_cast<long double>(point.x) * static_cast<long double>(next.y) -
            static_cast<long double>(next.x) * static_cast<long double>(point.y);
    }
    if (!std::isfinite(signedDoubleArea) || std::abs(signedDoubleArea) <= kGeometryEpsilon) {
        throw std::invalid_argument(std::string(label) + " boundary area must be non-zero");
    }

    std::vector<ProfilePoint> normalized = boundary;
    const bool reversed = signedDoubleArea < 0.0L;
    if (reversed) {
        std::reverse(normalized.begin(), normalized.end());
    }
    return {std::move(normalized), reversed};
}

void RequireFinite(double value, const char* field) {
    if (!std::isfinite(value)) {
        throw std::invalid_argument(std::string(field) + " must be finite");
    }
}

MCObjectHandle CreateClosedProfile(
    VectorWorks::ISDK& sdk,
    const std::vector<ProfilePoint>& boundary) {
    MCObjectHandle profile = sdk.CreatePolyshape();
    if (!profile) {
        throw std::runtime_error("Vectorworks did not create the BIM profile polygon");
    }

    CreatedObjectGuard profileGuard(sdk, profile);
    for (const ProfilePoint& point : boundary) {
        sdk.AddVertex(profile, WorldPt(point.x, point.y), nullptr, vtCorner, 0, false);
    }
    sdk.SetPolyShapeClose(profile, true);
    sdk.ResetObject(profile);
    profileGuard.Release();
    return profile;
}

CreationReceipt VerifiedReceipt(
    VectorWorks::ISDK& sdk,
    MCObjectHandle object,
    short expectedNodeType,
    SemanticNodeKind semanticKind,
    std::size_t vertexCount,
    bool inputWindingReversed) {
    if (!object) {
        throw std::runtime_error("Vectorworks did not return a BIM object handle");
    }
    const short actualNodeType = sdk.GetObjectTypeN(object);
    if (actualNodeType != expectedNodeType) {
        throw std::runtime_error(
            "Vectorworks returned node type " + std::to_string(actualNodeType) +
            " instead of the required BIM node type " + std::to_string(expectedNodeType));
    }
    return {object, actualNodeType, semanticKind, vertexCount, inputWindingReversed};
}

std::pair<double, double> VerifySlabSemantics(
    VectorWorks::ISDK& sdk,
    MCObjectHandle slab,
    const SlabRequest& request) {
    if (!IsTrueSlabObject(sdk, slab)) {
        throw std::runtime_error("Vectorworks slab is no longer a true Slab object");
    }
    short componentCount = 0;
    if (!sdk.GetNumberOfComponents(slab, componentCount) || componentCount <= 0) {
        throw std::runtime_error("Vectorworks could not verify slab components");
    }
    double verifiedThickness = 0.0;
    // Vectorworks SDK component collections are zero-based.
    for (short component = 0; component < componentCount; ++component) {
        WorldCoord width = 0.0;
        if (!sdk.GetComponentWidth(slab, component, width)) {
            throw std::runtime_error("Vectorworks could not read back slab component thickness");
        }
        verifiedThickness += static_cast<double>(width);
    }
    const double thicknessTolerance = std::max(1e-7, request.thickness * 1e-7);
    if (request.styleName.empty() &&
        std::abs(verifiedThickness - request.thickness) > thicknessTolerance) {
        throw std::runtime_error("Vectorworks slab thickness readback does not match the request");
    }
    const double verifiedElevation = static_cast<double>(sdk.GetSlabHeight(slab));
    const double elevationTolerance = std::max(1e-7, std::abs(request.elevation) * 1e-7);
    if (std::abs(verifiedElevation - request.elevation) > elevationTolerance) {
        throw std::runtime_error("Vectorworks slab elevation readback does not match the request");
    }
    return {verifiedThickness, verifiedElevation};
}

void VerifyRoofSemantics(
    VectorWorks::ISDK& sdk,
    MCObjectHandle roof,
    const std::vector<ProfilePoint>& requestedBoundary) {
    if (!roof || sdk.GetObjectTypeN(roof) != kRoofContainerNode) {
        throw std::runtime_error("Vectorworks roof is no longer a true Roof container");
    }
    WorldRect bounds;
    sdk.GetObjectBounds(roof, bounds);
    const double actualMinX = std::min(bounds.Left(), bounds.Right());
    const double actualMaxX = std::max(bounds.Left(), bounds.Right());
    const double actualMinY = std::min(bounds.Top(), bounds.Bottom());
    const double actualMaxY = std::max(bounds.Top(), bounds.Bottom());
    if (!std::isfinite(actualMinX) || !std::isfinite(actualMaxX) ||
        !std::isfinite(actualMinY) || !std::isfinite(actualMaxY) ||
        !(actualMaxX > actualMinX) || !(actualMaxY > actualMinY)) {
        throw std::runtime_error("Vectorworks roof bounds readback is empty or invalid");
    }
    double requestedMinX = requestedBoundary.front().x;
    double requestedMaxX = requestedBoundary.front().x;
    double requestedMinY = requestedBoundary.front().y;
    double requestedMaxY = requestedBoundary.front().y;
    for (const ProfilePoint& point : requestedBoundary) {
        requestedMinX = std::min(requestedMinX, point.x);
        requestedMaxX = std::max(requestedMaxX, point.x);
        requestedMinY = std::min(requestedMinY, point.y);
        requestedMaxY = std::max(requestedMaxY, point.y);
    }
    const double scale = std::max({
        1.0,
        std::abs(requestedMinX),
        std::abs(requestedMaxX),
        std::abs(requestedMinY),
        std::abs(requestedMaxY),
    });
    const double tolerance = scale * 1e-7;
    if (actualMinX > requestedMinX + tolerance ||
        actualMaxX < requestedMaxX - tolerance ||
        actualMinY > requestedMinY + tolerance ||
        actualMaxY < requestedMaxY - tolerance) {
        throw std::runtime_error("Vectorworks roof bounds do not contain the requested boundary");
    }
}

}  // namespace

CreationReceipt CreateTrueSlab(
    VectorWorks::ISDK& sdk,
    const SlabRequest& request,
    Transactions::NativeTransaction& transaction) {
    RequireFinite(request.elevation, "slab elevation");
    RequireFinite(request.thickness, "slab thickness");
    if (request.thickness <= 0.0) {
        throw std::invalid_argument("slab thickness must be positive");
    }
    auto [boundary, reversed] = ValidateAndNormalizeProfile(request.boundary, "slab");

    MCObjectHandle profile = CreateClosedProfile(sdk, boundary);
    CreatedObjectGuard profileGuard(sdk, profile);
    transaction.AdoptTemporary(profile, Transactions::ObjectFamily::Slab);
    profileGuard.Release();

    MCObjectHandle slab = sdk.CreateSlab(profile);
    if (!slab) {
        throw std::runtime_error("Vectorworks CreateSlab rejected the validated polygon profile");
    }
    CreatedObjectGuard slabGuard(sdk, slab);
    const short createdNodeType = sdk.GetObjectTypeN(slab);
    if (!IsTrueSlabObject(sdk, slab)) {
        std::string parametricIdentity;
        if (createdNodeType == kParametricNode) {
            parametricIdentity =
                ", universal plugin " + DescribeParametricObject(slab).universalPluginName;
        }
        throw std::runtime_error(
            "Vectorworks CreateSlab returned node type " + std::to_string(createdNodeType) +
            parametricIdentity + " instead of a genuine Slab object");
    }
    const auto artifact = transaction.AdoptFinal(
        slab,
        Transactions::ObjectFamily::Slab,
        createdNodeType,
        [&sdk, request](MCObjectHandle object) {
            (void) VerifySlabSemantics(sdk, object, request);
        });
    slabGuard.Release();

    if (!request.styleName.empty()) {
        MCObjectHandle style = sdk.GetNamedObject(TXString(request.styleName.c_str()));
        if (!style || sdk.GetObjectTypeN(style) != kSlabStyleNode) {
            throw std::invalid_argument("unique slab style was not found: " + request.styleName);
        }
        InternalIndex styleIndex = 0;
        if (!sdk.NameToInternalIndexN(TXString(request.styleName.c_str()), styleIndex) || styleIndex == 0) {
            throw std::runtime_error("Vectorworks could not resolve the slab style index");
        }
        sdk.SetSlabStyle(slab, styleIndex);
    } else {
        sdk.ConvertToUnstyledSlab(slab);
        short componentCount = 0;
        if (!sdk.GetNumberOfComponents(slab, componentCount) || componentCount <= 0) {
            throw std::runtime_error("Vectorworks did not expose editable unstyled slab components");
        }
        if (componentCount == 1 && !sdk.SetComponentWidth(slab, 0, request.thickness)) {
            throw std::runtime_error("Vectorworks rejected the unstyled slab thickness");
        }
    }
    CreationReceipt receipt = VerifiedReceipt(
        sdk,
        slab,
        createdNodeType,
        SemanticNodeKind::Slab,
        boundary.size(),
        reversed);
    sdk.SetSlabHeight(slab, request.elevation);
    sdk.ResetObject(slab);
    if (sdk.GetObjectTypeN(slab) != receipt.actualNodeType) {
        throw std::runtime_error("Vectorworks slab changed node type during regeneration");
    }
    const auto verified = VerifySlabSemantics(sdk, slab, request);
    receipt.verifiedThickness = verified.first;
    receipt.verifiedElevation = verified.second;
    receipt.uuid = transaction.Uuid(artifact);
    receipt.transactionArtifact = artifact;
    return receipt;
}

CreationReceipt CreateTrueRoof(
    VectorWorks::ISDK& sdk,
    const RoofRequest& request,
    Transactions::NativeTransaction& transaction) {
    RequireFinite(request.slopeDegrees, "roof slopeDegrees");
    RequireFinite(request.projection, "roof projection");
    RequireFinite(request.eaveHeight, "roof eaveHeight");
    RequireFinite(request.bearingInset, "roof bearingInset");
    RequireFinite(request.thickness, "roof thickness");
    RequireFinite(request.verticalMiter, "roof verticalMiter");
    if (request.slopeDegrees <= 0.0 || request.slopeDegrees > kMaxRoofSlopeDegrees) {
        throw std::invalid_argument("roof slopeDegrees must be greater than 0 and no greater than 89");
    }
    if (request.projection < 0.0 || request.bearingInset < 0.0 ||
        request.thickness <= 0.0 || request.verticalMiter < 0.0) {
        throw std::invalid_argument(
            "roof projection, bearingInset, and verticalMiter must be non-negative and thickness must be positive");
    }
    if (request.miterType < 1 || request.miterType > 4) {
        throw std::invalid_argument("roof miterType must be one of 1, 2, 3, or 4");
    }

    auto [boundary, reversed] = ValidateAndNormalizeProfile(request.boundary, "roof");
    MCObjectHandle roof = sdk.CreateRoof(
        request.generateGableWalls,
        request.bearingInset,
        request.thickness,
        request.miterType,
        request.verticalMiter);
    if (!roof) {
        throw std::runtime_error("Vectorworks CreateRoof did not return a roof container");
    }

    CreatedObjectGuard roofGuard(sdk, roof);
    if (sdk.GetObjectTypeN(roof) != kRoofContainerNode) {
        throw std::runtime_error("Vectorworks did not create a true Roof container");
    }
    const auto artifact = transaction.AdoptFinal(
        roof,
        Transactions::ObjectFamily::Roof,
        kRoofContainerNode,
        [&sdk, boundary](MCObjectHandle object) {
            VerifyRoofSemantics(sdk, object, boundary);
        });
    roofGuard.Release();

    for (const ProfilePoint& point : boundary) {
        if (!sdk.AppendRoofEdge(
                roof,
                WorldPt(point.x, point.y),
                request.slopeDegrees,
                request.projection,
                request.eaveHeight)) {
            throw std::runtime_error("Vectorworks rejected a roof edge while building the roof boundary");
        }
    }
    sdk.ResetObject(roof);
    CreationReceipt receipt = VerifiedReceipt(
        sdk,
        roof,
        kRoofContainerNode,
        SemanticNodeKind::Roof,
        boundary.size(),
        reversed);
    VerifyRoofSemantics(sdk, roof, boundary);
    receipt.uuid = transaction.Uuid(artifact);
    receipt.transactionArtifact = artifact;
    return receipt;
}

}  // namespace VectorworksMCP::BimObjects
