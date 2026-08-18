#include "StdAfx.h"

#include "ParametricObjectAdapter.hpp"

#include "Interfaces/VectorWorks/Extension/IExtendedProps.h"
#include "Interfaces/VectorWorks/Extension/IExtensionParametric.h"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <unordered_set>

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

void VerifyParameters(const VWParametricObj& object, const std::vector<ParametricValue>& values) {
    constexpr double kTolerance = 1e-7;
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
                matches = std::abs(object.GetParamReal(name) - value.realValue) <= kTolerance;
                break;
            case ParametricValueKind::String:
                matches = Utf8(object.GetParamString(name)) == value.stringValue;
                break;
        }
        if (!matches) {
            throw std::runtime_error(
                "Vectorworks parametric readback mismatch: " + value.universalName);
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

class CreatedParametricGuard final {
public:
    explicit CreatedParametricGuard(MCObjectHandle object) : object_(object) {}

    ~CreatedParametricGuard() {
        if (object_ && gSDK) {
            gSDK->DeleteObject(object_, false);
        }
    }

    CreatedParametricGuard(const CreatedParametricGuard&) = delete;
    CreatedParametricGuard& operator=(const CreatedParametricGuard&) = delete;

    void Release() noexcept {
        object_ = nullptr;
    }

private:
    MCObjectHandle object_ = nullptr;
};

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
    EVSPluginType pluginType = kVSPluginMenu;
    if (!gSDK->GetPluginType(pluginName, pluginType) || pluginType != kVSPluginObject) {
        throw std::invalid_argument(
            "installed parametric object plugin was not found by universal name: " +
            universalPluginName);
    }
    ParametricDescriptor descriptor;
    descriptor.universalPluginName = universalPluginName;
    TXString localizedPluginName;
    if (gSDK->GetLocalizedPluginName(pluginName, localizedPluginName) &&
        !localizedPluginName.IsEmpty()) {
        descriptor.localizedPluginName = Utf8(localizedPluginName);
    } else {
        descriptor.localizedPluginName = universalPluginName;
    }
    if (!DescribeWithParams2Provider(descriptor) &&
        !DescribeWithParamsProvider(descriptor)) {
        throw std::runtime_error(
            "installed parametric plugin does not expose an SDK parameter schema provider: " +
            universalPluginName);
    }
    descriptor.descriptorFingerprint = Fingerprint(descriptor);
    return descriptor;
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
    MCObjectHandle object = gSDK->CreateCustomObject(
        TXString(pluginName.c_str()),
        WorldPt(spec.x, spec.y),
        spec.rotationDegrees,
        true);
    if (!object || gSDK->GetObjectTypeN(object) != kParametricNode) {
        throw std::runtime_error(
            "Vectorworks did not create the requested built-in opening object");
    }
    CreatedParametricGuard guard(object);
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
    if (!ids.windowSillUniversalName.empty()) {
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
    if (std::abs(actualWidth - spec.width) > kTolerance ||
        std::abs(actualHeight - spec.height) > kTolerance ||
        (!ids.windowSillUniversalName.empty() &&
         std::abs(actualSill - spec.windowSillHeight) > kTolerance)) {
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

    BuiltInOpeningReceipt receipt;
    receipt.object = object;
    receipt.wall = spec.expectedWall;
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

MCObjectHandle CreateVerifiedParametricObject(const ParametricCreateSpec& spec) {
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
    const WorldPt location(spec.x, spec.y);
    MCObjectHandle object = gSDK->CreateCustomObject(
        TXString(spec.universalPluginName.c_str()),
        location,
        spec.rotationDegrees,
        spec.requireWallHost);
    if (!object || gSDK->GetObjectTypeN(object) != kParametricNode) {
        throw std::runtime_error(
            "Vectorworks did not create the requested parametric object: " +
            spec.universalPluginName);
    }

    VWParametricObj parametric(object);
    if (Utf8(parametric.GetParametricName()) != spec.universalPluginName) {
        throw std::runtime_error("created parametric object has the wrong universal plugin name");
    }
    ValidateParameters(parametric, spec.parameters);
    ApplyParameters(parametric, spec.parameters);
    gSDK->ResetObject(object);
    VerifyParameters(parametric, spec.parameters);
    if (spec.requireWallHost && !IsObjectHostedByWall(object, spec.expectedWall)) {
        throw std::runtime_error(
            "Vectorworks did not insert the parametric object into the required wall");
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
