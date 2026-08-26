#include "StdAfx.h"

#include "Interfaces/VectorWorks/Extension/ISpaceObjectSupport.h"
#include "SpaceObjectHandlers.hpp"

#include <cmath>
#include <stdexcept>

namespace VectorworksMCP {
namespace {

using VectorWorks::Extension::IID_VCOMSpace;
using VectorWorks::Extension::ISpaceObjectSupport;

class CreatedObjectGuard {
public:
    explicit CreatedObjectGuard(MCObjectHandle object) : object_(object) {}
    ~CreatedObjectGuard() {
        if (object_) {
            gSDK->DeleteObject(object_, false);
        }
    }
    void Release() { object_ = nullptr; }
    CreatedObjectGuard(const CreatedObjectGuard&) = delete;
    CreatedObjectGuard& operator=(const CreatedObjectGuard&) = delete;

private:
    MCObjectHandle object_;
};

double Cross(const SpacePoint& a, const SpacePoint& b, const SpacePoint& c) {
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}

bool OnSegment(const SpacePoint& a, const SpacePoint& b, const SpacePoint& p) {
    constexpr double kEpsilon = 1e-9;
    return std::abs(Cross(a, b, p)) <= kEpsilon &&
        p.x >= std::min(a.x, b.x) - kEpsilon && p.x <= std::max(a.x, b.x) + kEpsilon &&
        p.y >= std::min(a.y, b.y) - kEpsilon && p.y <= std::max(a.y, b.y) + kEpsilon;
}

bool SegmentsIntersect(
    const SpacePoint& a,
    const SpacePoint& b,
    const SpacePoint& c,
    const SpacePoint& d) {
    const double abC = Cross(a, b, c);
    const double abD = Cross(a, b, d);
    const double cdA = Cross(c, d, a);
    const double cdB = Cross(c, d, b);
    if (((abC > 0.0 && abD < 0.0) || (abC < 0.0 && abD > 0.0)) &&
        ((cdA > 0.0 && cdB < 0.0) || (cdA < 0.0 && cdB > 0.0))) {
        return true;
    }
    return OnSegment(a, b, c) || OnSegment(a, b, d) ||
        OnSegment(c, d, a) || OnSegment(c, d, b);
}

double ValidateBoundary(const std::vector<SpacePoint>& points) {
    if (points.size() < 3u) {
        throw std::invalid_argument("space boundary requires at least three points");
    }
    if (points.size() > 4096u) {
        throw std::invalid_argument("space boundary exceeds 4096 points");
    }
    for (const auto& point : points) {
        if (!std::isfinite(point.x) || !std::isfinite(point.y)) {
            throw std::invalid_argument("space boundary coordinates must be finite");
        }
    }
    for (size_t index = 0; index < points.size(); ++index) {
        const size_t next = (index + 1u) % points.size();
        if (points[index].x == points[next].x && points[index].y == points[next].y) {
            throw std::invalid_argument("space boundary contains a zero-length edge");
        }
        for (size_t other = index + 1u; other < points.size(); ++other) {
            const size_t otherNext = (other + 1u) % points.size();
            if (index == other || next == other || index == otherNext) {
                continue;
            }
            if (SegmentsIntersect(points[index], points[next], points[other], points[otherNext])) {
                throw std::invalid_argument("space boundary self-intersects");
            }
        }
    }
    double twiceArea = 0.0;
    for (size_t index = 0; index < points.size(); ++index) {
        const auto& current = points[index];
        const auto& next = points[(index + 1u) % points.size()];
        twiceArea += current.x * next.y - next.x * current.y;
    }
    const double area = std::abs(twiceArea) * 0.5;
    if (!(area > 1e-9)) {
        throw std::invalid_argument("space boundary area must be positive");
    }
    return area;
}

MCObjectHandle CreateProfile(const std::vector<SpacePoint>& points) {
    MCObjectHandle profile = gSDK->CreatePolyshape();
    if (!profile) {
        throw std::runtime_error("Vectorworks could not create the Space boundary profile");
    }
    for (const auto& point : points) {
        gSDK->AddVertex(profile, WorldPt(point.x, point.y), nullptr, vtCorner, 0, false);
    }
    gSDK->SetPolyShapeClose(profile, true);
    gSDK->ResetObject(profile);
    return profile;
}

VWFC::Math::VWPolygon2D RequestedBoundaryPolygon(const std::vector<SpacePoint>& points) {
    VWFC::Math::VWPolygon2D polygon;
    for (const auto& point : points) {
        polygon.AddVertex(point.x, point.y);
    }
    polygon.SetClosed(true);
    return polygon;
}

bool ReturnedBoundaryMatches(
    MCObjectHandle returnedPolygon,
    const VWFC::Math::VWPolygon2D& expected) {
    if (!returnedPolygon) {
        return false;
    }
    if (!VWFC::VWObjects::VWPolygon2DObj::IsPoly2DObject(returnedPolygon)) {
        return false;
    }
    VWFC::Math::VWPolygon2D actual;
    VWFC::VWObjects::VWPolygon2DObj(returnedPolygon).GetPolygon(actual);
    if (!actual.IsClosed()) {
        return false;
    }
    if (actual.GetVertexCount() > 1u &&
        actual.GetVertexAt(0u) == actual.GetVertexAt(actual.GetVertexCount() - 1u)) {
        actual.PopBack();
    }
    return expected.EqualishTo(actual, true);
}

bool SpaceBoundaryMatches(
    ISpaceObjectSupport& support,
    MCObjectHandle space,
    const std::vector<SpacePoint>& requested) {
    MCObjectHandle netPolygon = nullptr;
    MCObjectHandle grossPolygon = nullptr;
    const bool netRead = support.NetPoly(space, netPolygon, false);
    const bool grossRead = support.GrossPoly(space, grossPolygon);
    // The SDK may assign an owned polygon handle even when it reports false.
    // Guard every non-null handle exactly once before interpreting status.
    CreatedObjectGuard netGuard(netPolygon);
    CreatedObjectGuard grossGuard(grossPolygon != netPolygon ? grossPolygon : nullptr);
    const bool hasNet = netRead && netPolygon;
    const bool hasGross = grossRead && grossPolygon;
    const auto expected = RequestedBoundaryPolygon(requested);

    const bool netMatches = hasNet && ReturnedBoundaryMatches(netPolygon, expected);
    const bool grossMatches = hasGross && grossPolygon != netPolygon
        ? ReturnedBoundaryMatches(grossPolygon, expected)
        : hasGross && netMatches;
    return netMatches || grossMatches;
}

struct VerifiedSpaceReadback {
    double netArea = 0.0;
    double grossArea = 0.0;
};

VerifiedSpaceReadback VerifySpaceSemantics(
    MCObjectHandle space,
    const std::vector<SpacePoint>& boundary,
    const std::string& requestedName,
    const std::string& requestedRoomId) {
    if (!space || gSDK->GetObjectTypeN(space) != kParametricNode) {
        throw std::runtime_error("Vectorworks Space is no longer a true parametric object");
    }
    VCOMPtr<ISpaceObjectSupport> support(IID_VCOMSpace);
    if (!support) {
        throw std::runtime_error("Space capability became unavailable during semantic verification");
    }

    VWFC::VWObjects::VWParametricObj parametric(space);
    bool nameVerified = requestedName.empty();
    bool roomIdVerified = requestedRoomId.empty();
    for (size_t index = 0; index < parametric.GetParamsCount(); ++index) {
        const TXString universalName = parametric.GetParamName(index);
        const std::string value = parametric.GetParamAsString(universalName).GetStdString();
        nameVerified = nameVerified || value == requestedName;
        roomIdVerified = roomIdVerified || value == requestedRoomId;
    }
    if (!nameVerified) {
        throw std::runtime_error("Vectorworks Space name readback does not match the request");
    }
    if (!roomIdVerified) {
        throw std::runtime_error("Vectorworks Space room ID readback does not match the request");
    }

    WorldCoord netArea = 0.0;
    WorldCoord grossArea = 0.0;
    if (!support->NetArea(space, netArea) || !support->GrossArea(space, grossArea) ||
        !(netArea > 0.0) || !(grossArea > 0.0)) {
        throw std::runtime_error("Vectorworks Space semantic area readback failed");
    }
    if (!SpaceBoundaryMatches(*support, space, boundary)) {
        throw std::runtime_error("Vectorworks Space boundary readback does not match the request");
    }

    // Boundary readback returns temporary SDK polygons and their cleanup can
    // regenerate the Space. Verify the semantic identity one more time.
    WorldCoord verifiedNetArea = 0.0;
    WorldCoord verifiedGrossArea = 0.0;
    if (gSDK->GetObjectTypeN(space) != kParametricNode ||
        !support->NetArea(space, verifiedNetArea) ||
        !support->GrossArea(space, verifiedGrossArea) ||
        !(verifiedNetArea > 0.0) || !(verifiedGrossArea > 0.0)) {
        throw std::runtime_error("Vectorworks Space failed verification after boundary readback cleanup");
    }
    return {
        static_cast<double>(verifiedNetArea),
        static_cast<double>(verifiedGrossArea),
    };
}

}  // namespace

SpaceCreateReceipt CreateVerifiedSpace(
    const SpaceCreateSpec& spec,
    Transactions::NativeTransaction& transaction) {
    ValidateBoundary(spec.boundary);
    if (!std::isfinite(spec.height) || spec.height <= 0.0) {
        throw std::invalid_argument("space height must be a positive finite number");
    }
    VCOMPtr<ISpaceObjectSupport> support(IID_VCOMSpace);
    if (!support) {
        throw std::runtime_error("Space capability is unavailable in this Vectorworks product or license");
    }

    MCObjectHandle profile = CreateProfile(spec.boundary);
    CreatedObjectGuard profileGuard(profile);
    transaction.AdoptTemporary(profile, Transactions::ObjectFamily::Space);
    profileGuard.Release();

    MCObjectHandle space = support->Create(profile, spec.height);
    if (!space) {
        throw std::runtime_error(
            "Vectorworks Space support returned no object for the validated polygon profile");
    }
    const short createdNodeType = gSDK->GetObjectTypeN(space);
    if (createdNodeType != kParametricNode) {
        throw std::runtime_error(
            "Vectorworks Space support returned node type " +
            std::to_string(createdNodeType) +
            " instead of the required parametric Space node type " +
            std::to_string(kParametricNode));
    }
    CreatedObjectGuard spaceGuard(space);
    const auto artifact = transaction.AdoptFinal(
        space,
        Transactions::ObjectFamily::Space,
        kParametricNode,
        [boundary = spec.boundary, name = spec.name, roomId = spec.roomId](MCObjectHandle object) {
            (void) VerifySpaceSemantics(object, boundary, name, roomId);
        });
    // From this point onward the transaction owns the exact Space UUID. Any
    // later failure is rolled back only by the transaction coordinator.
    spaceGuard.Release();
    if (!spec.name.empty() && !support->AddName(space, TXString(spec.name.c_str()))) {
        throw std::runtime_error("Vectorworks rejected the Space name");
    }
    if (!spec.roomId.empty() && !support->AddRoomID(space, TXString(spec.roomId.c_str()))) {
        throw std::runtime_error("Vectorworks rejected the Space room ID");
    }
    gSDK->ResetObject(space);

    const VerifiedSpaceReadback verified =
        VerifySpaceSemantics(space, spec.boundary, spec.name, spec.roomId);
    SpaceCreateReceipt receipt = {
        space,
        transaction.Uuid(artifact),
        verified.netArea,
        verified.grossArea,
        gSDK->GetObjectTypeN(space),
        spec.name,
        spec.roomId,
        artifact,
    };
    return receipt;
}

}  // namespace VectorworksMCP
