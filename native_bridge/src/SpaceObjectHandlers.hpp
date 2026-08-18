#pragma once

#include "NativeTransaction.hpp"

#include <cstddef>
#include <string>
#include <vector>

namespace VectorworksMCP {

struct SpacePoint {
    double x = 0.0;
    double y = 0.0;
};

struct SpaceCreateSpec {
    std::vector<SpacePoint> boundary;
    double height = 0.0;
    std::string name;
    std::string roomId;
};

struct SpaceCreateReceipt {
    MCObjectHandle object = nullptr;
    std::string uuid;
    double netArea = 0.0;
    double grossArea = 0.0;
    short nativeObjectType = 0;
    std::string verifiedName;
    std::string verifiedRoomId;
    Transactions::ArtifactId transactionArtifact = 0;
};

// The caller owns the enclosing undo event. This handler either creates and
// verifies a true Space object through ISpaceObjectSupport or throws.
SpaceCreateReceipt CreateVerifiedSpace(
    const SpaceCreateSpec& spec,
    Transactions::NativeTransaction& transaction);

}  // namespace VectorworksMCP
