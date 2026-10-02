#include "StdAfx.h"

#include "JsonParams.hpp"

#include "BridgeProtocol.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <ctime>
#include <iomanip>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace VectorworksMCP {

namespace {

bool IsJsonWhitespace(char ch) {
    return ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t';
}

bool IsHexDigit(char ch) {
    return ('0' <= ch && ch <= '9') || ('a' <= ch && ch <= 'f') || ('A' <= ch && ch <= 'F');
}

int HexDigitValue(char ch) {
    if ('0' <= ch && ch <= '9') return ch - '0';
    if ('a' <= ch && ch <= 'f') return 10 + (ch - 'a');
    return 10 + (ch - 'A');
}

// Minimal, strict JSON reader shared by the flat params parser and the DOM.
class Reader {
public:
    explicit Reader(std::string_view text) : text_(text) {}

    bool AtEnd() const { return pos_ >= text_.size(); }

    char Peek() const {
        if (AtEnd()) {
            throw std::invalid_argument("unexpected end of params JSON");
        }
        return text_[pos_];
    }

    void SkipWhitespace() {
        while (!AtEnd() && IsJsonWhitespace(text_[pos_])) {
            ++pos_;
        }
    }

    void Expect(char expected, std::string_view message) {
        SkipWhitespace();
        if (AtEnd() || text_[pos_] != expected) {
            throw std::invalid_argument(std::string(message));
        }
        ++pos_;
    }

    bool ConsumeIf(char expected) {
        SkipWhitespace();
        if (!AtEnd() && text_[pos_] == expected) {
            ++pos_;
            return true;
        }
        return false;
    }

    bool ConsumeLiteral(std::string_view literal) {
        if (text_.substr(pos_, literal.size()) == literal) {
            pos_ += literal.size();
            return true;
        }
        return false;
    }

    std::uint32_t ParseHex4() {
        if (pos_ + 4u > text_.size()) {
            throw std::invalid_argument("params unicode escape was incomplete");
        }
        std::uint32_t value = 0;
        for (int i = 0; i < 4; ++i) {
            const char hex = text_[pos_++];
            if (!IsHexDigit(hex)) {
                throw std::invalid_argument("params unicode escape contained a non-hex digit");
            }
            value = (value << 4) | static_cast<std::uint32_t>(HexDigitValue(hex));
        }
        return value;
    }

    std::string ParseString() {
        SkipWhitespace();
        if (AtEnd() || text_[pos_] != '"') {
            throw std::invalid_argument("expected JSON string in params");
        }
        ++pos_;
        std::string value;
        while (!AtEnd()) {
            const char ch = text_[pos_++];
            if (ch == '"') {
                return value;
            }
            if (static_cast<unsigned char>(ch) < 0x20u) {
                throw std::invalid_argument("params string contained an unescaped control character");
            }
            if (ch != 0x5C) {
                value.push_back(ch);
                continue;
            }
            if (AtEnd()) {
                throw std::invalid_argument("params string ended after escape marker");
            }
            const char escaped = text_[pos_++];
            switch (escaped) {
                case '"':
                case '/':
                case 0x5C:
                    value.push_back(escaped);
                    break;
                case 'b': value.push_back('\b'); break;
                case 'f': value.push_back('\f'); break;
                case 'n': value.push_back('\n'); break;
                case 'r': value.push_back('\r'); break;
                case 't': value.push_back('\t'); break;
                case 'u': {
                    std::uint32_t codepoint = ParseHex4();
                    if (codepoint >= 0xD800u && codepoint <= 0xDBFFu) {
                        if (pos_ + 6u > text_.size() || text_[pos_] != 0x5C || text_[pos_ + 1u] != 'u') {
                            throw std::invalid_argument("params unicode escape contained an unpaired high surrogate");
                        }
                        pos_ += 2u;
                        const std::uint32_t low = ParseHex4();
                        if (low < 0xDC00u || low > 0xDFFFu) {
                            throw std::invalid_argument("params unicode escape contained an invalid low surrogate");
                        }
                        codepoint = 0x10000u + ((codepoint - 0xD800u) << 10) + (low - 0xDC00u);
                    } else if (codepoint >= 0xDC00u && codepoint <= 0xDFFFu) {
                        throw std::invalid_argument("params unicode escape contained an unpaired low surrogate");
                    }
                    Protocol::AppendUtf8(value, codepoint);
                    break;
                }
                default:
                    throw std::invalid_argument("params string contained an invalid escape sequence");
            }
        }
        throw std::invalid_argument("unterminated params string");
    }

    double ParseNumber() {
        SkipWhitespace();
        const auto start = pos_;
        if (!AtEnd() && text_[pos_] == '-') ++pos_;
        if (AtEnd()) throw std::invalid_argument("incomplete params number");
        if (text_[pos_] == '0') {
            ++pos_;
        } else if ('1' <= text_[pos_] && text_[pos_] <= '9') {
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
        } else {
            throw std::invalid_argument("invalid params number");
        }
        if (!AtEnd() && text_[pos_] == '.') {
            ++pos_;
            const auto digits = pos_;
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
            if (digits == pos_) throw std::invalid_argument("invalid params number");
        }
        if (!AtEnd() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            ++pos_;
            if (!AtEnd() && (text_[pos_] == '+' || text_[pos_] == '-')) ++pos_;
            const auto digits = pos_;
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
            if (digits == pos_) throw std::invalid_argument("invalid params number");
        }
        return std::stod(std::string(text_.substr(start, pos_ - start)));
    }

    void SkipValue(int depth = 0) {
        if (depth > 64) throw std::invalid_argument("params JSON nesting is too deep");
        SkipWhitespace();
        if (AtEnd()) throw std::invalid_argument("expected params value");
        const char ch = Peek();
        if (ch == '"') { ParseString(); return; }
        if (ch == '{') {
            Expect('{', "expected params object");
            if (ConsumeIf('}')) return;
            while (true) {
                ParseString();
                Expect(':', "expected ':' after params object key");
                SkipValue(depth + 1);
                if (ConsumeIf('}')) return;
                Expect(',', "expected ',' between params object fields");
            }
        }
        if (ch == '[') {
            Expect('[', "expected params array");
            if (ConsumeIf(']')) return;
            while (true) {
                SkipValue(depth + 1);
                if (ConsumeIf(']')) return;
                Expect(',', "expected ',' between params array items");
            }
        }
        if (ch == '-' || std::isdigit(static_cast<unsigned char>(ch))) { ParseNumber(); return; }
        if (ConsumeLiteral("true") || ConsumeLiteral("false") || ConsumeLiteral("null")) return;
        throw std::invalid_argument("expected params value");
    }

    JsonValue ParseDom(int depth = 0) {
        if (depth > 64) throw std::invalid_argument("JSON nesting is too deep");
        SkipWhitespace();
        if (AtEnd()) throw std::invalid_argument("expected JSON value");
        JsonValue value;
        const char ch = Peek();
        if (ch == '"') {
            value.type = JsonValue::Type::String;
            value.stringValue = ParseString();
            return value;
        }
        if (ch == '{') {
            value.type = JsonValue::Type::Object;
            Expect('{', "expected JSON object");
            if (ConsumeIf('}')) return value;
            while (true) {
                std::string key = ParseString();
                Expect(':', "expected ':' after JSON object key");
                value.members.emplace_back(std::move(key), ParseDom(depth + 1));
                if (ConsumeIf('}')) return value;
                Expect(',', "expected ',' between JSON object fields");
            }
        }
        if (ch == '[') {
            value.type = JsonValue::Type::Array;
            Expect('[', "expected JSON array");
            if (ConsumeIf(']')) return value;
            while (true) {
                value.items.push_back(ParseDom(depth + 1));
                if (ConsumeIf(']')) return value;
                Expect(',', "expected ',' between JSON array items");
            }
        }
        if (ch == '-' || std::isdigit(static_cast<unsigned char>(ch))) {
            value.type = JsonValue::Type::Number;
            value.numberValue = ParseNumber();
            return value;
        }
        if (ConsumeLiteral("true")) { value.type = JsonValue::Type::Bool; value.boolValue = true; return value; }
        if (ConsumeLiteral("false")) { value.type = JsonValue::Type::Bool; value.boolValue = false; return value; }
        if (ConsumeLiteral("null")) { value.type = JsonValue::Type::Null; return value; }
        throw std::invalid_argument("expected JSON value");
    }

    void Finish(const char* message) {
        SkipWhitespace();
        if (!AtEnd()) throw std::invalid_argument(message);
    }

    std::size_t Position() const { return pos_; }
    std::string_view Slice(std::size_t start, std::size_t end) const { return text_.substr(start, end - start); }

private:
    std::string_view text_;
    std::size_t pos_ = 0u;
};

ParamValue ParseFlatValue(Reader& reader) {
    reader.SkipWhitespace();
    const char ch = reader.Peek();
    ParamValue value;
    if (ch == '"') {
        value.type = ParamValue::Type::String;
        value.stringValue = reader.ParseString();
        return value;
    }
    if (ch == '-' || std::isdigit(static_cast<unsigned char>(ch))) {
        value.type = ParamValue::Type::Number;
        value.numberValue = reader.ParseNumber();
        return value;
    }
    if (reader.ConsumeLiteral("true")) { value.type = ParamValue::Type::Boolean; value.boolValue = true; return value; }
    if (reader.ConsumeLiteral("false")) { value.type = ParamValue::Type::Boolean; value.boolValue = false; return value; }
    if (reader.ConsumeLiteral("null")) { value.type = ParamValue::Type::Null; return value; }
    value.type = ParamValue::Type::Other;
    const auto start = reader.Position();
    reader.SkipValue();
    value.rawJson = std::string(reader.Slice(start, reader.Position()));
    return value;
}

}  // namespace

std::string EscapeJsonString(std::string_view value) {
    return Protocol::EscapeJsonString(value);
}

std::string JsonString(std::string_view value) {
    std::string out = "\"";
    out += Protocol::EscapeJsonString(value);
    out += "\"";
    return out;
}

std::string JsonNumber(double value) {
    if (!std::isfinite(value)) {
        return "0";
    }
    std::ostringstream out;
    out << std::setprecision(15) << value;
    return out.str();
}

std::string JsonStringArray(const std::vector<std::string>& values) {
    std::string json = "[";
    for (std::size_t index = 0; index < values.size(); ++index) {
        if (index != 0u) json += ",";
        json += JsonString(values[index]);
    }
    json += "]";
    return json;
}

std::string ToLower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return value;
}

std::string TrimCopy(std::string value) {
    auto first = value.begin();
    while (first != value.end() && std::isspace(static_cast<unsigned char>(*first))) ++first;
    auto last = value.end();
    while (last != first && std::isspace(static_cast<unsigned char>(*(last - 1)))) --last;
    return std::string(first, last);
}

bool TruthyEnvironmentFlag(const char* name) {
    const char* raw = std::getenv(name);
    if (!raw) return false;
    const std::string value = ToLower(TrimCopy(raw));
    return value == "1" || value == "true" || value == "yes" || value == "on";
}

bool FalsyEnvironmentFlag(const char* name) {
    const char* raw = std::getenv(name);
    if (!raw) return false;
    const std::string value = ToLower(TrimCopy(raw));
    return value == "0" || value == "false" || value == "no" || value == "off";
}

std::int64_t UnixMillisecondsNow() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string IsoUtc(std::int64_t unixMilliseconds) {
    const std::time_t seconds = static_cast<std::time_t>(unixMilliseconds / 1000);
    std::tm utc{};
#if defined(_WIN32)
    gmtime_s(&utc, &seconds);
#else
    gmtime_r(&seconds, &utc);
#endif
    char buffer[32] = {};
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%S", &utc);
    std::ostringstream out;
    out << buffer << '.' << std::setw(3) << std::setfill('0') << (unixMilliseconds % 1000) << 'Z';
    return out.str();
}

std::string IsoUtcNow() {
    return IsoUtc(UnixMillisecondsNow());
}

Params ParseParams(std::string_view paramsJson) {
    if (paramsJson.empty()) paramsJson = "{}";
    Reader reader(paramsJson);
    Params values;
    reader.Expect('{', "request params must be a JSON object");
    if (reader.ConsumeIf('}')) {
        reader.Finish("params contained trailing JSON");
        return values;
    }
    while (true) {
        std::string key = reader.ParseString();
        reader.Expect(':', "expected ':' after params key");
        values[key] = ParseFlatValue(reader);
        if (reader.ConsumeIf('}')) {
            reader.Finish("params contained trailing JSON");
            return values;
        }
        reader.Expect(',', "expected ',' between params fields");
    }
}

bool HasParam(const Params& params, const std::string& key) {
    return params.find(key) != params.end();
}

std::string GetStringParam(const Params& params, const std::string& key, std::string defaultValue) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    if (found->second.type != ParamValue::Type::String) {
        throw std::invalid_argument(key + " must be a string");
    }
    return found->second.stringValue;
}

double GetNumberParam(const Params& params, const std::string& key, double defaultValue) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    if (found->second.type != ParamValue::Type::Number) {
        throw std::invalid_argument(key + " must be a number");
    }
    return found->second.numberValue;
}

double GetFiniteNumberParam(const Params& params, const std::string& key, double defaultValue) {
    const double value = GetNumberParam(params, key, defaultValue);
    if (!std::isfinite(value)) {
        throw std::invalid_argument(key + " must be a finite number");
    }
    return value;
}

bool GetBoolParam(const Params& params, const std::string& key, bool defaultValue) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    if (found->second.type != ParamValue::Type::Boolean) {
        throw std::invalid_argument(key + " must be a boolean");
    }
    return found->second.boolValue;
}

bool GetLenientBoolParam(const Params& params, const std::string& key, bool defaultValue) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    const auto& value = found->second;
    if (value.type == ParamValue::Type::Boolean) return value.boolValue;
    if (value.type == ParamValue::Type::Number) return value.numberValue != 0.0;
    if (value.type == ParamValue::Type::String) {
        const std::string text = ToLower(TrimCopy(value.stringValue));
        if (text == "true" || text == "1" || text == "yes" || text == "on") return true;
        if (text == "false" || text == "0" || text == "no" || text == "off" || text.empty()) return false;
    }
    throw std::invalid_argument(key + " must be a boolean");
}

std::string GetRawJsonParam(const Params& params, const std::string& key, std::string defaultValue) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    if (found->second.type != ParamValue::Type::Other || found->second.rawJson.empty()) {
        throw std::invalid_argument(key + " must be an object or array");
    }
    return found->second.rawJson;
}

int GetBoundedIntParam(
    const Params& params,
    const std::string& key,
    int defaultValue,
    int minValue,
    int maxValue) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    if (found->second.type != ParamValue::Type::Number) {
        throw std::invalid_argument(key + " must be an integer");
    }
    const double raw = found->second.numberValue;
    if (raw < static_cast<double>(std::numeric_limits<int>::min()) ||
        raw > static_cast<double>(std::numeric_limits<int>::max()) ||
        raw != static_cast<double>(static_cast<int>(raw))) {
        throw std::invalid_argument(key + " must be an integer");
    }
    const int value = static_cast<int>(raw);
    if (value < minValue) {
        throw std::invalid_argument(key + " must be >= " + std::to_string(minValue));
    }
    if (value > maxValue) {
        throw std::invalid_argument(key + " must be <= " + std::to_string(maxValue));
    }
    return value;
}

int GetRequiredBoundedIntParam(
    const Params& params,
    const std::string& key,
    int minValue,
    int maxValue) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        throw std::invalid_argument(key + " is required");
    }
    return GetBoundedIntParam(params, key, minValue, minValue, maxValue);
}

int GetClampedIntParam(
    const Params& params,
    const std::string& key,
    int defaultValue,
    int minValue,
    int maxValue) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    double raw = 0.0;
    if (found->second.type == ParamValue::Type::Number) {
        raw = found->second.numberValue;
    } else if (found->second.type == ParamValue::Type::String) {
        try {
            raw = std::stod(TrimCopy(found->second.stringValue));
        } catch (...) {
            throw std::invalid_argument(key + " must be an integer");
        }
    } else {
        throw std::invalid_argument(key + " must be an integer");
    }
    if (!std::isfinite(raw)) {
        throw std::invalid_argument(key + " must be a finite integer");
    }
    if (raw < static_cast<double>(minValue)) return minValue;
    if (raw > static_cast<double>(maxValue)) return maxValue;
    return static_cast<int>(raw);
}

const JsonValue* JsonValue::Find(const std::string& key) const {
    for (const auto& member : members) {
        if (member.first == key) return &member.second;
    }
    return nullptr;
}

JsonValue ParseJsonValue(std::string_view text) {
    Reader reader(text);
    JsonValue value = reader.ParseDom();
    reader.Finish("JSON value contained trailing data");
    return value;
}

JsonValue GetJsonParam(const Params& params, const std::string& key) {
    const auto found = params.find(key);
    if (found == params.end()) {
        return JsonValue{};
    }
    const auto& value = found->second;
    JsonValue result;
    switch (value.type) {
        case ParamValue::Type::Null:
            return result;
        case ParamValue::Type::Boolean:
            result.type = JsonValue::Type::Bool;
            result.boolValue = value.boolValue;
            return result;
        case ParamValue::Type::Number:
            result.type = JsonValue::Type::Number;
            result.numberValue = value.numberValue;
            return result;
        case ParamValue::Type::String: {
            const std::string trimmed = TrimCopy(value.stringValue);
            if (!trimmed.empty() && (trimmed.front() == '[' || trimmed.front() == '{')) {
                try {
                    return ParseJsonValue(trimmed);
                } catch (...) {
                }
            }
            result.type = JsonValue::Type::String;
            result.stringValue = value.stringValue;
            return result;
        }
        case ParamValue::Type::Other:
            return ParseJsonValue(value.rawJson);
    }
    return result;
}

std::vector<std::string> GetStringListParam(const Params& params, const std::string& key) {
    const JsonValue value = GetJsonParam(params, key);
    std::vector<std::string> out;
    if (value.IsNull()) return out;
    if (value.IsString()) {
        // Comma separated list for forgiving hosts.
        std::stringstream input(value.stringValue);
        std::string part;
        while (std::getline(input, part, ',')) {
            part = TrimCopy(part);
            if (!part.empty()) out.push_back(part);
        }
        return out;
    }
    if (!value.IsArray()) {
        throw std::invalid_argument(key + " must be an array of strings");
    }
    for (const auto& item : value.items) {
        if (!item.IsString()) {
            throw std::invalid_argument(key + " must be an array of strings");
        }
        out.push_back(item.stringValue);
    }
    return out;
}

bool GetPointParam(const Params& params, const std::string& key, double& x, double& y) {
    const JsonValue value = GetJsonParam(params, key);
    if (value.IsNull()) return false;
    if (value.IsArray() && value.items.size() >= 2u &&
        value.items[0].IsNumber() && value.items[1].IsNumber()) {
        x = value.items[0].numberValue;
        y = value.items[1].numberValue;
        return true;
    }
    if (value.IsObject()) {
        const JsonValue* vx = value.Find("x");
        const JsonValue* vy = value.Find("y");
        if (vx && vy && vx->IsNumber() && vy->IsNumber()) {
            x = vx->numberValue;
            y = vy->numberValue;
            return true;
        }
    }
    if (value.IsString()) {
        const auto comma = value.stringValue.find(',');
        if (comma != std::string::npos) {
            try {
                x = std::stod(TrimCopy(value.stringValue.substr(0, comma)));
                y = std::stod(TrimCopy(value.stringValue.substr(comma + 1)));
                return true;
            } catch (...) {
            }
        }
    }
    throw std::invalid_argument(key + " must be a point [x, y]");
}

}  // namespace VectorworksMCP
