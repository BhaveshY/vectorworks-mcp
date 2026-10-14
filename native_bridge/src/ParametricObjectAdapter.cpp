#include "StdAfx.h"

#include "ParametricObjectAdapter.hpp"

#include "JsonParams.hpp"

#include "Interfaces/VectorWorks/Extension/IExtendedProps.h"
#include "Interfaces/VectorWorks/Extension/IExtensionParametric.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <functional>
#include <iomanip>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <unordered_set>
#include <utility>

namespace VectorworksMCP {
namespace {

using VWFC::VWObjects::VWParametricObj;
using VectorWorks::Extension::IParametricParams2Provider;
using VectorWorks::Extension::IParametricParamsProvider;
using VectorWorks::Extension::VCOMSinkPtr;

std::string Utf8(const TXString& value) {
    return value.GetStdString();
}

std::string Fingerprint(const ParametricDescriptor& descriptor) {
    constexpr std::uint64_t kOffset = 14695981039346656037ull;
    constexpr std::uint64_t kPrime = 1099511628211ull;
    std::uint64_t hash = kOffset;
    auto add = [&](const std::string& value) {
        for (const unsigned char ch : value) {
            hash ^= static_cast<std::uint64_t>(ch);
            hash *= kPrime;
        }
        hash ^= 0xffu;
        hash *= kPrime;
    };
    add(descriptor.universalPluginName);
    for (const auto& parameter : descriptor.parameters) {
        add(parameter.universalName);
        add(std::to_string(parameter.fieldStyle));
    }
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << hash;
    return out.str();
}

bool IsRealField(EFieldStyle style) {
    return style == kFieldReal || style == kFieldCoordDisp || style == kFieldCoordLocX ||
        style == kFieldCoordLocY || style == kFieldAngle || style == kFieldArea ||
        style == kFieldVolume;
}

bool IsStringField(EFieldStyle style) {
    return style == kFieldText || style == kFieldPopUp || style == kFieldRadio ||
        style == kFieldClassesPopup || style == kFieldLayersPopup;
}

void ValidateParameterKind(const ParametricValue& value, EFieldStyle actualStyle) {
    bool compatible = false;
    switch (value.kind) {
        case ParametricValueKind::Integer:
            compatible = actualStyle == kFieldLongInt;
            break;
        case ParametricValueKind::Boolean:
            compatible = actualStyle == kFieldBoolean;
            break;
        case ParametricValueKind::Real:
            compatible = IsRealField(actualStyle);
            break;
        case ParametricValueKind::String:
            compatible = IsStringField(actualStyle);
            break;
    }
    if (!compatible) {
        throw std::invalid_argument(
            "parameter type does not match SDK field style for universal parameter: " +
            value.universalName);
    }
}

void ValidateParameters(
    const VWParametricObj& object,
    const std::vector<ParametricValue>& values) {
    std::unordered_set<std::string> seen;
    for (const auto& value : values) {
        if (value.universalName.empty()) {
            throw std::invalid_argument("parametric parameter universalName is required");
        }
        if (!seen.insert(value.universalName).second) {
            throw std::invalid_argument("duplicate parametric parameter: " + value.universalName);
        }
        if (object.GetParamIndexByUnivName(TXString(value.universalName.c_str())) == static_cast<size_t>(-1)) {
            throw std::invalid_argument(
                "unknown universal parametric parameter: " + value.universalName);
        }
        const auto styleOwnership = gSDK->GetPluginStyleParameterType(
            object.GetThisObject(),
            TXString(value.universalName.c_str()));
        if (styleOwnership == kPluginStyleParameter_ByStyle ||
            styleOwnership == kPluginStyleParameter_ByCatalog) {
            throw std::invalid_argument(
                "parametric parameter is controlled by style or catalog: " + value.universalName);
        }
        ValidateParameterKind(value, object.GetParamStyle(TXString(value.universalName.c_str())));
        if (value.kind == ParametricValueKind::Real && !std::isfinite(value.realValue)) {
            throw std::invalid_argument("parametric real value must be finite: " + value.universalName);
        }
    }
}

void ApplyParameters(VWParametricObj& object, const std::vector<ParametricValue>& values) {
    for (const auto& value : values) {
        const TXString name(value.universalName.c_str());
        switch (value.kind) {
            case ParametricValueKind::Integer:
                object.SetParamLong(name, static_cast<Sint32>(value.integerValue));
                break;
            case ParametricValueKind::Boolean:
                object.SetParamBool(name, value.booleanValue);
                break;
            case ParametricValueKind::Real:
                object.SetParamReal(name, value.realValue);
                break;
            case ParametricValueKind::String:
                object.SetParamString(name, TXString(value.stringValue.c_str()));
                break;
        }
    }
}

// Leading number of a parameter string ("900", "900.0", "900mm", "0.9 m",
// "3'-0\"" -> 3): readback of numeric values stored in string/dimension fields
// is reformatted by Vectorworks, so string values that both parse as numbers
// are compared numerically (1.0 adds no unit conversion: the plug-in stores
// what was written; only its formatting changes).
bool LeadingNumber(const std::string& text, double& out) {
    std::size_t start = 0;
    while (start < text.size() && std::isspace(static_cast<unsigned char>(text[start]))) ++start;
    if (start == text.size()) return false;
    const char* begin = text.c_str() + start;
    char* end = nullptr;
    const double value = std::strtod(begin, &end);
    if (end == begin || !std::isfinite(value)) return false;
    out = value;
    return true;
}

bool NumbersClose(double left, double right) {
    const double scale = std::max({1.0, std::abs(left), std::abs(right)});
    return std::abs(left - right) <= 1e-6 * scale;
}

bool StringValuesMatch(const std::string& actual, const std::string& expected) {
    if (actual == expected) return true;
    const std::string a = TrimCopy(actual);
    const std::string e = TrimCopy(expected);
    if (a == e) return true;
    const std::string al = ToLower(a);
    const std::string el = ToLower(e);
    const auto truthy = [](const std::string& v) { return v == "true" || v == "1" || v == "yes"; };
    const auto falsy = [](const std::string& v) { return v == "false" || v == "0" || v == "no"; };
    if ((truthy(al) && truthy(el)) || (falsy(al) && falsy(el))) return true;
    double av = 0.0;
    double ev = 0.0;
    return LeadingNumber(a, av) && LeadingNumber(e, ev) && NumbersClose(av, ev);
}

void VerifyParameters(const VWParametricObj& object, const std::vector<ParametricValue>& values) {
    for (const auto& value : values) {
        const TXString name(value.universalName.c_str());
        bool matches = false;
        switch (value.kind) {
            case ParametricValueKind::Integer:
                matches = object.GetParamLong(name) == static_cast<Sint32>(value.integerValue);
                break;
            case ParametricValueKind::Boolean:
                matches = object.GetParamBool(name) == value.booleanValue;
                break;
            case ParametricValueKind::Real:
                matches = NumbersClose(object.GetParamReal(name), value.realValue);
                break;
            case ParametricValueKind::String:
                matches = StringValuesMatch(Utf8(object.GetParamString(name)), value.stringValue);
                break;
        }
        if (!matches) {
            std::string actual;
            try {
                switch (value.kind) {
                    case ParametricValueKind::Integer: actual = std::to_string(object.GetParamLong(name)); break;
                    case ParametricValueKind::Boolean: actual = object.GetParamBool(name) ? "true" : "false"; break;
                    case ParametricValueKind::Real: actual = JsonNumber(object.GetParamReal(name)); break;
                    case ParametricValueKind::String: actual = Utf8(object.GetParamString(name)); break;
                }
            } catch (...) {
            }
            throw std::runtime_error(
                "Vectorworks parametric readback mismatch: " + value.universalName +
                " (wrote " + (value.kind == ParametricValueKind::String ? value.stringValue
                    : value.kind == ParametricValueKind::Real ? JsonNumber(value.realValue)
                    : value.kind == ParametricValueKind::Integer ? std::to_string(value.integerValue)
                    : std::string(value.booleanValue ? "true" : "false")) +
                ", read back " + actual + "; check the parameter name/type with describe_parametric_schema)");
        }
    }
}

void RequireFingerprint(const ParametricDescriptor& descriptor, const std::string& expected) {
    if (expected.empty()) {
        throw std::invalid_argument(
            "descriptor fingerprint is required; discover the runtime schema before writing");
    }
    if (descriptor.descriptorFingerprint != expected) {
        throw std::invalid_argument(
            "parametric descriptor changed; refresh capability metadata before writing");
    }
}

std::string LocalizedParameterName(
    const std::string& universalPluginName,
    const TXString& universalParameterName,
    const TXString& providerLocalizedName) {
    TXString localizedName;
    if (gSDK->GetLocalizedPluginParameter(
            TXString(universalPluginName.c_str()),
            universalParameterName,
            localizedName) &&
        !localizedName.IsEmpty()) {
        return Utf8(localizedName);
    }
    return providerLocalizedName.IsEmpty()
        ? Utf8(universalParameterName)
        : Utf8(providerLocalizedName);
}

void AppendParameter(
    ParametricDescriptor& descriptor,
    std::unordered_set<std::string>& seen,
    const TXString& universalName,
    const TXString& localizedName,
    EFieldStyle fieldStyle,
    const TXString& defaultValue) {
    const std::string universal = Utf8(universalName);
    if (universal.empty()) {
        throw std::runtime_error(
            "installed parametric schema contains an empty universal parameter name");
    }
    if (!seen.insert(universal).second) {
        throw std::runtime_error(
            "installed parametric schema contains a duplicate universal parameter: " + universal);
    }
    descriptor.parameters.push_back({
        universal,
        LocalizedParameterName(
            descriptor.universalPluginName,
            universalName,
            localizedName),
        static_cast<short>(fieldStyle),
        Utf8(defaultValue),
    });
}

bool DescribeWithParams2Provider(ParametricDescriptor& descriptor) {
    const TXString pluginName(descriptor.universalPluginName.c_str());
    VCOMSinkPtr<IParametricParams2Provider> provider(
        pluginName,
        VectorWorks::Extension::IID_ParametricParams2Provider);
    if (!provider) {
        return false;
    }
    const size_t count = provider->GetParamsCount();
    descriptor.parameters.reserve(count);
    std::unordered_set<std::string> seen;
    for (size_t index = 0; index < count; ++index) {
        TXString universalName;
        EFieldStyle fieldStyle = kFieldText;
        provider->GetParamAt(index, universalName, fieldStyle);
        TRecordItem defaultValue(fieldStyle);
        provider->GetParamDefValueMetric(index, defaultValue);
        TXString defaultValueText;
        defaultValue.GetFieldValueAsString(defaultValueText);
        AppendParameter(
            descriptor,
            seen,
            universalName,
            provider->GetParamNameAt(index),
            fieldStyle,
            defaultValueText);
    }
    return true;
}

bool DescribeWithParamsProvider(ParametricDescriptor& descriptor) {
    const TXString pluginName(descriptor.universalPluginName.c_str());
    VCOMSinkPtr<IParametricParamsProvider> provider(
        pluginName,
        VectorWorks::Extension::IID_ParametricParamsProvider);
    if (!provider) {
        return false;
    }
    const size_t count = provider->GetParamsCount();
    descriptor.parameters.reserve(count);
    std::unordered_set<std::string> seen;
    for (size_t index = 0; index < count; ++index) {
        TXString universalName;
        EFieldStyle fieldStyle = kFieldText;
        TXString defaultValue;
        provider->GetParamAt(index, universalName, fieldStyle);
        provider->GetParamDefValueMetric(index, defaultValue);
        AppendParameter(
            descriptor,
            seen,
            universalName,
            provider->GetParamNameAt(index),
            fieldStyle,
            defaultValue);
    }
    return true;
}

const ParametricParameterDescriptor& ResolveUniqueSemanticParameter(
    const ParametricDescriptor& descriptor,
    const std::string& semanticName,
    const std::vector<std::string>& allowedUniversalNames) {
    const ParametricParameterDescriptor* match = nullptr;
    for (const auto& parameter : descriptor.parameters) {
        if (std::find(
                allowedUniversalNames.begin(),
                allowedUniversalNames.end(),
                parameter.universalName) == allowedUniversalNames.end()) {
            continue;
        }
        if (match) {
            throw std::runtime_error(
                "installed parametric schema is ambiguous for semantic parameter " +
                semanticName);
        }
        match = &parameter;
    }
    if (!match) {
        throw std::runtime_error(
            "installed parametric schema does not expose the required universal parameter for " +
            semanticName);
    }
    if (!IsRealField(static_cast<EFieldStyle>(match->fieldStyle))) {
        throw std::runtime_error(
            "installed parametric semantic parameter has an unsupported field style: " +
            match->universalName);
    }
    return *match;
}

void RequireInstanceControlled(
    const VWParametricObj& object,
    const std::string& universalName) {
    const EPluginStyleParameter ownership = gSDK->GetPluginStyleParameterType(
        object.GetThisObject(),
        TXString(universalName.c_str()));
    if (ownership == kPluginStyleParameter_ByStyle ||
        ownership == kPluginStyleParameter_ByCatalog) {
        throw std::runtime_error(
            "built-in opening parameter is controlled by style or catalog: " +
            universalName);
    }
    if (ownership != kPluginStyleParameter_ByInstance &&
        ownership != kPluginStyleParameter_AllwaysByInstance) {
        throw std::runtime_error(
            "built-in opening parameter has unsupported ownership: " + universalName);
    }
}

std::string HandleUuid(MCObjectHandle object) {
    TXString uuid;
    if (object && gSDK->GetObjectUuid(object, uuid)) {
        return uuid.GetStdString();
    }
    return std::string();
}

// Index of the symbol break of `wall` whose theSymbol is `object`, or -1.
Sint32 FindWallSymbolBreak(MCObjectHandle wall, MCObjectHandle object, double* offset = nullptr) {
    Sint32 found = -1;
    if (!wall || !object || gSDK->GetObjectTypeN(wall) != kWallNode) {
        return found;
    }
    gSDK->ForEachBreakN(
        wall,
        [&](MCObjectHandle, Sint32 index, WorldCoord off, Sint32 breakType, void* breakData) -> bool {
            if (breakType == kSymbolBreak && breakData &&
                static_cast<const SymbolBreakType*>(breakData)->theSymbol == object) {
                found = index;
                if (offset) {
                    *offset = off;
                }
                return true;  // stop
            }
            return false;
        });
    return found;
}

// Deleting an inserted object must also delete the wall break that
// references it (a dangling break would point at freed memory).
void DeleteInsertAndBreak(MCObjectHandle object, MCObjectHandle wall) {
    if (!object || !gSDK) {
        return;
    }
    if (wall && gSDK->GetObjectTypeN(wall) == kWallNode) {
        const Sint32 index = FindWallSymbolBreak(wall, object);
        if (index >= 0) {
            gSDK->DeleteBreak(wall, static_cast<short>(index));
        }
    }
    gSDK->DeleteObject(object, false);
}

// Vectorworks keeps the objects inserted in a wall in two places: the wall's
// member list AND a symbol break (kSymbolBreak, SymbolBreakType.theSymbol)
// that positions the insert along the wall. AddObjectToContainer only does the
// first; an insert without a break may be dropped when Vectorworks rebuilds the
// wall (class change, move, reset). Live history (VW 2024):
//  - before 714c15d: AddObjectToContainer only; creates succeeded, the
//    openings had no break and were dropped by later wall edits;
//  - 714c15d: AddObjectToContainer + ISDK::AddBreak registered NO break, and
//    the wall ResetObject added to prove survival made every door/window
//    create fail ("final object disappeared before commit");
//  - vs.AddSymToWall (Python) does register a break; the MCP host now creates
//    doors/windows that way.
// The native path therefore treats the break as best effort: it never fails a
// create because of break handling and never regenerates the wall itself.
struct WallBreakRegistration {
    bool registered = false;
    double offset = 0.0;
    std::string method;                 // how the break was registered (empty = none)
    std::vector<std::string> attempts;  // diagnostics, one entry per attempt
    MCObjectHandle object = nullptr;    // insert handle after the attempts
};

// Break data of an interactive centre insertion: full break with caps, the
// offset of (x, y) projected onto the wall axis (clamped to the wall),
// mirrored (rightSide + flipH = 180 degrees) only when the requested rotation
// is opposite to the wall direction.
struct SymbolBreakPlacement {
    bool valid = false;
    double along = 0.0;
    SymbolBreakType data;
};

SymbolBreakPlacement ComputeSymbolBreakPlacement(
    MCObjectHandle wall,
    double x,
    double y,
    double rotationDegrees) {
    SymbolBreakPlacement placement;
    WorldPt start;
    WorldPt end;
    gSDK->GetEndPoints(wall, start, end);
    const double dx = end.x - start.x;
    const double dy = end.y - start.y;
    const double length = std::hypot(dx, dy);
    if (!std::isfinite(length) || length <= 1e-6) {
        return placement;
    }
    double along = ((x - start.x) * dx + (y - start.y) * dy) / length;
    if (!std::isfinite(along)) {
        along = length / 2.0;
    }
    placement.along = std::clamp(along, 0.0, length);
    placement.data.insertMode = kSymInsertOnCenter;
    placement.data.breakMode = kSymFullBreakWithCaps;
    placement.data.height = 0.0;
    const double wallAngle = std::atan2(dy, dx) * 180.0 / 3.14159265358979323846;
    double relative = std::fmod(rotationDegrees - wallAngle, 360.0);
    if (relative > 180.0) relative -= 360.0;
    if (relative <= -180.0) relative += 360.0;
    if (std::abs(std::abs(relative) - 180.0) < 1.0) {
        placement.data.rightSide = true;
        placement.data.flipH = true;
    }
    placement.valid = true;
    return placement;
}

// (index, theSymbol) of every symbol break of `wall`.
std::vector<std::pair<Sint32, MCObjectHandle>> ListWallSymbolBreaks(MCObjectHandle wall) {
    std::vector<std::pair<Sint32, MCObjectHandle>> breaks;
    if (!wall || gSDK->GetObjectTypeN(wall) != kWallNode) {
        return breaks;
    }
    gSDK->ForEachBreakN(
        wall,
        [&](MCObjectHandle, Sint32 index, WorldCoord, Sint32 breakType, void* breakData) -> bool {
            if (breakType == kSymbolBreak) {
                breaks.emplace_back(
                    index,
                    breakData ? static_cast<const SymbolBreakType*>(breakData)->theSymbol : nullptr);
            }
            return false;
        });
    return breaks;
}

// Removes symbol breaks that appeared since `before` and do not reference a
// symbol that was already there (an AddBreak that registered something other
// than our insert must not leave a dangling break behind).
size_t RemoveNewSymbolBreaks(
    MCObjectHandle wall,
    const std::vector<std::pair<Sint32, MCObjectHandle>>& before) {
    std::vector<Sint32> stray;
    for (const auto& entry : ListWallSymbolBreaks(wall)) {
        const bool known = std::any_of(
            before.begin(), before.end(),
            [&](const std::pair<Sint32, MCObjectHandle>& old) { return old.second == entry.second; });
        if (!known) {
            stray.push_back(entry.first);
        }
    }
    std::sort(stray.begin(), stray.end(), std::greater<Sint32>());
    for (const Sint32 index : stray) {
        gSDK->DeleteBreak(wall, static_cast<short>(index));
    }
    return stray.size();
}

// Strict containment: the insert's container chain reaches the wall.
bool IsParentedByWall(MCObjectHandle object, MCObjectHandle wall) {
    MCObjectHandle current = object ? gSDK->ParentObject(object) : nullptr;
    for (int depth = 0; current && depth < 16; ++depth) {
        if (current == wall) {
            return true;
        }
        current = gSDK->ParentObject(current);
    }
    return false;
}

std::string JoinAttempts(const std::vector<std::string>& attempts) {
    std::string joined;
    for (const auto& attempt : attempts) {
        if (!joined.empty()) joined += "; ";
        joined += attempt;
    }
    return joined.empty() ? std::string("no attempt") : joined;
}

std::string NoWallBreakWarning(const std::string& what, const WallBreakRegistration& registration) {
    return "wallBreakRegistered=false: the " + what +
           " is in the wall's object list but has no wall break record (" +
           JoinAttempts(registration.attempts) +
           "); it may be dropped when Vectorworks regenerates the wall (class change, move, "
           "reset). The MCP host creates doors/windows with vs.AddSymToWall, which registers "
           "the break; use vw_create rather than raw apply_operations for durable openings";
}

class CreatedParametricGuard final {
public:
    explicit CreatedParametricGuard(MCObjectHandle object)
        : object_(object), uuid_(HandleUuid(object)) {}

    ~CreatedParametricGuard() {
        if (!object_ || !gSDK) {
            return;
        }
        // Re-resolve: a wall regeneration may have re-created or dropped the
        // insert, so the original handle must not be trusted.
        MCObjectHandle current = uuid_.empty()
            ? object_
            : gSDK->GetObjectByUuid(TXString(uuid_.c_str()));
        if (current) {
            DeleteInsertAndBreak(current, wall_);
        }
    }

    CreatedParametricGuard(const CreatedParametricGuard&) = delete;
    CreatedParametricGuard& operator=(const CreatedParametricGuard&) = delete;

    void SetWall(MCObjectHandle wall) noexcept {
        wall_ = wall;
    }

    void Release() noexcept {
        object_ = nullptr;
    }

private:
    MCObjectHandle object_ = nullptr;
    MCObjectHandle wall_ = nullptr;
    std::string uuid_;
};

// Moves a detached insert into `wall` and tries to register its symbol break:
// (a) ISDK::AddBreak while the insert is NOT yet in the wall (AddBreak does the
// insertion), (b) AddObjectToContainer when the insert is still not inside the
// wall (exactly the pre-714c15d behaviour), (c) with still no break,
// ISDK::SetObjectWallOffset. Throws only when the insert cannot be attached to
// the wall at all; a missing break is reported (registered=false + attempts),
// never thrown. The wall itself is never reset here.
WallBreakRegistration InsertIntoWallWithBreak(
    MCObjectHandle object,
    const std::string& uuid,
    MCObjectHandle wall,
    double x,
    double y,
    double rotationDegrees,
    CreatedParametricGuard& guard,
    const std::string& what) {
    WallBreakRegistration result;
    result.object = object;
    // From here a failure removes the insert together with any break of it.
    guard.SetWall(wall);
    auto resolve = [&]() {
        if (!uuid.empty()) {
            if (MCObjectHandle current = gSDK->GetObjectByUuid(TXString(uuid.c_str()))) {
                result.object = current;
            }
        }
    };
    auto probe = [&](const char* method) -> bool {
        double offset = 0.0;
        if (FindWallSymbolBreak(wall, result.object, &offset) >= 0) {
            result.registered = true;
            result.offset = offset;
            result.method = method;
            return true;
        }
        return false;
    };

    const SymbolBreakPlacement placement =
        ComputeSymbolBreakPlacement(wall, x, y, rotationDegrees);
    if (!placement.valid) {
        result.attempts.push_back("no break attempted: the wall has no measurable axis");
    }

    // (a) AddBreak with the insert still detached.
    if (placement.valid && !IsParentedByWall(result.object, wall)) {
        const auto before = ListWallSymbolBreaks(wall);
        SymbolBreakType data = placement.data;
        data.theSymbol = result.object;
        const short rc = gSDK->AddBreak(wall, placement.along, kSymbolBreak, &data);
        resolve();
        const bool found = probe("add_break_before_attach");
        const bool parented = IsParentedByWall(result.object, wall);
        std::string note = "AddBreak on the detached insert returned " + std::to_string(rc) +
                           (found ? ", break registered" : ", no break") +
                           (parented ? ", insert now in the wall" : ", insert not in the wall");
        if (!found) {
            const size_t removed = RemoveNewSymbolBreaks(wall, before);
            if (removed > 0) {
                note += ", removed " + std::to_string(removed) + " unmatched new break(s)";
            }
        }
        result.attempts.push_back(note);
    }

    // (b) The pre-714c15d attachment.
    if (!IsParentedByWall(result.object, wall)) {
        if (!gSDK->AddObjectToContainer(result.object, wall)) {
            throw std::runtime_error(
                "Vectorworks did not attach the " + what + " to the exact requested wall");
        }
        resolve();
        if (!result.registered && probe("add_object_to_container")) {
            result.attempts.push_back("AddObjectToContainer registered a break");
        }
    }

    // (c) Ask the wall to position the insert (expected to need an existing
    // break; tried because it is the only other SDK call that addresses an
    // insert by wall offset).
    if (!result.registered && placement.valid) {
        const bool ok = gSDK->SetObjectWallOffset(result.object, wall, placement.along);
        const bool found = probe("set_object_wall_offset");
        result.attempts.push_back(
            std::string("SetObjectWallOffset returned ") + (ok ? "true" : "false") +
            (found ? ", break registered" : ", no break"));
    }
    if (!result.registered) {
        WorldCoord offset = 0.0;
        const bool known = gSDK->GetObjectWallOffset(result.object, wall, offset);
        result.attempts.push_back(
            std::string("GetObjectWallOffset ") + (known ? "true" : "false") +
            ", wall symbol breaks: " + std::to_string(ListWallSymbolBreaks(wall).size()));
    }
    return result;
}

// Non-destructive break readback (no wall ResetObject) after the insert's own
// parameter reset.
void RecheckWallBreak(
    WallBreakRegistration& registration,
    MCObjectHandle wall,
    MCObjectHandle object) {
    double offset = 0.0;
    if (FindWallSymbolBreak(wall, object, &offset) >= 0) {
        if (!registration.registered) {
            registration.method = "after_insert_reset";
        }
        registration.registered = true;
        registration.offset = offset;
    } else if (registration.registered) {
        registration.attempts.push_back(
            "the break registered by " + registration.method +
            " was gone after the insert's parameter reset");
        registration.registered = false;
        registration.method.clear();
    }
}

}  // namespace

ParametricDescriptor DescribeParametricObject(MCObjectHandle object) {
    if (!object || gSDK->GetObjectTypeN(object) != kParametricNode) {
        throw std::invalid_argument("object is not a parametric object");
    }
    VWParametricObj parametric(object);
    ParametricDescriptor descriptor;
    descriptor.universalPluginName = Utf8(parametric.GetParametricName());
    descriptor.localizedPluginName = Utf8(parametric.GetLocalizedParametricName());
    const size_t count = parametric.GetParamsCount();
    descriptor.parameters.reserve(count);
    for (size_t index = 0; index < count; ++index) {
        const TXString universalName = parametric.GetParamName(index);
        descriptor.parameters.push_back({
            Utf8(universalName),
            Utf8(parametric.GetParamLocalizedName(index)),
            static_cast<short>(parametric.GetParamStyle(universalName)),
            Utf8(parametric.GetParamAsString(universalName)),
        });
    }
    descriptor.descriptorFingerprint = Fingerprint(descriptor);
    return descriptor;
}

ParametricDescriptor DescribeParametricDefinition(const std::string& universalPluginName) {
    if (universalPluginName.empty()) {
        throw std::invalid_argument("universal plugin name is required");
    }
    if (!gSDK) {
        throw std::runtime_error("Vectorworks SDK is unavailable");
    }
    const TXString pluginName(universalPluginName.c_str());
    ParametricDescriptor descriptor;
    descriptor.universalPluginName = universalPluginName;
    TXString localizedPluginName;
    if (gSDK->GetLocalizedPluginName(pluginName, localizedPluginName) &&
        !localizedPluginName.IsEmpty()) {
        descriptor.localizedPluginName = Utf8(localizedPluginName);
    } else {
        descriptor.localizedPluginName = universalPluginName;
    }
    if (DescribeWithParams2Provider(descriptor) ||
        DescribeWithParamsProvider(descriptor)) {
        descriptor.descriptorFingerprint = Fingerprint(descriptor);
        return descriptor;
    }

    // Bundled Vectorworks extension objects (notably Door and Window in 2024)
    // can expose their parameter provider even when GetPluginType reports a
    // false negative. The provider is therefore authoritative; this registry
    // check only improves the error when no provider exists.
    EVSPluginType pluginType = kVSPluginMenu;
    if (!gSDK->GetPluginType(pluginName, pluginType) || pluginType != kVSPluginObject) {
        throw std::invalid_argument(
            "installed parametric object plugin was not found by universal name: " +
            universalPluginName);
    }
    throw std::runtime_error(
        "installed parametric plugin does not expose an SDK parameter schema provider: " +
        universalPluginName);
}

const char* BuiltInParametricUniversalName(BuiltInParametricKind kind) noexcept {
    switch (kind) {
        case BuiltInParametricKind::Door: return "Door";
        case BuiltInParametricKind::Window: return "Window";
    }
    return "";
}

ParametricDescriptor DescribeBuiltInParametricDefinition(BuiltInParametricKind kind) {
    return DescribeParametricDefinition(BuiltInParametricUniversalName(kind));
}

BuiltInOpeningSemanticIds ResolveBuiltInOpeningSemanticIds(
    BuiltInParametricKind kind,
    const ParametricDescriptor& descriptor) {
    const std::string expectedPlugin = BuiltInParametricUniversalName(kind);
    if (expectedPlugin.empty() || descriptor.universalPluginName != expectedPlugin) {
        throw std::invalid_argument(
            "built-in opening descriptor does not match the requested universal plugin");
    }

    BuiltInOpeningSemanticIds ids;
    ids.widthUniversalName = ResolveUniqueSemanticParameter(
        descriptor,
        "opening width",
        {"Width"}).universalName;
    ids.heightUniversalName = ResolveUniqueSemanticParameter(
        descriptor,
        "opening height",
        {"Height"}).universalName;
    if (kind == BuiltInParametricKind::Window) {
        ids.windowSillUniversalName = ResolveUniqueSemanticParameter(
            descriptor,
            "window sill elevation",
            {"Elevation"}).universalName;
    }
    return ids;
}

BuiltInOpeningReceipt CreateVerifiedBuiltInOpening(
    const BuiltInOpeningCreateSpec& spec) {
    if (!gSDK) {
        throw std::runtime_error("Vectorworks SDK is unavailable");
    }
    if (!spec.expectedWall || gSDK->GetObjectTypeN(spec.expectedWall) != kWallNode) {
        throw std::invalid_argument("built-in opening requires an exact wall handle");
    }
    if (!std::isfinite(spec.x) || !std::isfinite(spec.y) ||
        !std::isfinite(spec.rotationDegrees) || !std::isfinite(spec.width) ||
        !std::isfinite(spec.height) || !std::isfinite(spec.windowSillHeight)) {
        throw std::invalid_argument("built-in opening placement and dimensions must be finite");
    }
    if (spec.width <= 0.0 || spec.height <= 0.0) {
        throw std::invalid_argument("built-in opening width and height must be positive");
    }
    if (spec.kind == BuiltInParametricKind::Window && spec.windowSillHeight < 0.0) {
        throw std::invalid_argument("window sill height must be non-negative");
    }

    const ParametricDescriptor schema = DescribeBuiltInParametricDefinition(spec.kind);
    RequireFingerprint(schema, spec.descriptorFingerprint);
    const BuiltInOpeningSemanticIds ids = ResolveBuiltInOpeningSemanticIds(spec.kind, schema);
    const std::string pluginName = BuiltInParametricUniversalName(spec.kind);
    // Match VWFC::VWObjects::VWParametricObj's SDK creation sequence.  Built-in
    // objects such as Door and Window otherwise honor their "show preferences"
    // extended property and block this non-modal bridge on a preferences dialog.
    gSDK->DefineCustomObject(
        TXString(pluginName.c_str()),
        kCustomObjectPrefNever);
    MCObjectHandle object = gSDK->CreateCustomObject(
        TXString(pluginName.c_str()),
        WorldPt(spec.x, spec.y),
        spec.rotationDegrees,
        false);
    if (!object || gSDK->GetObjectTypeN(object) != kParametricNode) {
        throw std::runtime_error(
            "Vectorworks did not create the requested built-in opening object");
    }
    CreatedParametricGuard guard(object);
    const std::string objectUuid = HandleUuid(object);
    WallBreakRegistration wallBreak = InsertIntoWallWithBreak(
        object, objectUuid, spec.expectedWall, spec.x, spec.y, spec.rotationDegrees,
        guard, "built-in opening");
    object = wallBreak.object;
    // Built-in Door/Window parametrics become wall-hosted when moved into the
    // exact wall container.  SetObjectWallInsertMode is intended for symbol
    // definitions/instances and returns false for these built-in parametrics in
    // Vectorworks 2024 even after the attachment succeeds.  The strict parent
    // check below is the authoritative hosting proof.
    VWParametricObj parametric(object);
    if (Utf8(parametric.GetParametricName()) != pluginName) {
        throw std::runtime_error(
            "created built-in opening has the wrong universal plugin name");
    }
    if (!IsObjectHostedByWall(object, spec.expectedWall)) {
        throw std::runtime_error(
            "Vectorworks did not insert the built-in opening into the exact requested wall");
    }

    RequireInstanceControlled(parametric, ids.widthUniversalName);
    RequireInstanceControlled(parametric, ids.heightUniversalName);
    if (!ids.windowSillUniversalName.empty()) {
        RequireInstanceControlled(parametric, ids.windowSillUniversalName);
    }
    parametric.SetParamReal(TXString(ids.widthUniversalName.c_str()), spec.width);
    parametric.SetParamReal(TXString(ids.heightUniversalName.c_str()), spec.height);
    std::string elevationNote;
    if (!ids.windowSillUniversalName.empty()) {
        // The Window's "Elevation" is the sill only while "Elevation Set At"
        // is "Sill of Window" (the default "Head of Window" put the head at
        // the sill height, i.e. the window below the floor; live VW 2024).
        const bool hasSetAt = std::any_of(
            schema.parameters.begin(), schema.parameters.end(),
            [](const ParametricParameterDescriptor& parameter) {
                return parameter.universalName == "ElevationSetAt";
            });
        if (hasSetAt) {
            const TXString setAtName("ElevationSetAt");
            const TXString previous = parametric.GetParamString(setAtName);
            const TXString wanted("Sill of Window");
            if (previous != wanted) {
                parametric.SetParamString(setAtName, wanted);
                if (parametric.GetParamString(setAtName) != wanted) {
                    parametric.SetParamString(setAtName, previous);
                    elevationNote = "Vectorworks rejected ElevationSetAt='Sill of Window'; the window elevation is "
                                    "measured as set by '" + previous.GetStdString() + "'";
                }
            }
        }
        parametric.SetParamReal(
            TXString(ids.windowSillUniversalName.c_str()),
            spec.windowSillHeight);
    }
    gSDK->ResetObject(object);

    constexpr double kTolerance = 1e-7;
    const double actualWidth = parametric.GetParamReal(TXString(ids.widthUniversalName.c_str()));
    const double actualHeight = parametric.GetParamReal(TXString(ids.heightUniversalName.c_str()));
    const double actualSill = ids.windowSillUniversalName.empty()
        ? 0.0
        : parametric.GetParamReal(TXString(ids.windowSillUniversalName.c_str()));
    (void) kTolerance;
    if (!NumbersClose(actualWidth, spec.width) ||
        !NumbersClose(actualHeight, spec.height) ||
        (!ids.windowSillUniversalName.empty() &&
         !NumbersClose(actualSill, spec.windowSillHeight))) {
        throw std::runtime_error("built-in opening semantic dimension readback mismatch");
    }
    if (!IsObjectHostedByWall(object, spec.expectedWall)) {
        throw std::runtime_error(
            "built-in opening lost its exact wall host after parameter reset");
    }
    const ParametricDescriptor actualDescriptor = DescribeParametricObject(object);
    if (actualDescriptor.universalPluginName != pluginName ||
        actualDescriptor.descriptorFingerprint != schema.descriptorFingerprint) {
        throw std::runtime_error(
            "built-in opening schema changed between discovery and semantic readback");
    }

    // Break readback only. The wall is deliberately NOT reset here: in 714c15d
    // a ResetObject(wall) made every create fail when no break registered
    // (the regenerated wall dropped the insert before commit).
    RecheckWallBreak(wallBreak, spec.expectedWall, object);

    BuiltInOpeningReceipt receipt;
    receipt.object = object;
    receipt.wall = spec.expectedWall;
    receipt.wallBreakRegistered = wallBreak.registered;
    receipt.wallOffset = wallBreak.offset;
    receipt.wallBreakMethod = wallBreak.method;
    if (!wallBreak.registered) {
        receipt.warnings.push_back(NoWallBreakWarning(
            spec.kind == BuiltInParametricKind::Window ? "window" : "door", wallBreak));
    }
    if (!elevationNote.empty()) {
        receipt.warnings.push_back(elevationNote);
    }
    receipt.kind = spec.kind;
    receipt.universalPluginName = pluginName;
    receipt.descriptorFingerprint = schema.descriptorFingerprint;
    receipt.semanticIds = ids;
    receipt.width = actualWidth;
    receipt.height = actualHeight;
    receipt.windowSillHeight = actualSill;
    receipt.wallHostVerified = true;
    receipt.semanticReadbackVerified = true;
    guard.Release();
    return receipt;
}

void VerifyBuiltInOpeningReceipt(
    MCObjectHandle object,
    const BuiltInOpeningReceipt& receipt) {
    if (!gSDK || !object || object != receipt.object ||
        gSDK->GetObjectTypeN(object) != kParametricNode) {
        throw std::runtime_error(
            "built-in opening commit verifier received the wrong object handle or node type");
    }
    if (!receipt.wall || gSDK->GetObjectTypeN(receipt.wall) != kWallNode ||
        !IsObjectHostedByWall(object, receipt.wall)) {
        throw std::runtime_error(
            "built-in opening commit verifier could not confirm the exact wall host");
    }
    // The wall break is best effort (reported at create time) and is never a
    // commit failure.
    const std::string expectedPlugin = BuiltInParametricUniversalName(receipt.kind);
    if (expectedPlugin.empty() || receipt.universalPluginName != expectedPlugin ||
        receipt.descriptorFingerprint.empty()) {
        throw std::runtime_error(
            "built-in opening commit receipt has invalid semantic identity");
    }

    const ParametricDescriptor descriptor = DescribeParametricObject(object);
    if (descriptor.universalPluginName != expectedPlugin ||
        descriptor.descriptorFingerprint != receipt.descriptorFingerprint) {
        throw std::runtime_error(
            "built-in opening commit verifier detected plugin or schema drift");
    }
    const BuiltInOpeningSemanticIds actualIds = ResolveBuiltInOpeningSemanticIds(
        receipt.kind,
        descriptor);
    if (actualIds.widthUniversalName != receipt.semanticIds.widthUniversalName ||
        actualIds.heightUniversalName != receipt.semanticIds.heightUniversalName ||
        actualIds.windowSillUniversalName != receipt.semanticIds.windowSillUniversalName) {
        throw std::runtime_error(
            "built-in opening commit verifier detected semantic parameter identifier drift");
    }

    VWParametricObj parametric(object);
    RequireInstanceControlled(parametric, actualIds.widthUniversalName);
    RequireInstanceControlled(parametric, actualIds.heightUniversalName);
    if (!actualIds.windowSillUniversalName.empty()) {
        RequireInstanceControlled(parametric, actualIds.windowSillUniversalName);
    }
    const double actualWidth = parametric.GetParamReal(
        TXString(actualIds.widthUniversalName.c_str()));
    const double actualHeight = parametric.GetParamReal(
        TXString(actualIds.heightUniversalName.c_str()));
    const double actualSill = actualIds.windowSillUniversalName.empty()
        ? 0.0
        : parametric.GetParamReal(TXString(actualIds.windowSillUniversalName.c_str()));
    constexpr double kTolerance = 1e-7;
    if (!std::isfinite(actualWidth) || !std::isfinite(actualHeight) ||
        !std::isfinite(actualSill) ||
        std::abs(actualWidth - receipt.width) > kTolerance ||
        std::abs(actualHeight - receipt.height) > kTolerance ||
        std::abs(actualSill - receipt.windowSillHeight) > kTolerance) {
        throw std::runtime_error(
            "built-in opening commit verifier detected semantic dimension drift");
    }
}

bool IsObjectHostedByWall(MCObjectHandle object, MCObjectHandle expectedWall) {
    if (!object || !expectedWall || gSDK->GetObjectTypeN(expectedWall) != kWallNode) {
        return false;
    }
    MCObjectHandle current = object;
    for (int depth = 0; current && depth < 16; ++depth) {
        if (current == expectedWall) {
            return true;
        }
        current = gSDK->ParentObject(current);
    }
    InsertModeType mode = 0;
    return gSDK->GetObjectWallInsertMode(object, expectedWall, mode);
}

bool HasWallSymbolBreak(MCObjectHandle wall, MCObjectHandle object, double* offset) {
    return FindWallSymbolBreak(wall, object, offset) >= 0;
}

MCObjectHandle CreateVerifiedParametricObject(
    const ParametricCreateSpec& spec,
    std::vector<std::string>* warnings) {
    if (spec.universalPluginName.empty()) {
        throw std::invalid_argument("universal plugin name is required");
    }
    if (!std::isfinite(spec.x) || !std::isfinite(spec.y) ||
        !std::isfinite(spec.rotationDegrees)) {
        throw std::invalid_argument("parametric placement values must be finite");
    }
    if (spec.requireWallHost &&
        (!spec.expectedWall || gSDK->GetObjectTypeN(spec.expectedWall) != kWallNode)) {
        throw std::invalid_argument("a valid exact wall reference is required");
    }

    RequireFingerprint(DescribeParametricDefinition(spec.universalPluginName), spec.descriptorFingerprint);
    // Define the instance with preferences disabled before creation, as the
    // official VWParametricObj constructors do for programmatic objects.
    gSDK->DefineCustomObject(
        TXString(spec.universalPluginName.c_str()),
        kCustomObjectPrefNever);
    const WorldPt location(spec.x, spec.y);
    // Generic point parametrics must be inserted into the active document.
    // Hosted parametrics are created detached and then moved into the exact
    // requested wall container, matching the dedicated Door/Window path.
    const bool insertOnActiveLayer = !spec.requireWallHost;
    MCObjectHandle object = gSDK->CreateCustomObject(
        TXString(spec.universalPluginName.c_str()),
        location,
        spec.rotationDegrees,
        insertOnActiveLayer);
    if (!object || gSDK->GetObjectTypeN(object) != kParametricNode) {
        throw std::runtime_error(
            "Vectorworks did not create the requested parametric object: " +
            spec.universalPluginName);
    }

    VWParametricObj parametric(object);
    if (Utf8(parametric.GetParametricName()) != spec.universalPluginName) {
        throw std::runtime_error("created parametric object has the wrong universal plugin name");
    }
    // Hosted objects are detached until attached; a failure below must not
    // leave a half-inserted object (or a dangling wall break) behind.
    std::optional<CreatedParametricGuard> hostedGuard;
    WallBreakRegistration wallBreak;
    if (spec.requireWallHost) {
        hostedGuard.emplace(object);
        wallBreak = InsertIntoWallWithBreak(
            object, HandleUuid(object), spec.expectedWall, spec.x, spec.y,
            spec.rotationDegrees, *hostedGuard, "parametric object");
        object = wallBreak.object;
    }
    VWParametricObj target(object);
    ValidateParameters(target, spec.parameters);
    ApplyParameters(target, spec.parameters);
    gSDK->ResetObject(object);
    VerifyParameters(target, spec.parameters);
    if (spec.requireWallHost) {
        if (!IsObjectHostedByWall(object, spec.expectedWall)) {
            throw std::runtime_error(
                "Vectorworks did not insert the parametric object into the required wall");
        }
        // Readback only; the wall is not reset here (see CreateVerifiedBuiltInOpening).
        RecheckWallBreak(wallBreak, spec.expectedWall, object);
        if (!wallBreak.registered && warnings) {
            warnings->push_back(NoWallBreakWarning("hosted parametric object", wallBreak));
        }
        hostedGuard->Release();
    }
    return object;
}

void UpdateVerifiedParametricObject(
    MCObjectHandle object,
    const std::string& expectedUniversalPluginName,
    const std::string& descriptorFingerprint,
    const std::vector<ParametricValue>& parameters) {
    const ParametricDescriptor descriptor = DescribeParametricObject(object);
    if (!expectedUniversalPluginName.empty() &&
        descriptor.universalPluginName != expectedUniversalPluginName) {
        throw std::invalid_argument("target parametric object has the wrong plugin type");
    }
    RequireFingerprint(descriptor, descriptorFingerprint);
    VWParametricObj parametric(object);
    ValidateParameters(parametric, parameters);
    ApplyParameters(parametric, parameters);
    gSDK->ResetObject(object);
    VerifyParameters(parametric, parameters);
}

}  // namespace VectorworksMCP
