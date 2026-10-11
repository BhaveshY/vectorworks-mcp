#pragma once

// Objects inserted into walls (doors, windows, symbols, other plug-in
// objects) live in the wall's own member list, not in the layer list, so a
// plain FirstMemberObj(layer)/NextObject walk (and ForEachObjectN with
// descendIntoAll) never sees them. Every object enumeration and UUID/name
// resolution in the bridge goes through these helpers so wall inserts are
// listed, counted, resolvable and reported with parent_uuid / host:"wall".
//
// Include after StdAfx.h (needs gSDK and the node type constants).

#include <string>
#include <type_traits>
#include <vector>

namespace VectorworksMCP::WallInserts {

inline bool IsWall(MCObjectHandle object) {
    return object && gSDK->GetObjectTypeN(object) == kWallNode;
}

inline bool IsInsertType(short type) {
    return type == kSymbolNode || type == kParametricNode;
}

// Calls fn(insert) for every symbol / plug-in object inserted in `wall`.
template <class Fn>
void ForEachInsert(MCObjectHandle wall, Fn&& fn) {
    if (!IsWall(wall)) return;
    for (MCObjectHandle member = gSDK->FirstMemberObj(wall);
         member && gSDK->GetObjectTypeN(member) != kTermNode;
         member = gSDK->NextObject(member)) {
        if (IsInsertType(gSDK->GetObjectTypeN(member))) fn(member);
    }
}

// Calls fn(object) for each top-level object of `layer`, and right after each
// wall, fn(insert) for that wall's inserts. fn may return void or bool; a
// bool false stops the walk.
template <class Fn>
bool ForEachLayerObject(MCObjectHandle layer, Fn&& fn) {
    for (MCObjectHandle object = gSDK->FirstMemberObj(layer);
         object && gSDK->GetObjectTypeN(object) != kTermNode;
         object = gSDK->NextObject(object)) {
        if constexpr (std::is_same_v<decltype(fn(object)), bool>) {
            if (!fn(object)) return false;
            bool keepGoing = true;
            if (IsWall(object)) {
                for (MCObjectHandle member = gSDK->FirstMemberObj(object);
                     keepGoing && member && gSDK->GetObjectTypeN(member) != kTermNode;
                     member = gSDK->NextObject(member)) {
                    if (IsInsertType(gSDK->GetObjectTypeN(member))) keepGoing = fn(member);
                }
            }
            if (!keepGoing) return false;
        } else {
            fn(object);
            ForEachInsert(object, fn);
        }
    }
    return true;
}

// The host wall of an insert, or nullptr.
inline MCObjectHandle HostWall(MCObjectHandle object) {
    if (!object) return nullptr;
    MCObjectHandle parent = gSDK->ParentObject(object);
    return IsWall(parent) ? parent : nullptr;
}

inline std::string UuidOf(MCObjectHandle object) {
    TXString uuid;
    if (object && gSDK->GetObjectUuid(object, uuid)) return uuid.GetStdString();
    return std::string();
}

// UUIDs of the inserts of `wall` (deleted with it).
inline std::vector<std::string> InsertUuids(MCObjectHandle wall) {
    std::vector<std::string> uuids;
    ForEachInsert(wall, [&](MCObjectHandle insert) {
        const std::string uuid = UuidOf(insert);
        if (!uuid.empty()) uuids.push_back(uuid);
    });
    return uuids;
}

}  // namespace VectorworksMCP::WallInserts
