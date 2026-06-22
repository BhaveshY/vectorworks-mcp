#include "StdAfx.h"

#include "BridgeProtocol.hpp"

#include <cctype>
#include <stdexcept>
#include <string>
#include <string_view>

namespace VectorworksMCP {
namespace Protocol {

namespace {

bool IsWhitespace(char ch) {
    return ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t';
}

bool IsHex(char ch) {
    return ('0' <= ch && ch <= '9') || ('a' <= ch && ch <= 'f') || ('A' <= ch && ch <= 'F');
}

int HexValue(char ch) {
    if ('0' <= ch && ch <= '9') {
        return ch - '0';
    }
    if ('a' <= ch && ch <= 'f') {
        return 10 + (ch - 'a');
    }
    return 10 + (ch - 'A');
}

std::string_view Trim(std::string_view value) {
    while (!value.empty() && IsWhitespace(value.front())) {
        value.remove_prefix(1);
    }
    while (!value.empty() && IsWhitespace(value.back())) {
        value.remove_suffix(1);
    }
    return value;
}

class JsonCursor {
public:
    explicit JsonCursor(std::string_view text) : text_(text) {}

    bool AtEnd() const {
        return pos_ >= text_.size();
    }

    std::size_t Position() const {
        return pos_;
    }

    char Peek() const {
        if (AtEnd()) {
            throw std::invalid_argument("unexpected end of JSON");
        }
        return text_[pos_];
    }

    void SkipWhitespace() {
        while (!AtEnd() && IsWhitespace(text_[pos_])) {
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

    std::string ParseString() {
        SkipWhitespace();
        if (AtEnd() || text_[pos_] != '"') {
            throw std::invalid_argument("expected JSON string");
        }
        ++pos_;
        std::string value;
        while (!AtEnd()) {
            const char ch = text_[pos_++];
            if (ch == '"') {
                return value;
            }
            if (static_cast<unsigned char>(ch) < 0x20u) {
                throw std::invalid_argument("JSON string contained an unescaped control character");
            }
            if (ch != '\\') {
                value.push_back(ch);
                continue;
            }
            if (AtEnd()) {
                throw std::invalid_argument("JSON string ended after escape marker");
            }
            const char escaped = text_[pos_++];
            switch (escaped) {
                case '"':
                case '\\':
                case '/':
                    value.push_back(escaped);
                    break;
                case 'b':
                    value.push_back('\b');
                    break;
                case 'f':
                    value.push_back('\f');
                    break;
                case 'n':
                    value.push_back('\n');
                    break;
                case 'r':
                    value.push_back('\r');
                    break;
                case 't':
                    value.push_back('\t');
                    break;
                case 'u': {
                    if (pos_ + 4u > text_.size()) {
                        throw std::invalid_argument("JSON unicode escape was incomplete");
                    }
                    int codepoint = 0;
                    for (int i = 0; i < 4; ++i) {
                        const char hex = text_[pos_++];
                        if (!IsHex(hex)) {
                            throw std::invalid_argument("JSON unicode escape contained a non-hex digit");
                        }
                        codepoint = (codepoint << 4) | HexValue(hex);
                    }
                    if (codepoint <= 0x7f) {
                        value.push_back(static_cast<char>(codepoint));
                    } else {
                        throw std::invalid_argument("native bridge scaffold only supports ASCII request strings");
                    }
                    break;
                }
                default:
                    throw std::invalid_argument("JSON string contained an invalid escape sequence");
            }
        }
        throw std::invalid_argument("unterminated JSON string");
    }

    void SkipValue() {
        SkipWhitespace();
        if (AtEnd()) {
            throw std::invalid_argument("expected JSON value");
        }
        const char ch = Peek();
        if (ch == '"') {
            ParseString();
            return;
        }
        if (ch == '{') {
            SkipObject();
            return;
        }
        if (ch == '[') {
            SkipArray();
            return;
        }
        if (ch == '-' || std::isdigit(static_cast<unsigned char>(ch))) {
            SkipNumber();
            return;
        }
        if (ConsumeLiteral("true") || ConsumeLiteral("false") || ConsumeLiteral("null")) {
            return;
        }
        throw std::invalid_argument("expected JSON value");
    }

    void SkipObject() {
        Expect('{', "expected JSON object");
        if (ConsumeIf('}')) {
            return;
        }
        while (true) {
            ParseString();
            Expect(':', "expected ':' after JSON object key");
            SkipValue();
            if (ConsumeIf('}')) {
                return;
            }
            Expect(',', "expected ',' between JSON object fields");
        }
    }

    void SkipArray() {
        Expect('[', "expected JSON array");
        if (ConsumeIf(']')) {
            return;
        }
        while (true) {
            SkipValue();
            if (ConsumeIf(']')) {
                return;
            }
            Expect(',', "expected ',' between JSON array items");
        }
    }

private:
    bool ConsumeLiteral(std::string_view literal) {
        if (text_.substr(pos_, literal.size()) == literal) {
            pos_ += literal.size();
            return true;
        }
        return false;
    }

    void SkipDigits() {
        const auto start = pos_;
        while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
            ++pos_;
        }
        if (start == pos_) {
            throw std::invalid_argument("expected JSON number digits");
        }
    }

    void SkipNumber() {
        if (!AtEnd() && text_[pos_] == '-') {
            ++pos_;
        }
        if (AtEnd()) {
            throw std::invalid_argument("incomplete JSON number");
        }
        if (text_[pos_] == '0') {
            ++pos_;
        } else if ('1' <= text_[pos_] && text_[pos_] <= '9') {
            SkipDigits();
        } else {
            throw std::invalid_argument("invalid JSON number");
        }
        if (!AtEnd() && text_[pos_] == '.') {
            ++pos_;
            SkipDigits();
        }
        if (!AtEnd() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            ++pos_;
            if (!AtEnd() && (text_[pos_] == '+' || text_[pos_] == '-')) {
                ++pos_;
            }
            SkipDigits();
        }
    }

    std::string_view text_;
    std::size_t pos_ = 0u;
};

void ValidateCompleteJsonValue(std::string_view value, std::string_view label) {
    JsonCursor cursor(value);
    cursor.SkipValue();
    cursor.SkipWhitespace();
    if (!cursor.AtEnd()) {
        throw std::invalid_argument(std::string(label) + " contained trailing JSON");
    }
}

std::string EscapeJsonString(std::string_view value) {
    std::string escaped;
    escaped.reserve(value.size() + 8u);
    constexpr char kHex[] = "0123456789abcdef";
    for (const unsigned char ch : value) {
        switch (ch) {
            case '"':
                escaped += "\\\"";
                break;
            case '\\':
                escaped += "\\\\";
                break;
            case '\b':
                escaped += "\\b";
                break;
            case '\f':
                escaped += "\\f";
                break;
            case '\n':
                escaped += "\\n";
                break;
            case '\r':
                escaped += "\\r";
                break;
            case '\t':
                escaped += "\\t";
                break;
            default:
                if (ch < 0x20u) {
                    escaped += "\\u00";
                    escaped.push_back(kHex[(ch >> 4) & 0x0f]);
                    escaped.push_back(kHex[ch & 0x0f]);
                } else {
                    escaped.push_back(static_cast<char>(ch));
                }
                break;
        }
    }
    return escaped;
}

}  // namespace

std::array<std::uint8_t, kFrameHeaderBytes> EncodeFrameHeader(std::uint32_t payloadSize) {
    if (payloadSize == 0 || payloadSize > kMaxFrameBytes) {
        throw std::out_of_range("payload size is outside the Vectorworks MCP frame limit");
    }
    return {
        static_cast<std::uint8_t>((payloadSize >> 24) & 0xFFu),
        static_cast<std::uint8_t>((payloadSize >> 16) & 0xFFu),
        static_cast<std::uint8_t>((payloadSize >> 8) & 0xFFu),
        static_cast<std::uint8_t>(payloadSize & 0xFFu),
    };
}

std::uint32_t DecodeFrameHeader(const std::array<std::uint8_t, kFrameHeaderBytes>& header) {
    const auto size =
        (static_cast<std::uint32_t>(header[0]) << 24) |
        (static_cast<std::uint32_t>(header[1]) << 16) |
        (static_cast<std::uint32_t>(header[2]) << 8) |
        static_cast<std::uint32_t>(header[3]);
    if (size == 0 || size > kMaxFrameBytes) {
        throw std::out_of_range("frame size is outside the Vectorworks MCP frame limit");
    }
    return size;
}

RequestEnvelope ParseRequestEnvelope(std::string_view payload) {
    if (payload.size() > kMaxFrameBytes) {
        throw std::out_of_range("request payload is outside the Vectorworks MCP frame limit");
    }

    JsonCursor cursor(payload);
    RequestEnvelope request;
    bool seenId = false;
    bool seenAction = false;
    bool seenParams = false;
    bool seenAuthToken = false;

    cursor.Expect('{', "request envelope must be a JSON object");
    if (!cursor.ConsumeIf('}')) {
        while (true) {
            const std::string key = cursor.ParseString();
            cursor.Expect(':', "expected ':' after request field name");
            if (key == kFieldId) {
                if (seenId) {
                    throw std::invalid_argument("duplicate request id field");
                }
                request.id = cursor.ParseString();
                seenId = true;
            } else if (key == kFieldAction) {
                if (seenAction) {
                    throw std::invalid_argument("duplicate request action field");
                }
                request.action = cursor.ParseString();
                seenAction = true;
            } else if (key == kFieldParams) {
                if (seenParams) {
                    throw std::invalid_argument("duplicate request params field");
                }
                cursor.SkipWhitespace();
                const auto start = cursor.Position();
                if (cursor.Peek() != '{') {
                    throw std::invalid_argument("request params must be a JSON object");
                }
                cursor.SkipObject();
                const auto end = cursor.Position();
                request.paramsJson = std::string(Trim(payload.substr(start, end - start)));
                seenParams = true;
            } else if (key == kFieldAuthToken) {
                if (seenAuthToken) {
                    throw std::invalid_argument("duplicate request auth_token field");
                }
                request.authToken = cursor.ParseString();
                seenAuthToken = true;
            } else {
                cursor.SkipValue();
            }
            if (cursor.ConsumeIf('}')) {
                break;
            }
            cursor.Expect(',', "expected ',' between request fields");
        }
    }

    cursor.SkipWhitespace();
    if (!cursor.AtEnd()) {
        throw std::invalid_argument("request envelope contained trailing JSON");
    }
    if (!seenId || request.id.empty()) {
        throw std::invalid_argument("request id is required");
    }
    if (!seenAction || request.action.empty()) {
        throw std::invalid_argument("request action is required");
    }
    if (!seenParams) {
        request.paramsJson = "{}";
    }
    return request;
}

std::string SerializeResponseEnvelope(const ResponseEnvelope& response) {
    if (response.id.empty()) {
        throw std::invalid_argument("response id is required");
    }

    std::string serialized = "{\"id\":\"";
    serialized += EscapeJsonString(response.id);
    serialized += "\",\"success\":";

    if (response.success) {
        const auto resultJson = Trim(response.resultJson);
        if (resultJson.empty()) {
            throw std::invalid_argument("success response result is required");
        }
        ValidateCompleteJsonValue(resultJson, "success response result");
        serialized += "true,\"result\":";
        serialized.append(resultJson.data(), resultJson.size());
        serialized += "}";
        return serialized;
    }

    if (Trim(response.error).empty()) {
        throw std::invalid_argument("failure response error is required");
    }
    serialized += "false,\"error\":\"";
    serialized += EscapeJsonString(response.error);
    serialized += "\"}";
    return serialized;
}

}  // namespace Protocol
}  // namespace VectorworksMCP
