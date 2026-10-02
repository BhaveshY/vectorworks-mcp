#pragma once

// Shared JSON helpers and request-parameter accessors for every handler file.
// Split out of VectorworksMCPBridge.cpp (1.0 rewrite) so new handler files can
// parse params and emit JSON without duplicating code.

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

namespace VectorworksMCP {

// ---- JSON output -----------------------------------------------------------
std::string EscapeJsonString(std::string_view value);
std::string JsonString(std::string_view value);
std::string JsonNumber(double value);
inline const char* JsonBool(bool value) { return value ? "true" : "false"; }
std::string JsonStringArray(const std::vector<std::string>& values);
std::string ToLower(std::string value);
std::string TrimCopy(std::string value);
bool TruthyEnvironmentFlag(const char* name);
bool FalsyEnvironmentFlag(const char* name);
std::string IsoUtcNow();
std::string IsoUtc(std::int64_t unixMilliseconds);
std::int64_t UnixMillisecondsNow();

// ---- flat params (top-level keys; nested values kept raw) ------------------
struct ParamValue {
    enum class Type {
        String,
        Number,
        Boolean,
        Null,
        Other,
    };

    Type type = Type::Other;
    std::string stringValue;
    std::string rawJson;
    double numberValue = 0.0;
    bool boolValue = false;
};

using Params = std::unordered_map<std::string, ParamValue>;

Params ParseParams(std::string_view paramsJson);
bool HasParam(const Params& params, const std::string& key);
std::string GetStringParam(const Params& params, const std::string& key, std::string defaultValue = "");
double GetNumberParam(const Params& params, const std::string& key, double defaultValue);
double GetFiniteNumberParam(const Params& params, const std::string& key, double defaultValue);
bool GetBoolParam(const Params& params, const std::string& key, bool defaultValue = false);
std::string GetRawJsonParam(const Params& params, const std::string& key, std::string defaultValue = "");
int GetBoundedIntParam(
    const Params& params,
    const std::string& key,
    int defaultValue,
    int minValue,
    int maxValue);
int GetRequiredBoundedIntParam(
    const Params& params,
    const std::string& key,
    int minValue,
    int maxValue);
// Lenient integer read: accepts numbers and numeric strings, clamps to range.
int GetClampedIntParam(
    const Params& params,
    const std::string& key,
    int defaultValue,
    int minValue,
    int maxValue);
// Lenient boolean read: accepts true/false, 1/0 and "true"/"false" strings.
bool GetLenientBoolParam(const Params& params, const std::string& key, bool defaultValue);

// ---- JSON DOM (for nested params such as arrays of uuids or [x,y]) -------
struct JsonValue {
    enum class Type { Null, Bool, Number, String, Array, Object };
    Type type = Type::Null;
    bool boolValue = false;
    double numberValue = 0.0;
    std::string stringValue;
    std::vector<JsonValue> items;
    std::vector<std::pair<std::string, JsonValue>> members;

    bool IsNull() const { return type == Type::Null; }
    bool IsArray() const { return type == Type::Array; }
    bool IsObject() const { return type == Type::Object; }
    bool IsString() const { return type == Type::String; }
    bool IsNumber() const { return type == Type::Number; }
    bool IsBool() const { return type == Type::Bool; }
    const JsonValue* Find(const std::string& key) const;
};

JsonValue ParseJsonValue(std::string_view text);
// Returns the parsed nested value for key (string values that themselves
// contain JSON arrays/objects are parsed too, for forgiving hosts).
JsonValue GetJsonParam(const Params& params, const std::string& key);
std::vector<std::string> GetStringListParam(const Params& params, const std::string& key);
// Accepts [x,y], {"x":..,"y":..} or "x,y".
bool GetPointParam(const Params& params, const std::string& key, double& x, double& y);

}  // namespace VectorworksMCP
