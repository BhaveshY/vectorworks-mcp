#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace VectorworksMCP {

enum class ParametricValueKind {
    Integer,
    Boolean,
    Real,
    String,
};

struct ParametricValue {
    std::string universalName;
    ParametricValueKind kind = ParametricValueKind::String;
    std::int32_t integerValue = 0;
    bool booleanValue = false;
    double realValue = 0.0;
    std::string stringValue;
};

struct ParametricParameterDescriptor {
    std::string universalName;
    std::string localizedName;
    short fieldStyle = 0;
    std::string value;
};

struct ParametricDescriptor {
    std::string universalPluginName;
    std::string localizedPluginName;
    std::string descriptorFingerprint;
    std::vector<ParametricParameterDescriptor> parameters;
};

struct ParametricCreateSpec {
    std::string universalPluginName;
    double x = 0.0;
    double y = 0.0;
    double rotationDegrees = 0.0;
    bool requireWallHost = false;
    MCObjectHandle expectedWall = nullptr;
    std::string descriptorFingerprint;
    std::vector<ParametricValue> parameters;
};

enum class BuiltInParametricKind {
    Door,
    Window,
};

struct BuiltInOpeningSemanticIds {
    std::string widthUniversalName;
    std::string heightUniversalName;
    std::string windowSillUniversalName;
};

struct BuiltInOpeningCreateSpec {
    BuiltInParametricKind kind = BuiltInParametricKind::Door;
    MCObjectHandle expectedWall = nullptr;
    double x = 0.0;
    double y = 0.0;
    double rotationDegrees = 0.0;
    double width = 0.0;
    double height = 0.0;
    double windowSillHeight = 0.0;
    std::string descriptorFingerprint;
};

struct BuiltInOpeningReceipt {
    MCObjectHandle object = nullptr;
    MCObjectHandle wall = nullptr;
    BuiltInParametricKind kind = BuiltInParametricKind::Door;
    std::string universalPluginName;
    std::string descriptorFingerprint;
    BuiltInOpeningSemanticIds semanticIds;
    double width = 0.0;
    double height = 0.0;
    double windowSillHeight = 0.0;
    bool wallHostVerified = false;
    bool semanticReadbackVerified = false;
};

ParametricDescriptor DescribeParametricObject(MCObjectHandle object);
ParametricDescriptor DescribeParametricDefinition(const std::string& universalPluginName);
const char* BuiltInParametricUniversalName(BuiltInParametricKind kind) noexcept;
ParametricDescriptor DescribeBuiltInParametricDefinition(BuiltInParametricKind kind);
BuiltInOpeningSemanticIds ResolveBuiltInOpeningSemanticIds(
    BuiltInParametricKind kind,
    const ParametricDescriptor& descriptor);
BuiltInOpeningReceipt CreateVerifiedBuiltInOpening(
    const BuiltInOpeningCreateSpec& spec);
void VerifyBuiltInOpeningReceipt(
    MCObjectHandle object,
    const BuiltInOpeningReceipt& receipt);

// These functions deliberately do not open or commit an undo event. The native
// transaction executor owns undo scope so a mixed plan remains one atomic event.
MCObjectHandle CreateVerifiedParametricObject(const ParametricCreateSpec& spec);
void UpdateVerifiedParametricObject(
    MCObjectHandle object,
    const std::string& expectedUniversalPluginName,
    const std::string& descriptorFingerprint,
    const std::vector<ParametricValue>& parameters);

bool IsObjectHostedByWall(MCObjectHandle object, MCObjectHandle expectedWall);

}  // namespace VectorworksMCP
