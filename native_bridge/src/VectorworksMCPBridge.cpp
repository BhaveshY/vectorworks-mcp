#include "StdAfx.h"

#include "BridgeDispatcher.hpp"
#include "BridgeProtocol.hpp"
#include "CadRequestQueue.hpp"
#include "NativeIOHandlers.hpp"
#include "NativeTransport.hpp"
#include "ViewDocumentHandlers.hpp"

#if defined(SDK_VERSION)
#include "Interfaces/VectorWorks/Extension/ISpaceObjectSupport.h"
#include "BimObjectHandlers.hpp"
#include "NativeObjectFactory.hpp"
#include "NativeTransaction.hpp"
#include "ParametricObjectAdapter.hpp"
#include "ResourceWorksheetHandlers.hpp"
#include "SpaceObjectHandlers.hpp"
#endif

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <exception>
#include <fstream>
#include <functional>
#include <iomanip>
#include <limits>
#include <map>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#if defined(SDK_VERSION)
#define VECTORWORKS_MCP_HAS_SDK 1
#else
#define VECTORWORKS_MCP_HAS_SDK 0
#endif

namespace VectorworksMCP {

void OnVectorworksMainPluginEvent();

namespace {

CadRequestQueue gCadQueue;
NativeTransport gTransport;
std::atomic_bool gStopRequested{false};
std::atomic_bool gCadQueuePumpActive{false};
std::mutex gTransportStartMutex;
std::chrono::steady_clock::time_point gNextTransportStartAttempt{};
constexpr auto kCadRequestTimeout = std::chrono::seconds(30);
constexpr auto kTransportStartRetryInterval = std::chrono::seconds(2);
constexpr bool kCadHandlersImplemented = VECTORWORKS_MCP_HAS_SDK != 0;

#if VECTORWORKS_MCP_HAS_SDK
struct DeferredDocumentOpen {
    std::string requestId;
    ViewDocument::PreparedOpenDocument request;
    bool responseSent = false;
};

std::mutex gDeferredDocumentOpenMutex;
std::optional<DeferredDocumentOpen> gDeferredDocumentOpen;
#endif

class ScopedAtomicBoolReset {
public:
    explicit ScopedAtomicBoolReset(std::atomic_bool& value) : value_(value) {}
    ~ScopedAtomicBoolReset() {
        value_.store(false);
    }

    ScopedAtomicBoolReset(const ScopedAtomicBoolReset&) = delete;
    ScopedAtomicBoolReset& operator=(const ScopedAtomicBoolReset&) = delete;

private:
    std::atomic_bool& value_;
};

#if VECTORWORKS_MCP_HAS_SDK && defined(_WINDOWS)
constexpr wchar_t kMainContextPumpWindowClassName[] = L"VectorworksMCPMainContextPump";
constexpr UINT_PTR kMainContextPumpTimerId = 1;
constexpr UINT kMainContextPumpIntervalMs = 50;
constexpr UINT kMainContextPumpMessage = WM_APP + 0x4d3;

std::atomic_bool gMainContextPumpReady{false};
HWND gMainContextPumpWindow = nullptr;
ATOM gMainContextPumpWindowClass = 0;
HINSTANCE gMainContextPumpInstance = nullptr;
int gMainContextPumpModuleAnchor = 0;

LRESULT CALLBACK MainContextPumpWndProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    if ((message == WM_TIMER && wParam == kMainContextPumpTimerId) ||
        message == kMainContextPumpMessage) {
        OnVectorworksMainPluginEvent();
        return 0;
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

HINSTANCE MainContextPumpModuleHandle() {
    HMODULE module = nullptr;
    if (GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&gMainContextPumpModuleAnchor),
            &module) &&
        module) {
        return reinterpret_cast<HINSTANCE>(module);
    }
    return GetModuleHandleW(nullptr);
}

bool PinBridgeModuleForProcessLifetime() {
    HMODULE module = nullptr;
    return GetModuleHandleExW(
               GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
               reinterpret_cast<LPCWSTR>(&gMainContextPumpModuleAnchor),
               &module) != 0 &&
        module != nullptr;
}

void UnregisterMainContextPumpWindowClass() {
    if (gMainContextPumpWindowClass != 0 && gMainContextPumpInstance) {
        UnregisterClassW(kMainContextPumpWindowClassName, gMainContextPumpInstance);
        gMainContextPumpWindowClass = 0;
    }
    gMainContextPumpInstance = nullptr;
}

bool StartMainContextPump() {
    if (gMainContextPumpWindow) {
        gMainContextPumpReady.store(true);
        return true;
    }

    HINSTANCE instance = MainContextPumpModuleHandle();
    gMainContextPumpInstance = instance;
    WNDCLASSW windowClass = {};
    windowClass.lpfnWndProc = MainContextPumpWndProc;
    windowClass.hInstance = instance;
    windowClass.lpszClassName = kMainContextPumpWindowClassName;

    const ATOM registeredClass = RegisterClassW(&windowClass);
    if (registeredClass == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
        gMainContextPumpReady.store(false);
        return false;
    }
    if (registeredClass != 0) {
        gMainContextPumpWindowClass = registeredClass;
    }

    gMainContextPumpWindow = CreateWindowExW(
        0,
        kMainContextPumpWindowClassName,
        L"Vectorworks MCP Main Context Pump",
        WS_POPUP,
        0,
        0,
        0,
        0,
        nullptr,
        nullptr,
        instance,
        nullptr);
    if (!gMainContextPumpWindow) {
        UnregisterMainContextPumpWindowClass();
        gMainContextPumpReady.store(false);
        return false;
    }

    if (SetTimer(gMainContextPumpWindow, kMainContextPumpTimerId, kMainContextPumpIntervalMs, nullptr) == 0) {
        DestroyWindow(gMainContextPumpWindow);
        gMainContextPumpWindow = nullptr;
        UnregisterMainContextPumpWindowClass();
        gMainContextPumpReady.store(false);
        return false;
    }

    gMainContextPumpReady.store(true);
    return true;
}

void StopMainContextPump() {
    gMainContextPumpReady.store(false);
    if (gMainContextPumpWindow) {
        KillTimer(gMainContextPumpWindow, kMainContextPumpTimerId);
        DestroyWindow(gMainContextPumpWindow);
        gMainContextPumpWindow = nullptr;
    }
    UnregisterMainContextPumpWindowClass();
}

bool MainContextPumpReady() {
    return gMainContextPumpReady.load();
}

void NotifyMainContextPump() {
    if (gMainContextPumpWindow && gMainContextPumpReady.load()) {
        PostMessageW(gMainContextPumpWindow, kMainContextPumpMessage, 0, 0);
    }
}

constexpr const char* MainContextPumpName() {
    return "win32_ui_timer";
}
#else
bool StartMainContextPump() {
    return false;
}

void StopMainContextPump() {}

bool MainContextPumpReady() {
    return false;
}

void NotifyMainContextPump() {}

constexpr const char* MainContextPumpName() {
    return "unavailable";
}
#endif

#if VECTORWORKS_MCP_HAS_SDK
void StageDeferredDocumentOpen(
    const std::string& requestId,
    ViewDocument::PreparedOpenDocument request) {
    std::lock_guard<std::mutex> lock(gDeferredDocumentOpenMutex);
    if (gDeferredDocumentOpen) {
        throw std::runtime_error("another document open is already pending");
    }
    gDeferredDocumentOpen = DeferredDocumentOpen{
        requestId,
        std::move(request),
        false,
    };
}

void MarkDeferredDocumentOpenResponseSent(
    const Protocol::RequestEnvelope& request,
    const Protocol::ResponseEnvelope& response) {
    if (request.action != "open_document" || !response.success) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(gDeferredDocumentOpenMutex);
        if (!gDeferredDocumentOpen || gDeferredDocumentOpen->requestId != request.id) {
            return;
        }
        gDeferredDocumentOpen->responseSent = true;
    }
    NotifyMainContextPump();
}

std::optional<ViewDocument::PreparedOpenDocument> TakeReadyDeferredDocumentOpen() {
    std::lock_guard<std::mutex> lock(gDeferredDocumentOpenMutex);
    if (!gDeferredDocumentOpen || !gDeferredDocumentOpen->responseSent) {
        return std::nullopt;
    }
    auto request = std::move(gDeferredDocumentOpen->request);
    gDeferredDocumentOpen.reset();
    return request;
}

void ClearDeferredDocumentOpen() {
    std::lock_guard<std::mutex> lock(gDeferredDocumentOpenMutex);
    gDeferredDocumentOpen.reset();
}
#else
void MarkDeferredDocumentOpenResponseSent(
    const Protocol::RequestEnvelope&,
    const Protocol::ResponseEnvelope&) {}
#endif

bool CadHandlersRuntimeReady() {
    return kCadHandlersImplemented && MainContextPumpReady();
}

bool IsWhitespace(char ch) {
    return ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t';
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

std::string JsonString(std::string_view value) {
    return "\"" + EscapeJsonString(value) + "\"";
}

std::string JsonNumber(double value) {
    if (!std::isfinite(value)) {
        return "0";
    }
    std::ostringstream out;
    out << std::setprecision(15) << value;
    return out.str();
}

std::string ToLower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return value;
}

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

class FlatJsonParamsParser {
public:
    explicit FlatJsonParamsParser(std::string_view text) : text_(text) {}

    std::unordered_map<std::string, ParamValue> Parse() {
        std::unordered_map<std::string, ParamValue> values;
        Expect('{', "request params must be a JSON object");
        if (ConsumeIf('}')) {
            Finish();
            return values;
        }
        while (true) {
            const std::string key = ParseString();
            Expect(':', "expected ':' after params key");
            values[key] = ParseValue();
            if (ConsumeIf('}')) {
                Finish();
                return values;
            }
            Expect(',', "expected ',' between params fields");
        }
    }

private:
    bool AtEnd() const {
        return pos_ >= text_.size();
    }

    char Peek() const {
        if (AtEnd()) {
            throw std::invalid_argument("unexpected end of params JSON");
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

    bool ConsumeLiteral(std::string_view literal) {
        if (text_.substr(pos_, literal.size()) == literal) {
            pos_ += literal.size();
            return true;
        }
        return false;
    }

    static bool IsHex(char ch) {
        return ('0' <= ch && ch <= '9') || ('a' <= ch && ch <= 'f') || ('A' <= ch && ch <= 'F');
    }

    static int HexValue(char ch) {
        if ('0' <= ch && ch <= '9') {
            return ch - '0';
        }
        if ('a' <= ch && ch <= 'f') {
            return 10 + (ch - 'a');
        }
        return 10 + (ch - 'A');
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
            if (ch != '\\') {
                value.push_back(ch);
                continue;
            }
            if (AtEnd()) {
                throw std::invalid_argument("params string ended after escape marker");
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
                        throw std::invalid_argument("params unicode escape was incomplete");
                    }
                    int codepoint = 0;
                    for (int i = 0; i < 4; ++i) {
                        const char hex = text_[pos_++];
                        if (!IsHex(hex)) {
                            throw std::invalid_argument("params unicode escape contained a non-hex digit");
                        }
                        codepoint = (codepoint << 4) | HexValue(hex);
                    }
                    if (codepoint <= 0x7f) {
                        value.push_back(static_cast<char>(codepoint));
                    } else {
                        throw std::invalid_argument("native bridge params only support ASCII unicode escapes");
                    }
                    break;
                }
                default:
                    throw std::invalid_argument("params string contained an invalid escape sequence");
            }
        }
        throw std::invalid_argument("unterminated params string");
    }

    ParamValue ParseNumber() {
        const auto start = pos_;
        if (!AtEnd() && text_[pos_] == '-') {
            ++pos_;
        }
        if (AtEnd()) {
            throw std::invalid_argument("incomplete params number");
        }
        if (text_[pos_] == '0') {
            ++pos_;
        } else if ('1' <= text_[pos_] && text_[pos_] <= '9') {
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
                ++pos_;
            }
        } else {
            throw std::invalid_argument("invalid params number");
        }
        if (!AtEnd() && text_[pos_] == '.') {
            ++pos_;
            const auto digits = pos_;
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
                ++pos_;
            }
            if (digits == pos_) {
                throw std::invalid_argument("invalid params number");
            }
        }
        if (!AtEnd() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            ++pos_;
            if (!AtEnd() && (text_[pos_] == '+' || text_[pos_] == '-')) {
                ++pos_;
            }
            const auto digits = pos_;
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
                ++pos_;
            }
            if (digits == pos_) {
                throw std::invalid_argument("invalid params number");
            }
        }
        ParamValue value;
        value.type = ParamValue::Type::Number;
        value.numberValue = std::stod(std::string(text_.substr(start, pos_ - start)));
        return value;
    }

    void SkipValue() {
        SkipWhitespace();
        if (AtEnd()) {
            throw std::invalid_argument("expected params value");
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
            ParseNumber();
            return;
        }
        if (ConsumeLiteral("true") || ConsumeLiteral("false") || ConsumeLiteral("null")) {
            return;
        }
        throw std::invalid_argument("expected params value");
    }

    void SkipObject() {
        Expect('{', "expected params object");
        if (ConsumeIf('}')) {
            return;
        }
        while (true) {
            ParseString();
            Expect(':', "expected ':' after params object key");
            SkipValue();
            if (ConsumeIf('}')) {
                return;
            }
            Expect(',', "expected ',' between params object fields");
        }
    }

    void SkipArray() {
        Expect('[', "expected params array");
        if (ConsumeIf(']')) {
            return;
        }
        while (true) {
            SkipValue();
            if (ConsumeIf(']')) {
                return;
            }
            Expect(',', "expected ',' between params array items");
        }
    }

    ParamValue ParseValue() {
        SkipWhitespace();
        if (AtEnd()) {
            throw std::invalid_argument("expected params value");
        }
        const char ch = Peek();
        if (ch == '"') {
            ParamValue value;
            value.type = ParamValue::Type::String;
            value.stringValue = ParseString();
            return value;
        }
        if (ch == '-' || std::isdigit(static_cast<unsigned char>(ch))) {
            return ParseNumber();
        }
        if (ConsumeLiteral("true")) {
            ParamValue value;
            value.type = ParamValue::Type::Boolean;
            value.boolValue = true;
            return value;
        }
        if (ConsumeLiteral("false")) {
            ParamValue value;
            value.type = ParamValue::Type::Boolean;
            value.boolValue = false;
            return value;
        }
        if (ConsumeLiteral("null")) {
            ParamValue value;
            value.type = ParamValue::Type::Null;
            return value;
        }
        ParamValue value;
        value.type = ParamValue::Type::Other;
        const auto start = pos_;
        SkipValue();
        value.rawJson = std::string(text_.substr(start, pos_ - start));
        return value;
    }

    void Finish() {
        SkipWhitespace();
        if (!AtEnd()) {
            throw std::invalid_argument("params contained trailing JSON");
        }
    }

    std::string_view text_;
    std::size_t pos_ = 0u;
};

using Params = std::unordered_map<std::string, ParamValue>;

Params ParseParams(std::string_view paramsJson) {
    return FlatJsonParamsParser(paramsJson.empty() ? "{}" : paramsJson).Parse();
}

std::string GetStringParam(const Params& params, const std::string& key, std::string defaultValue = "") {
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

bool GetBoolParam(const Params& params, const std::string& key, bool defaultValue = false) {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    if (found->second.type != ParamValue::Type::Boolean) {
        throw std::invalid_argument(key + " must be a boolean");
    }
    return found->second.boolValue;
}

std::string GetRawJsonParam(const Params& params, const std::string& key, std::string defaultValue = "") {
    const auto found = params.find(key);
    if (found == params.end() || found->second.type == ParamValue::Type::Null) {
        return defaultValue;
    }
    if (found->second.type != ParamValue::Type::Other || found->second.rawJson.empty()) {
        throw std::invalid_argument(key + " must be an object or array");
    }
    return found->second.rawJson;
}

std::string TrimCopy(std::string value) {
    auto first = value.begin();
    while (first != value.end() && std::isspace(static_cast<unsigned char>(*first))) {
        ++first;
    }
    auto last = value.end();
    while (last != first && std::isspace(static_cast<unsigned char>(*(last - 1)))) {
        --last;
    }
    return std::string(first, last);
}

bool TruthyEnvironmentFlag(const char* name) {
    const char* raw = std::getenv(name);
    if (!raw) {
        return false;
    }
    std::string value = ToLower(TrimCopy(raw));
    return value == "1" || value == "true" || value == "yes" || value == "on";
}

std::string DefaultAuthTokenPath() {
    if (const char* configured = std::getenv("VW_MCP_AUTH_TOKEN_FILE")) {
        if (configured[0] != '\0') {
            return configured;
        }
    }
    if (const char* userProfile = std::getenv("USERPROFILE")) {
        if (userProfile[0] != '\0') {
            std::string path = userProfile;
            if (!path.empty() && path.back() != '\\' && path.back() != '/') {
                path += "\\";
            }
            path += ".vectorworks-mcp\\auth-token";
            return path;
        }
    }
    return "";
}

std::string ReadAuthTokenFile() {
    const auto path = DefaultAuthTokenPath();
    if (path.empty()) {
        return "";
    }
    std::ifstream input(path);
    if (!input) {
        return "";
    }
    std::string token;
    std::getline(input, token);
    return TrimCopy(token);
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

NativeTransportOptions GetTransportOptionsFromEnvironment() {
    NativeTransportOptions options;
    if (const char* host = std::getenv("VW_MCP_HOST")) {
        if (host[0] != '\0') {
            options.host = host;
        }
    }
    if (const char* port = std::getenv("VW_MCP_PORT")) {
        try {
            const auto parsed = std::stoul(port);
            if (parsed <= 65535u) {
                options.port = static_cast<std::uint16_t>(parsed);
            }
        } catch (...) {
            // Keep the default port when the environment is malformed.
        }
    }
    return options;
}

void AppendTransportStartupDiagnostic(
    const std::string& state,
    const std::string& detail) noexcept {
    try {
        const char* userProfile = std::getenv("USERPROFILE");
        if (!userProfile || userProfile[0] == '\0') {
            return;
        }
        std::string path = userProfile;
        if (!path.empty() && path.back() != '\\' && path.back() != '/') {
            path += "\\";
        }
        path += ".vectorworks-mcp\\native-bridge-startup.log";
        std::ofstream output(path, std::ios::app);
        if (output) {
            output << state << ": " << detail << '\n';
        }
    } catch (...) {
    }
}

std::string RequiredAuthTokenFromEnvironment() {
    if (const char* token = std::getenv("VW_MCP_AUTH_TOKEN")) {
        if (token[0] != '\0') {
            return token;
        }
    }
    return ReadAuthTokenFile();
}

bool RequestAuthAccepted(const Protocol::RequestEnvelope& request) {
    if (TruthyEnvironmentFlag("VW_MCP_INSECURE_NO_AUTH")) {
        return true;
    }
    const std::string requiredToken = RequiredAuthTokenFromEnvironment();
    return !requiredToken.empty() && request.authToken == requiredToken;
}

Protocol::ResponseEnvelope HandlePingOnTransportThread(const Protocol::RequestEnvelope& request) {
#if VECTORWORKS_MCP_HAS_SDK
    const bool ready = CadHandlersRuntimeReady();
    std::string payload = R"({"pong":true,"version":"native-sdk-bridge-phase4","bridge_kind":"native_sdk_bridge_phase4","dispatch_mode":"native_sdk","handlers":)";
    payload += std::to_string(ImplementedActionCount(true));
    payload += ",\"cad_api_safe\":";
    payload += ready ? "true" : "false";
    payload += ",\"transport_only\":";
    payload += ready ? "false" : "true";
    payload += R"(,"native_bridge":true,"native_phase":4)";
    payload += ",\"capability_revision\":" + std::to_string(kCapabilityRevision);
    payload += ",\"capability_fingerprint\":";
    payload += JsonString(CapabilityFingerprint(true));
    payload += ",\"implemented_actions\":";
    payload += ImplementedActionsJson(true);
    payload += ",\"create_object_types\":";
    payload += CreateObjectTypesJson(true);
    payload += ",\"cad_handlers_implemented\":true";
    payload += ",\"main_context_pump\":";
    payload += JsonString(MainContextPumpName());
    payload += ",\"main_context_pump_ready\":";
    payload += ready ? "true" : "false";
    payload += R"(,"dispatch_wakeup":"win32_message","pump_watchdog_ms":50,"pump_budget_ms":8)";
    payload += "}";
    return {
        request.id,
        true,
        payload,
        "",
    };
#else
    std::string payload = R"({"pong":true,"version":"native-scaffold-phase0","bridge_kind":"native_sdk_bridge_scaffold","dispatch_mode":"native_sdk","handlers":)";
    payload += std::to_string(ImplementedActionCount(false));
    payload += R"(,"cad_api_safe":false,"transport_only":true,"native_bridge":true,"native_phase":0,"implemented_actions":)";
    payload += ImplementedActionsJson(false);
    payload += ",\"capability_revision\":" + std::to_string(kCapabilityRevision);
    payload += ",\"capability_fingerprint\":";
    payload += JsonString(CapabilityFingerprint(false));
    payload += ",\"create_object_types\":";
    payload += CreateObjectTypesJson(false);
    payload += R"(,"cad_handlers_implemented":false})";
    return {
        request.id,
        true,
        payload,
        "",
    };
#endif
}

Protocol::ResponseEnvelope HandleCapabilitiesOnTransportThread(const Protocol::RequestEnvelope& request) {
    return {
        request.id,
        true,
        CapabilitiesResultJson(VECTORWORKS_MCP_HAS_SDK != 0),
        "",
    };
}

#if VECTORWORKS_MCP_HAS_SDK

std::string TxToUtf8(const TXString& value) {
    return value.GetStdString();
}

std::unordered_set<std::string> gKnownObjectHandleIds;
constexpr std::size_t kMaxPropertyValueChars = 1024;

std::string HandleIdFromRaw(std::uintptr_t raw) {
    std::ostringstream out;
    out << "0x" << std::hex << raw;
    return out.str();
}

std::string HandleId(MCObjectHandle handle) {
    const auto id = HandleIdFromRaw(reinterpret_cast<std::uintptr_t>(handle));
    if (handle) {
        gKnownObjectHandleIds.insert(id);
    }
    return id;
}

std::string ObjectUuidString(MCObjectHandle handle) {
    if (!handle) {
        return "";
    }
    TXString uuid;
    if (gSDK->GetObjectUuid(handle, uuid)) {
        return TxToUtf8(uuid);
    }
    return "";
}

class UnregisteredCreatedObjectGuard {
public:
    explicit UnregisteredCreatedObjectGuard(MCObjectHandle object) : object_(object) {}
    ~UnregisteredCreatedObjectGuard() {
        if (object_) {
            gSDK->DeleteObject(object_, false);
        }
    }
    void Release() { object_ = nullptr; }
    UnregisteredCreatedObjectGuard(const UnregisteredCreatedObjectGuard&) = delete;
    UnregisteredCreatedObjectGuard& operator=(const UnregisteredCreatedObjectGuard&) = delete;

private:
    MCObjectHandle object_;
};

void EndUndoEventOrThrow(const char* operation) {
    if (!gSDK->EndUndoEvent()) {
        throw std::runtime_error(
            std::string("Vectorworks failed to commit the undo event for ") + operation);
    }
}

std::string ObjectTypeName(short type) {
    switch (type) {
        case kLineNode:
            return "line";
        case kBoxNode:
            return "rect";
        case kOvalNode:
            return "oval";
        case kPolygonNode:
            return "polygon";
        case kArcNode:
            return "arc";
        case kFreehandPolygonNode:
            return "freehand";
        case kTextNode:
            return "text";
        case kGroupNode:
            return "group";
        case kSymbolNode:
            return "symbol";
        case kWorksheetNode:
            return "worksheet";
        case kPolylineNode:
            return "polyline";
        case kExtrudeNode:
            return "extrude";
        case kLayerNode:
            return "layer";
        case dimHeaderNode:
            return "dimension";
        case kWallNode:
            return "wall";
        case kSlabNode:
            return "slab";
        case kRoofContainerNode:
            return "roof";
        case kParametricNode:
            return "parametric";
        default:
            return "type_" + std::to_string(static_cast<int>(type));
    }
}

std::string ParametricPluginName(MCObjectHandle object) {
    if (!object || gSDK->GetObjectTypeN(object) != kParametricNode) {
        return "";
    }
    try {
        VWFC::VWObjects::VWParametricObj parametric(object);
        return TxToUtf8(parametric.GetParametricName());
    } catch (...) {
        return "";
    }
}

bool IsSpaceObject(MCObjectHandle object) {
    return ParametricPluginName(object) == "Space";
}

std::string SemanticObjectTypeName(MCObjectHandle object) {
    if (gSDK->GetObjectTypeN(object) == kParametricNode) {
        const std::string pluginName = ToLower(ParametricPluginName(object));
        if (pluginName == "space" || pluginName == "door" || pluginName == "window") {
            return pluginName;
        }
    }
    return ObjectTypeName(gSDK->GetObjectTypeN(object));
}

bool MatchesObjectType(MCObjectHandle object, std::string requestedType) {
    requestedType = ToLower(requestedType);
    if (requestedType.empty()) {
        return true;
    }
    if (requestedType == "rectangle" || requestedType == "box") {
        requestedType = "rect";
    } else if (requestedType == "linear_dimension") {
        requestedType = "dimension";
    }
    if (requestedType == "space" || requestedType == "door" || requestedType == "window") {
        return ToLower(ParametricPluginName(object)) == requestedType;
    }
    return ObjectTypeName(gSDK->GetObjectTypeN(object)) == requestedType;
}

bool IsUserVisibleObjectType(short type) {
    return type != kTermNode && type != kLayerNode && type != kHeaderNode && type != kUndoPlaceholderNode;
}

std::string LayerNameForObject(MCObjectHandle object) {
    MCObjectHandle layer = gSDK->SearchForAncestorType(kLayerNode, object);
    if (!layer) {
        return "";
    }
    TXString name;
    gSDK->GetObjectName(layer, name);
    return TxToUtf8(name);
}

std::string ClassNameForObject(MCObjectHandle object) {
    if (!object) {
        return "";
    }
    const InternalIndex classId = gSDK->GetObjectClass(object);
    if (!gSDK->ValidClass(classId)) {
        return "";
    }
    TXString name;
    gSDK->ClassIDToName(classId, name);
    return TxToUtf8(name);
}

std::string RgbStringFromColorRef(ColorRef colorRef) {
    RGBColor rgb = {};
    gSDK->ColorIndexToRGB(colorRef, rgb);
    return std::to_string(static_cast<unsigned int>(rgb.red)) + "," +
        std::to_string(static_cast<unsigned int>(rgb.green)) + "," +
        std::to_string(static_cast<unsigned int>(rgb.blue));
}

std::string ObjectJson(MCObjectHandle object) {
    const short type = gSDK->GetObjectTypeN(object);
    const std::string semanticType = SemanticObjectTypeName(object);
    const bool isSpace = semanticType == "space";
    const std::string pluginName = type == kParametricNode ? ParametricPluginName(object) : "";
    TXString name;
    gSDK->GetObjectName(object, name);

    std::string json = "{\"handle\":";
    json += JsonString(HandleId(object));
    const auto uuid = ObjectUuidString(object);
    if (!uuid.empty()) {
        json += ",\"uuid\":";
        json += JsonString(uuid);
    }
    json += ",\"type\":";
    json += JsonString(semanticType);
    json += ",\"type_id\":";
    json += std::to_string(static_cast<int>(type));
    if (!pluginName.empty()) {
        json += ",\"plugin_name\":";
        json += JsonString(pluginName);
    }
    if (semanticType != ObjectTypeName(type)) {
        json += ",\"native_type\":";
        json += JsonString(ObjectTypeName(type));
    }
    json += ",\"name\":";
    json += JsonString(TxToUtf8(name));

    if (isSpace) {
        VWFC::VWObjects::VWParametricObj parametric(object);
        json += ",\"room_id\":";
        json += JsonString(TxToUtf8(parametric.GetParamString(TXString("11_Room ID"))));
        json += ",\"height\":";
        json += JsonNumber(parametric.GetParamReal(TXString("11_Net Height")));
        VCOMPtr<VectorWorks::Extension::ISpaceObjectSupport> support(
            VectorWorks::Extension::IID_VCOMSpace);
        WorldCoord netArea = 0.0;
        WorldCoord grossArea = 0.0;
        if (support && support->NetArea(object, netArea) && support->GrossArea(object, grossArea)) {
            json += ",\"net_area\":";
            json += JsonNumber(static_cast<double>(netArea));
            json += ",\"gross_area\":";
            json += JsonNumber(static_cast<double>(grossArea));
        }
    }

    const auto layerName = LayerNameForObject(object);
    if (!layerName.empty()) {
        json += ",\"layer\":";
        json += JsonString(layerName);
    }
    const auto className = ClassNameForObject(object);
    if (!className.empty()) {
        json += ",\"class\":";
        json += JsonString(className);
        json += ",\"class_name\":";
        json += JsonString(className);
    }

    ObjectColorType colors = {};
    if (gSDK->GetColor(object, colors)) {
        json += ",\"fillColor\":";
        json += JsonString(RgbStringFromColorRef(colors.fillFore));
        json += ",\"penColor\":";
        json += JsonString(RgbStringFromColorRef(colors.penFore));
    }
    json += ",\"lineWeight\":";
    json += std::to_string(static_cast<int>(gSDK->GetLineWeight(object)));
    json += ",\"fillPattern\":";
    json += std::to_string(static_cast<int>(gSDK->GetFillPat(object)));
    json += ",\"opacity\":";
    json += std::to_string(static_cast<int>(gSDK->GetOpacity(object)));

    WorldRect bounds;
    gSDK->GetObjectBounds(object, bounds);
    json += ",\"bounds\":{\"top_left\":[";
    json += JsonNumber(bounds.Left());
    json += ",";
    json += JsonNumber(bounds.Top());
    json += "],\"bottom_right\":[";
    json += JsonNumber(bounds.Right());
    json += ",";
    json += JsonNumber(bounds.Bottom());
    json += "]}";
    json += "}";
    return json;
}

std::string ObjectListJson(const std::vector<MCObjectHandle>& objects) {
    std::string json = "[";
    for (std::size_t index = 0; index < objects.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        json += ObjectJson(objects[index]);
    }
    json += "]";
    return json;
}

std::string CountMapJson(const std::map<std::string, int>& counts) {
    std::string json = "{";
    bool first = true;
    for (const auto& [key, value] : counts) {
        if (!first) {
            json += ",";
        }
        first = false;
        json += JsonString(key);
        json += ":";
        json += std::to_string(value);
    }
    json += "}";
    return json;
}

std::string NestedCountMapJson(const std::map<std::string, std::map<std::string, int>>& counts) {
    std::string json = "{";
    bool first = true;
    for (const auto& [key, nested] : counts) {
        if (!first) {
            json += ",";
        }
        first = false;
        json += JsonString(key);
        json += ":";
        json += CountMapJson(nested);
    }
    json += "}";
    return json;
}

std::vector<MCObjectHandle> CollectLayerHandles() {
    std::vector<MCObjectHandle> layers;
    gSDK->ForEachLayerN([&](MCObjectHandle layer) {
        if (layer) {
            layers.push_back(layer);
        }
    });
    return layers;
}

std::vector<std::string> CollectLayerNames() {
    std::vector<std::string> names;
    for (MCObjectHandle layer : CollectLayerHandles()) {
        TXString name;
        gSDK->GetObjectName(layer, name);
        names.push_back(TxToUtf8(name));
    }
    return names;
}

std::string HandleGetDocumentInfo() {
    std::string filename = "Untitled.vwx";
    std::string filepath;

    VectorWorks::Filing::IFileIdentifierPtr activeFile(VectorWorks::Filing::IID_FileIdentifier);
    bool saved = false;
    if (activeFile && gSDK->GetActiveDocument(&activeFile, saved)) {
        TXString name;
        TXString path;
        activeFile->GetFileName(name);
        activeFile->GetFileFullPath(path);
        const auto utf8Name = TxToUtf8(name);
        if (!utf8Name.empty()) {
            filename = utf8Name;
        }
        filepath = TxToUtf8(path);
    }

    const auto layerNames = CollectLayerNames();
    int totalObjects = 0;
    for (MCObjectHandle layer : CollectLayerHandles()) {
        for (MCObjectHandle object = gSDK->FirstMemberObj(layer);
             object && gSDK->GetObjectTypeN(object) != kTermNode;
             object = gSDK->NextObject(object)) {
            if (IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
                ++totalObjects;
            }
        }
    }

    std::string json = "{\"filename\":";
    json += JsonString(filename);
    json += ",\"filepath\":";
    json += JsonString(filepath);
    json += ",\"layers\":[";
    for (std::size_t index = 0; index < layerNames.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        json += JsonString(layerNames[index]);
    }
    json += "],\"layer_count\":";
    json += std::to_string(layerNames.size());
    json += ",\"total_objects\":";
    json += std::to_string(totalObjects);
    json += "}";
    return json;
}

std::string HandleGetLayers() {
    std::string json = "[";
    const auto layers = CollectLayerHandles();
    for (std::size_t index = 0; index < layers.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        TXString name;
        gSDK->GetObjectName(layers[index], name);
        json += "{\"name\":";
        json += JsonString(TxToUtf8(name));
        json += ",\"visible\":";
        json += gSDK->IsVisible(layers[index]) ? "true" : "false";
        json += "}";
    }
    json += "]";
    return json;
}

MCObjectHandle FindLayerByName(const std::string& layerName) {
    if (layerName.empty()) {
        return nullptr;
    }
    for (MCObjectHandle layer : CollectLayerHandles()) {
        TXString name;
        gSDK->GetObjectName(layer, name);
        if (TxToUtf8(name) == layerName) {
            return layer;
        }
    }
    return nullptr;
}

void CollectObjectsInLayer(
    MCObjectHandle layer,
    const std::string& objectType,
    int limit,
    std::vector<MCObjectHandle>& outObjects) {
    for (MCObjectHandle object = gSDK->FirstMemberObj(layer);
         object && gSDK->GetObjectTypeN(object) != kTermNode && static_cast<int>(outObjects.size()) < limit;
         object = gSDK->NextObject(object)) {
        const short type = gSDK->GetObjectTypeN(object);
        if (!IsUserVisibleObjectType(type)) {
            continue;
        }
        if (MatchesObjectType(object, objectType)) {
            outObjects.push_back(object);
        }
    }
}

std::string HandleGetObjects(const Params& params) {
    const int limit = GetBoundedIntParam(params, "limit", 100, 1, 1000);
    const std::string layerName = GetStringParam(params, "layer");
    const std::string objectType = GetStringParam(params, "object_type");
    std::vector<MCObjectHandle> objects;
    objects.reserve(static_cast<std::size_t>(limit));

    if (!layerName.empty()) {
        MCObjectHandle layer = FindLayerByName(layerName);
        if (!layer) {
            throw std::runtime_error("Layer '" + layerName + "' not found");
        }
        CollectObjectsInLayer(layer, objectType, limit, objects);
        return ObjectListJson(objects);
    }

    for (MCObjectHandle layer : CollectLayerHandles()) {
        if (static_cast<int>(objects.size()) >= limit) {
            break;
        }
        CollectObjectsInLayer(layer, objectType, limit, objects);
    }
    return ObjectListJson(objects);
}

std::string HandleDrawingSummary(const Params& params) {
    const int scanLimit = GetBoundedIntParam(params, "scan_limit", GetBoundedIntParam(params, "limit", 1000, 1, 100000), 1, 100000);
    const bool includeExamples = GetBoolParam(params, "include_examples", true);
    const int exampleLimit = GetBoundedIntParam(params, "example_limit", 20, 0, 100);
    const std::string layerFilter = GetStringParam(params, "layer");
    const std::string objectTypeFilter = GetStringParam(params, "object_type");

    std::map<std::string, int> byType;
    std::map<std::string, int> byLayer;
    std::map<std::string, int> byClass;
    std::map<std::string, std::map<std::string, int>> byLayerType;
    std::vector<MCObjectHandle> examples;
    int scanned = 0;
    int namedCount = 0;
    bool truncated = false;
    bool hasBounds = false;
    double left = 0.0;
    double top = 0.0;
    double right = 0.0;
    double bottom = 0.0;

    for (MCObjectHandle layer : CollectLayerHandles()) {
        TXString txLayerName;
        gSDK->GetObjectName(layer, txLayerName);
        const std::string layerName = TxToUtf8(txLayerName);
        if (!layerFilter.empty() && layerName != layerFilter) {
            continue;
        }
        for (MCObjectHandle object = gSDK->FirstMemberObj(layer);
             object && gSDK->GetObjectTypeN(object) != kTermNode;
             object = gSDK->NextObject(object)) {
            const short type = gSDK->GetObjectTypeN(object);
            if (!IsUserVisibleObjectType(type) || !MatchesObjectType(object, objectTypeFilter)) {
                continue;
            }
            if (scanned >= scanLimit) {
                truncated = true;
                break;
            }
            ++scanned;
            const std::string objectType = SemanticObjectTypeName(object);
            ++byType[objectType];
            ++byLayer[layerName.empty() ? "unknown" : layerName];
            ++byLayerType[layerName.empty() ? "unknown" : layerName][objectType];
            const std::string className = ClassNameForObject(object);
            if (!className.empty()) {
                ++byClass[className];
            }
            TXString txName;
            gSDK->GetObjectName(object, txName);
            if (!TxToUtf8(txName).empty()) {
                ++namedCount;
            }
            if (includeExamples && static_cast<int>(examples.size()) < exampleLimit) {
                examples.push_back(object);
            }

            WorldRect bounds;
            gSDK->GetObjectBounds(object, bounds);
            const double objLeft = std::min(bounds.Left(), bounds.Right());
            const double objRight = std::max(bounds.Left(), bounds.Right());
            const double objTop = std::min(bounds.Top(), bounds.Bottom());
            const double objBottom = std::max(bounds.Top(), bounds.Bottom());
            if (!hasBounds) {
                left = objLeft;
                right = objRight;
                top = objTop;
                bottom = objBottom;
                hasBounds = true;
            } else {
                left = std::min(left, objLeft);
                right = std::max(right, objRight);
                top = std::min(top, objTop);
                bottom = std::max(bottom, objBottom);
            }
        }
        if (truncated) {
            break;
        }
    }

    const auto layers = CollectLayerHandles();
    std::string json = "{\"ok\":true,\"tool\":\"vw_drawing_summary\",\"native_summary\":true";
    json += ",\"query\":{\"layer\":";
    json += JsonString(layerFilter);
    json += ",\"object_type\":";
    json += JsonString(objectTypeFilter);
    json += ",\"scan_limit\":";
    json += std::to_string(scanLimit);
    json += ",\"include_examples\":";
    json += includeExamples ? "true" : "false";
    json += ",\"example_limit\":";
    json += std::to_string(exampleLimit);
    json += ",\"source\":\"native_drawing_summary\"}";
    json += ",\"document\":";
    json += HandleGetDocumentInfo();
    json += ",\"layers\":";
    json += HandleGetLayers();
    json += ",\"layer_count\":";
    json += std::to_string(layers.size());
    json += ",\"objects_returned\":";
    json += std::to_string(scanned);
    json += ",\"objects_scanned\":";
    json += std::to_string(scanned);
    json += ",\"possibly_truncated\":";
    json += truncated ? "true" : "false";
    json += ",\"named_objects_returned\":";
    json += std::to_string(namedCount);
    json += ",\"counts_by_type\":";
    json += CountMapJson(byType);
    json += ",\"counts_by_layer\":";
    json += CountMapJson(byLayer);
    json += ",\"counts_by_layer_type\":";
    json += NestedCountMapJson(byLayerType);
    json += ",\"counts_by_class\":";
    json += CountMapJson(byClass);
    json += ",\"bounds\":";
    if (hasBounds) {
        json += "{\"left\":";
        json += JsonNumber(left);
        json += ",\"top\":";
        json += JsonNumber(top);
        json += ",\"right\":";
        json += JsonNumber(right);
        json += ",\"bottom\":";
        json += JsonNumber(bottom);
        json += "}";
    } else {
        json += "null";
    }
    if (includeExamples) {
        json += ",\"examples\":";
        json += ObjectListJson(examples);
    }
    json += "}";
    return json;
}

std::vector<MCObjectHandle> CollectSelectedObjects() {
    std::vector<MCObjectHandle> selected;
    gSDK->ForEachObjectN(allObjects + descendIntoAll + descendIntoViewports + descendIntoAuxLists, [&](MCObjectHandle object) {
        if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object)) && gSDK->IsSelected(object)) {
            selected.push_back(object);
        }
    });
    return selected;
}

std::vector<MCObjectHandle> CollectObjectsByCriteria(const std::string& criteria, int limit = 1000) {
    std::vector<MCObjectHandle> objects;
    gSDK->ForEachObjectInCriteria(TXString(criteria.c_str()), [&](MCObjectHandle object) {
        if (static_cast<int>(objects.size()) >= limit) {
            return;
        }
        if (!object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
            return;
        }
        if (std::find(objects.begin(), objects.end(), object) == objects.end()) {
            objects.push_back(object);
        }
    });
    return objects;
}

std::string HandleFindObjects(const Params& params) {
    const std::string criteria = TrimCopy(GetStringParam(params, "criteria", "ALL"));
    const std::string layer = TrimCopy(GetStringParam(params, "layer"));
    const std::string objectType = TrimCopy(GetStringParam(params, "object_type"));
    const int limit = GetBoundedIntParam(params, "limit", 100, 1, 1000);
    if (criteria.empty()) {
        throw std::invalid_argument("criteria is required");
    }
    if (!layer.empty()) {
        bool layerFound = false;
        for (MCObjectHandle layerHandle : CollectLayerHandles()) {
            TXString layerName;
            gSDK->GetObjectName(layerHandle, layerName);
            if (TxToUtf8(layerName) == layer) {
                layerFound = true;
                break;
            }
        }
        if (!layerFound) {
            throw std::invalid_argument("layer filter was not found: " + layer);
        }
    }
    std::vector<MCObjectHandle> filtered;
    filtered.reserve(static_cast<std::size_t>(limit));
    for (MCObjectHandle object : CollectObjectsByCriteria(criteria, 1000)) {
        if (!layer.empty() && LayerNameForObject(object) != layer) {
            continue;
        }
        if (!objectType.empty() && !MatchesObjectType(object, objectType)) {
            continue;
        }
        filtered.push_back(object);
        if (static_cast<int>(filtered.size()) >= limit) {
            break;
        }
    }
    return ObjectListJson(filtered);
}

std::optional<std::string> ExactNameFromCriteria(const std::string& criteria) {
    constexpr std::string_view prefix = "((N='";
    constexpr std::string_view suffix = "'))";
    if (criteria.size() <= prefix.size() + suffix.size()) {
        return std::nullopt;
    }
    if (criteria.compare(0, prefix.size(), prefix) != 0) {
        return std::nullopt;
    }
    if (criteria.compare(criteria.size() - suffix.size(), suffix.size(), suffix) != 0) {
        return std::nullopt;
    }
    std::string name = criteria.substr(prefix.size(), criteria.size() - prefix.size() - suffix.size());
    if (name.empty() || name.size() > 255u || name.find('\'') != std::string::npos) {
        return std::nullopt;
    }
    return name;
}

std::vector<MCObjectHandle> CollectObjectsByExactNameCriteria(const std::string& criteria) {
    const auto name = ExactNameFromCriteria(criteria);
    if (!name) {
        throw std::invalid_argument("selection delete criteria must be exact object-name criteria like ((N='Name'))");
    }
    std::vector<MCObjectHandle> objects;
    MCObjectHandle object = gSDK->GetNamedObject(TXString(name->c_str()));
    if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
        objects.push_back(object);
    }
    return objects;
}

std::string HandleSelection(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "get"));
    const int limit = GetBoundedIntParam(params, "limit", 1000, 1, 1000);
    if (action == "get") {
        const auto selected = CollectSelectedObjects();
        std::vector<MCObjectHandle> limited;
        limited.reserve(std::min(static_cast<std::size_t>(limit), selected.size()));
        for (MCObjectHandle object : selected) {
            if (static_cast<int>(limited.size()) >= limit) {
                break;
            }
            limited.push_back(object);
        }
        return ObjectListJson(limited);
    }
    if (action == "clear") {
        gSDK->DeselectAll();
        return R"({"cleared":true})";
    }
    if (action == "select") {
        const std::string criteria = GetStringParam(params, "criteria");
        if (criteria.empty()) {
            throw std::invalid_argument("criteria is required for selection select");
        }
        const auto exactName = ExactNameFromCriteria(criteria);
        int matchedCount = 0;
        int selectedCount = 0;
        gSDK->DeselectAll();
        if (exactName) {
            MCObjectHandle object = gSDK->GetNamedObject(TXString(exactName->c_str()));
            if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
                ++matchedCount;
                if (selectedCount < limit) {
                    gSDK->SelectObject(object, true);
                    ++selectedCount;
                }
            }
        } else {
            gSDK->ForEachObjectInCriteria(TXString(criteria.c_str()), [&](MCObjectHandle object) {
                if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
                    ++matchedCount;
                }
                if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object)) && selectedCount < limit) {
                    gSDK->SelectObject(object, true);
                    ++selectedCount;
                }
            });
        }
        return "{\"selected\":" + std::to_string(selectedCount)
            + ",\"matched\":" + std::to_string(matchedCount)
            + ",\"limit\":" + std::to_string(limit)
            + ",\"truncated\":" + (matchedCount > selectedCount ? "true" : "false") + "}";
    }
    if (action == "delete") {
        const std::string criteria = GetStringParam(params, "criteria");
        if (!criteria.empty()) {
            if (GetStringParam(params, "confirm") != "DELETE_EXACT_NAME") {
                throw std::invalid_argument("selection delete with criteria requires confirm='DELETE_EXACT_NAME'");
            }
        } else if (GetStringParam(params, "confirm") != "DELETE_SELECTED") {
            throw std::invalid_argument("selection delete requires confirm='DELETE_SELECTED'");
        }
        const auto targets = criteria.empty() ? CollectSelectedObjects() : CollectObjectsByExactNameCriteria(criteria);
        if (static_cast<int>(targets.size()) > limit) {
            throw std::invalid_argument("selection delete matched more objects than the requested limit");
        }
        if (targets.empty()) {
            return R"({"deleted":0})";
        }
        gSDK->SupportUndoAndRemove();
        gSDK->SetUndoMethod(kUndoSwapObjects);
        gSDK->NameUndoEvent(TXString("Vectorworks MCP delete selection"));
        int deleted = 0;
        try {
            for (MCObjectHandle object : targets) {
                if (!gSDK->AddBeforeSwapObject(object)) {
                    throw std::runtime_error("Vectorworks failed to register an object before selection deletion");
                }
                gSDK->DeleteObject(object, true);
                ++deleted;
            }
            EndUndoEventOrThrow("selection deletion");
        } catch (...) {
            gSDK->UndoAndRemove();
            throw;
        }
        return "{\"deleted\":" + std::to_string(deleted) + "}";
    }
    throw std::invalid_argument("unsupported selection action: " + action);
}

class PointListJsonParser {
public:
    PointListJsonParser(std::string_view text, std::string label)
        : text_(text), label_(std::move(label)) {}

    std::vector<WorldPt> Parse() {
        std::vector<WorldPt> points;
        Expect('[', "must be an array");
        if (ConsumeIf(']')) {
            Finish();
            return points;
        }
        while (true) {
            if (points.size() >= kMaxPoints) {
                throw std::invalid_argument(label_ + " is limited to " + std::to_string(kMaxPoints) + " points");
            }
            Expect('[', "point must be a two-number array");
            const double x = ParseNumber();
            Expect(',', "point must contain x and y");
            const double y = ParseNumber();
            Expect(']', "point must contain exactly two numbers");
            points.emplace_back(x, y);
            if (ConsumeIf(']')) {
                Finish();
                return points;
            }
            Expect(',', "points must be comma-separated");
        }
    }

private:
    static constexpr std::size_t kMaxPoints = 4096u;

    bool AtEnd() const {
        return pos_ >= text_.size();
    }

    void SkipWhitespace() {
        while (!AtEnd() && IsWhitespace(text_[pos_])) {
            ++pos_;
        }
    }

    bool ConsumeIf(char expected) {
        SkipWhitespace();
        if (!AtEnd() && text_[pos_] == expected) {
            ++pos_;
            return true;
        }
        return false;
    }

    void Expect(char expected, const char* message) {
        if (!ConsumeIf(expected)) {
            throw std::invalid_argument(label_ + " " + message);
        }
    }

    double ParseNumber() {
        SkipWhitespace();
        const auto start = pos_;
        if (!AtEnd() && (text_[pos_] == '-' || text_[pos_] == '+')) {
            ++pos_;
        }
        bool hasDigits = false;
        while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
            hasDigits = true;
            ++pos_;
        }
        if (!AtEnd() && text_[pos_] == '.') {
            ++pos_;
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
                hasDigits = true;
                ++pos_;
            }
        }
        if (!hasDigits) {
            throw std::invalid_argument(label_ + " coordinates must be numbers");
        }
        if (!AtEnd() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            ++pos_;
            if (!AtEnd() && (text_[pos_] == '-' || text_[pos_] == '+')) {
                ++pos_;
            }
            const auto exponentStart = pos_;
            while (!AtEnd() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) {
                ++pos_;
            }
            if (exponentStart == pos_) {
                throw std::invalid_argument(label_ + " coordinate exponent is incomplete");
            }
        }
        const double value = std::stod(std::string(text_.substr(start, pos_ - start)));
        if (!std::isfinite(value)) {
            throw std::invalid_argument(label_ + " coordinates must be finite");
        }
        return value;
    }

    void Finish() {
        SkipWhitespace();
        if (!AtEnd()) {
            throw std::invalid_argument(label_ + " contained trailing JSON");
        }
    }

    std::string_view text_;
    std::string label_;
    std::size_t pos_ = 0u;
};

struct PrimitiveSpec {
    std::string objectType;
    double x1 = 0.0;
    double y1 = 0.0;
    double x2 = 100.0;
    double y2 = 100.0;
    double radius = 50.0;
    double startAngle = 0.0;
    double sweepAngle = 90.0;
    double height = 3000.0;
    double thickness = 200.0;
    double elevation = 0.0;
    double slope = 30.0;
    double overhang = 500.0;
    double bearingInset = 0.0;
    double verticalMiter = 0.0;
    int miterType = 1;
    bool generateGableWalls = false;
    double width = 0.0;
    double sillHeight = 0.0;
    double rotation = 0.0;
    double textSize = 0.0;
    double dimensionOffset = 300.0;
    double dimensionTextOffset = 0.0;
    double directionX = 0.0;
    double directionY = 0.0;
    int dimensionType = 1;
    bool fixedSizeText = false;
    bool wrapText = false;
    bool closed = true;
    bool hasExplicitX = false;
    bool hasExplicitY = false;
    bool hasExplicitWidth = false;
    bool hasExplicitHeight = false;
    bool hasExplicitSill = false;
    std::vector<WorldPt> points;
    std::string text;
    std::string styleName;
    std::string name;
    std::string className;
    std::string roomId;
    std::string symbolDefinitionName;
    std::string pluginName;
    std::string descriptorFingerprint;
    std::string wallUuid;
    bool requireWallHost = false;
    std::vector<ParametricValue> parametricValues;
};

struct CreatedPrimitive {
    int index = 0;
    std::string objectType;
    MCObjectHandle handle = nullptr;
    std::vector<std::string> warnings;
    std::size_t vertexCount = 0u;
    bool closed = false;
    short nativeNodeType = 0;
    bool semanticallyVerified = false;
};

MCObjectHandle EnsureWritableLayer() {
    MCObjectHandle layer = gSDK->GetActiveLayer();
    if (!layer) {
        layer = gSDK->GetCurrentLayer();
    }
    if (!layer) {
        const auto layers = CollectLayerHandles();
        if (!layers.empty()) {
            layer = layers.front();
        }
    }
    if (!layer) {
        throw std::runtime_error(
            "active Vectorworks document has no writable design layer; "
            "open a document with a design layer before running CAD operations");
    }
    // Re-selecting the already-active layer for every object can trigger a
    // costly Vectorworks layer/document refresh. Only switch when recovery
    // selected a different writable layer.
    if (gSDK->GetCurrentLayer() != layer) {
        gSDK->SetCurrentLayer(layer);
    }
    return layer;
}

std::string ParametricDescriptorJson(const ParametricDescriptor& descriptor) {
    std::string json = "{\"universal_plugin_name\":" + JsonString(descriptor.universalPluginName);
    json += ",\"localized_plugin_name\":" + JsonString(descriptor.localizedPluginName);
    json += ",\"descriptor_fingerprint\":" + JsonString(descriptor.descriptorFingerprint);
    json += ",\"parameters\":[";
    for (std::size_t index = 0; index < descriptor.parameters.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        const auto& parameter = descriptor.parameters[index];
        json += "{\"id\":" + JsonString(parameter.universalName);
        json += ",\"display_name\":" + JsonString(parameter.localizedName);
        json += ",\"field_style\":" + std::to_string(parameter.fieldStyle);
        json += ",\"value\":" + JsonString(parameter.value) + "}";
    }
    json += "]}";
    return json;
}

std::string HandleDescribeParametricSchema(const Params& params) {
    const std::string pluginName = TrimCopy(GetStringParam(params, "plugin_name"));
    if (pluginName.empty()) {
        throw std::invalid_argument("plugin_name is required for parametric schema discovery");
    }
    return ParametricDescriptorJson(DescribeParametricDefinition(pluginName));
}

std::string JsonStringArray(const std::vector<std::string>& values);

std::string NativeIOLayerSnapshotJson(const NativeIO::LayerSnapshot& layer) {
    return "{\"uuid\":" + JsonString(layer.uuid) +
        ",\"name\":" + JsonString(layer.name) +
        ",\"native_node_type\":" + std::to_string(layer.actualNodeType) +
        ",\"visible\":" + (layer.visible ? "true" : "false") + "}";
}

std::string NativeIOLayerSnapshotsJson(const std::vector<NativeIO::LayerSnapshot>& layers) {
    std::string json = "[";
    for (std::size_t index = 0; index < layers.size(); ++index) {
        if (index != 0u) json += ",";
        json += NativeIOLayerSnapshotJson(layers[index]);
    }
    return json + "]";
}

std::string NativeIODocumentSnapshotJson(const NativeIO::DocumentMutationSnapshot& snapshot) {
    return "{\"document_path\":" + JsonString(snapshot.documentPath) +
        ",\"active_layer_uuid\":" + JsonString(snapshot.activeLayerUuid) +
        ",\"active_layer_name\":" + JsonString(snapshot.activeLayerName) +
        ",\"layer_count\":" + std::to_string(snapshot.layers.size()) +
        ",\"object_count\":" + std::to_string(snapshot.objectUuids.size()) + "}";
}

std::string NativeIOResultJson(const NativeIO::Result& result) {
    std::string json = "{\"operation\":" + JsonString(result.operation);
    json += ",\"path\":" + JsonString(result.canonicalPath);
    json += ",\"size_bytes\":" + std::to_string(result.sizeBytes);
    json += ",\"replaced_existing\":";
    json += result.replacedExisting ? "true" : "false";
    if (result.hasDocumentMutationReceipt) {
        const auto& mutation = result.documentMutationReceipt;
        json += ",\"document_mutation\":{\"verified\":";
        json += mutation.verified ? "true" : "false";
        json += ",\"before\":" + NativeIODocumentSnapshotJson(mutation.before);
        json += ",\"after\":" + NativeIODocumentSnapshotJson(mutation.after);
        json += ",\"created_object_uuids\":" + JsonStringArray(mutation.createdObjectUuids);
        json += ",\"deleted_object_uuids\":" + JsonStringArray(mutation.deletedObjectUuids);
        json += ",\"created_layers\":" + NativeIOLayerSnapshotsJson(mutation.createdLayers);
        json += ",\"deleted_layers\":" + NativeIOLayerSnapshotsJson(mutation.deletedLayers);
        json += ",\"changed_layers\":" + NativeIOLayerSnapshotsJson(mutation.changedLayers);
        json += ",\"active_layer_changed\":";
        json += mutation.activeLayerChanged ? "true" : "false";
        json += "}";
    }
    json += "}";
    return json;
}

NativeIO::OutputTarget ParseOutputTarget(const Params& params) {
    NativeIO::OutputTarget output;
    output.absolutePath = GetStringParam(params, "file_path");
    const bool replace = GetBoolParam(params, "replace", false);
    output.overwritePolicy = replace
        ? NativeIO::OverwritePolicy::Replace
        : NativeIO::OverwritePolicy::FailIfExists;
    output.replaceConfirmation = GetStringParam(params, "replace_confirmation");
    return output;
}

std::string HandleNativeIO(const std::string& action, const Params& params) {
    if (action == "export_image" || action == "capture_view") {
        if (action == "capture_view" &&
            (GetBoolParam(params, "fit_to_objects", false) ||
             GetBoolParam(params, "clear_selection", false))) {
            ViewDocument::SetViewRequest viewRequest;
            viewRequest.fitToObjects = GetBoolParam(params, "fit_to_objects", false);
            viewRequest.clearSelection = GetBoolParam(params, "clear_selection", false);
            ViewDocument::SetView(viewRequest);
        }
        NativeIO::ImageExportRequest request;
        request.output = ParseOutputTarget(params);
        request.updateViewports = GetBoolParam(params, "update_viewports", true);
        request.resetPlugInObjects = GetBoolParam(params, "reset_plugin_objects", true);
        request.exportGeoreferencing = GetBoolParam(params, "export_georeferencing", false);
        return NativeIOResultJson(action == "capture_view"
            ? NativeIO::CaptureView(request)
            : NativeIO::ExportImage(request));
    }
    if (action == "export_pdf") {
        NativeIO::PDFExportRequest request;
        request.output = ParseOutputTarget(params);
        request.currentViewOnly = GetBoolParam(params, "current_view_only", false);
        request.resolutionDpi = GetBoundedIntParam(params, "resolution_dpi", 300, 72, 2400);
        request.updateViewports = GetBoolParam(params, "update_viewports", true);
        request.resetPlugInObjects = GetBoolParam(params, "reset_plugin_objects", true);
        request.recalculateWorksheets = GetBoolParam(params, "recalculate_worksheets", true);
        return NativeIOResultJson(NativeIO::ExportPDF(request));
    }
    if (action == "export_vectorworks_document") {
        NativeIO::VectorworksExportRequest request;
        request.output = ParseOutputTarget(params);
        request.targetFileVersion = static_cast<short>(GetBoundedIntParam(
            params, "target_file_version", 29, 1, std::numeric_limits<short>::max()));
        return NativeIOResultJson(NativeIO::ExportVectorworksDocument(request));
    }
    if (action == "import_dwg") {
        NativeIO::DWGImportRequest request;
        request.absolutePath = GetStringParam(params, "file_path");
        return NativeIOResultJson(NativeIO::ImportDWG(request));
    }
    if (action == "export_dwg") {
        NativeIO::DWGExportRequest request;
        request.output = ParseOutputTarget(params);
        request.updateViewports = GetBoolParam(params, "update_viewports", true);
        request.resetPlugInObjects = GetBoolParam(params, "reset_plugin_objects", true);
        request.recalculateWorksheets = GetBoolParam(params, "recalculate_worksheets", true);
        return NativeIOResultJson(NativeIO::ExportDWG(request));
    }
    throw std::invalid_argument("unsupported native I/O action: " + action);
}

std::string ResourceRecordsJson(
    const std::vector<ResourceWorksheets::ResourceRecord>& records,
    const std::string& query) {
    const std::string lowerQuery = ToLower(query);
    std::string json = "[";
    bool first = true;
    for (const auto& record : records) {
        if (!lowerQuery.empty() && ToLower(record.name).find(lowerQuery) == std::string::npos) {
            continue;
        }
        if (!first) json += ",";
        first = false;
        json += "{\"name\":" + JsonString(record.name) +
            ",\"native_node_type\":" + std::to_string(record.actualNodeType) + "}";
    }
    json += "]";
    return json;
}

std::string HandleResources(const Params& params) {
    const std::string query = GetStringParam(params, "query");
    std::string json = "{\"symbols\":" + ResourceRecordsJson(
        ResourceWorksheets::ListSymbolDefinitions(*gSDK), query);
    const auto worksheets = ResourceWorksheets::ListWorksheets(*gSDK);
    json += ",\"worksheets\":[";
    bool first = true;
    for (const auto& worksheet : worksheets) {
        if (!query.empty() && ToLower(worksheet.name).find(ToLower(query)) == std::string::npos) continue;
        if (!first) json += ",";
        first = false;
        json += "{\"name\":" + JsonString(worksheet.name) +
            ",\"rows\":" + std::to_string(worksheet.rowCount) +
            ",\"columns\":" + std::to_string(worksheet.columnCount) +
            ",\"native_node_type\":" + std::to_string(worksheet.actualNodeType) + "}";
    }
    json += "]}";
    return json;
}

std::string HandleSymbol(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    if (action == "list") {
        return "{\"symbols\":" + ResourceRecordsJson(
            ResourceWorksheets::ListSymbolDefinitions(*gSDK), GetStringParam(params, "query")) + "}";
    }
    if (action == "insert") {
        ResourceWorksheets::SymbolInsertionRequest request;
        request.definitionName = GetStringParam(params, "definition_name");
        request.x = GetFiniteNumberParam(params, "x", 0.0);
        request.y = GetFiniteNumberParam(params, "y", 0.0);
        request.rotationDegrees = GetFiniteNumberParam(params, "rotation_deg", 0.0);
        Transactions::TransactionOptions options;
        options.expectedArtifactCount = 1u;
        options.sdkManagedRegistrationFamilies = {Transactions::ObjectFamily::Symbol};
        Transactions::NativeTransaction transaction(
            *gSDK,
            TXString("Vectorworks MCP insert symbol"),
            std::move(options));
        try {
            const auto receipt = ResourceWorksheets::InsertSymbol(*gSDK, request);
            UnregisteredCreatedObjectGuard guard(receipt.handle);
            const auto artifact = transaction.AdoptFinal(
                receipt.handle,
                Transactions::ObjectFamily::Symbol,
                kSymbolNode,
                [definitionName = receipt.definitionName](MCObjectHandle verifiedSymbol) {
                    if (!verifiedSymbol || gSDK->GetObjectTypeN(verifiedSymbol) != kSymbolNode) {
                        throw std::runtime_error("inserted symbol changed native node type before commit");
                    }
                    MCObjectHandle definition = gSDK->GetDefinition(verifiedSymbol);
                    TXString actualName;
                    if (definition) {
                        gSDK->GetObjectName(definition, actualName);
                    }
                    if (!definition || gSDK->GetObjectTypeN(definition) != kSymDefNode ||
                        TxToUtf8(actualName) != definitionName) {
                        throw std::runtime_error("inserted symbol lost its exact definition before commit");
                    }
                });
            (void) artifact;
            guard.Release();
            transaction.Commit();
            return "{\"inserted\":true,\"definition_name\":" + JsonString(receipt.definitionName) +
                ",\"native_node_type\":" + std::to_string(receipt.actualNodeType) +
                ",\"object\":" + ObjectJson(receipt.handle) + "}";
        } catch (...) {
            transaction.RollbackAndRethrow(std::current_exception());
        }
    }
    throw std::invalid_argument("symbol.action must be list or insert");
}

std::string WorksheetCellJson(const ResourceWorksheets::WorksheetCellSnapshot& cell) {
    return "{\"worksheet\":" + JsonString(cell.worksheetName) +
        ",\"row\":" + std::to_string(cell.row) +
        ",\"column\":" + std::to_string(cell.column) +
        ",\"formula\":" + JsonString(cell.formula) +
        ",\"displayed_value\":" + JsonString(cell.displayedValue) + "}";
}

std::string HandleWorksheet(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    if (action == "list") {
        const std::string query = ToLower(GetStringParam(params, "query"));
        const auto worksheets = ResourceWorksheets::ListWorksheets(*gSDK);
        std::string json = "{\"worksheets\":[";
        bool first = true;
        for (const auto& worksheet : worksheets) {
            if (!query.empty() && ToLower(worksheet.name).find(query) == std::string::npos) continue;
            if (!first) json += ",";
            first = false;
            json += "{\"name\":" + JsonString(worksheet.name) +
                ",\"rows\":" + std::to_string(worksheet.rowCount) +
                ",\"columns\":" + std::to_string(worksheet.columnCount) + "}";
        }
        return json + "]}";
    }
    const std::string worksheetName = GetStringParam(params, "worksheet_name");
    const int row = GetRequiredBoundedIntParam(params, "row", 1, 32767);
    const int column = GetRequiredBoundedIntParam(params, "column", 1, 32767);
    if (action == "read") {
        return WorksheetCellJson(ResourceWorksheets::ReadWorksheetCell(
            *gSDK, worksheetName, row, column));
    }
    if (action == "write") {
        ResourceWorksheets::WorksheetCellWriteRequest request{
            worksheetName, row, column, GetStringParam(params, "formula")};
        MCObjectHandle worksheetHandle = nullptr;
        for (const auto& item : ResourceWorksheets::ListWorksheets(*gSDK)) {
            if (item.name == worksheetName) {
                if (worksheetHandle) throw std::invalid_argument("worksheet name is ambiguous");
                worksheetHandle = item.handle;
            }
        }
        if (!worksheetHandle) throw std::invalid_argument("worksheet was not found by exact name");
        gSDK->SupportUndoAndRemove();
        gSDK->SetUndoMethod(kUndoSwapObjects);
        gSDK->NameUndoEvent(TXString("Vectorworks MCP worksheet write"));
        try {
            if (!gSDK->AddBeforeSwapObject(worksheetHandle)) {
                throw std::runtime_error("Vectorworks failed to register the worksheet before mutation");
            }
            const auto receipt = ResourceWorksheets::WriteWorksheetCell(*gSDK, request);
            if (!gSDK->AddAfterSwapObject(receipt.after.worksheetHandle)) {
                throw std::runtime_error("Vectorworks failed to register the worksheet after mutation");
            }
            EndUndoEventOrThrow("worksheet write");
            return "{\"verified\":true,\"before\":" + WorksheetCellJson(receipt.before) +
                ",\"after\":" + WorksheetCellJson(receipt.after) + "}";
        } catch (...) {
            gSDK->UndoAndRemove();
            throw;
        }
    }
    throw std::invalid_argument("worksheet.action must be list, read, or write");
}

std::string ViewStateJson(const ViewDocument::ViewState& state) {
    return "{\"standard_view\":" + std::to_string(state.standardView) +
        ",\"projection\":" + std::to_string(state.projection) +
        ",\"render_mode\":" + std::to_string(state.renderMode) +
        ",\"fit_to_objects_applied\":" + (state.fitToObjectsApplied ? "true" : "false") +
        ",\"selection_cleared\":" + (state.selectionCleared ? "true" : "false") + "}";
}

std::string HandleView(const std::string& action, const Params& params) {
    if (action == "get_view") return ViewStateJson(ViewDocument::GetView());
    ViewDocument::SetViewRequest request;
    request.setStandardView = params.find("standard_view") != params.end();
    request.standardView = static_cast<short>(GetBoundedIntParam(params, "standard_view", 0, 0, 16));
    request.setProjection = params.find("projection") != params.end();
    request.projection = static_cast<short>(GetBoundedIntParam(params, "projection", 0, 0, 6));
    request.setRenderMode = params.find("render_mode") != params.end();
    request.renderMode = static_cast<short>(GetBoundedIntParam(params, "render_mode", 0, 0, 15));
    request.fitToObjects = GetBoolParam(params, "fit_to_objects", false);
    request.clearSelection = GetBoolParam(params, "clear_selection", false);
    return ViewStateJson(ViewDocument::SetView(request));
}

std::string HandleDocumentLifecycle(
    const std::string& action,
    const Params& params,
    const std::string& requestId) {
    ViewDocument::DocumentResult result;
    if (action == "save_document") {
        result = ViewDocument::SaveDocument(
            GetStringParam(params, "file_path"), GetStringParam(params, "replace_confirmation"));
    } else if (action == "open_document") {
        auto prepared = ViewDocument::PrepareOpenDocument(
            GetStringParam(params, "file_path"), GetStringParam(params, "replace_dirty_confirmation"));
        const std::string requestedPath = prepared.canonicalPath;
        StageDeferredDocumentOpen(requestId, std::move(prepared));
        result = {
            "open_document",
            requestedPath,
            false,
            requestedPath,
            "",
            ViewDocument::CommitState::Accepted,
        };
    } else {
        throw std::invalid_argument("unsupported document lifecycle action: " + action);
    }
    return "{\"operation\":" + JsonString(result.operation) +
        ",\"path\":" + JsonString(result.canonicalPath) +
        ",\"saved\":" + (result.saved ? "true" : "false") +
        ",\"requested_path\":" + JsonString(result.requestedPath) +
        ",\"active_path\":" + JsonString(result.activePath) +
        ",\"commit_state\":" + JsonString(ViewDocument::CommitStateName(result.commitState)) + "}";
}

std::string CanonicalCreateObjectType(std::string objectType) {
    objectType = ToLower(objectType);
    if (objectType == "rectangle" || objectType == "box") {
        return "rect";
    }
    if (objectType == "dimension" || objectType == "linear_dimension") {
        return "linear_dimension";
    }
    if (objectType == "polyline") {
        return "polygon";
    }
    return objectType;
}

bool HasParam(const Params& params, const std::string& key) {
    return params.find(key) != params.end();
}

double GetFiniteNumberParamAlias(
    const Params& params,
    const std::string& preferredKey,
    const std::string& aliasKey,
    double defaultValue) {
    if (HasParam(params, preferredKey)) {
        return GetFiniteNumberParam(params, preferredKey, defaultValue);
    }
    return GetFiniteNumberParam(params, aliasKey, defaultValue);
}

double RequirePositiveNumber(double value, const std::string& label) {
    if (value <= 0.0) {
        throw std::invalid_argument(label + " must be > 0");
    }
    return value;
}

std::string JsonStringArray(const std::vector<std::string>& values) {
    std::string json = "[";
    for (std::size_t index = 0; index < values.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        json += JsonString(values[index]);
    }
    json += "]";
    return json;
}

void ValidatePrimitiveSpec(const PrimitiveSpec& spec, const std::string& label) {
    if (spec.objectType.empty()) {
        throw std::invalid_argument(label + ".object_type is required");
    }
    if (spec.objectType == "rect" || spec.objectType == "oval") {
        if (spec.x1 == spec.x2 || spec.y1 == spec.y2) {
            throw std::invalid_argument(label + " " + spec.objectType + " bounds must have non-zero width and height");
        }
        return;
    }
    if (spec.objectType == "line") {
        if (spec.x1 == spec.x2 && spec.y1 == spec.y2) {
            throw std::invalid_argument(label + " line endpoints must not be identical");
        }
        return;
    }
    if (spec.objectType == "circle") {
        if (spec.radius <= 0.0) {
            throw std::invalid_argument(label + ".radius must be > 0");
        }
        return;
    }
    if (spec.objectType == "arc") {
        if (spec.radius <= 0.0) {
            throw std::invalid_argument(label + ".radius must be > 0");
        }
        if (spec.sweepAngle == 0.0) {
            throw std::invalid_argument(label + ".sweep_angle must not be 0");
        }
        return;
    }
    if (spec.objectType == "polygon" || spec.objectType == "slab" ||
        spec.objectType == "roof" || spec.objectType == "space") {
        const std::size_t minimum = spec.closed ? 3u : 2u;
        if (spec.points.size() < minimum) {
            throw std::invalid_argument(
                label + " " + (spec.closed ? "closed polygon" : "open polyline") +
                " requires at least " + std::to_string(minimum) + " points");
        }
        for (std::size_t index = 1; index < spec.points.size(); ++index) {
            if (spec.points[index - 1].x == spec.points[index].x &&
                spec.points[index - 1].y == spec.points[index].y) {
                throw std::invalid_argument(label + ".points contains consecutive duplicate points");
            }
        }
        if (spec.objectType == "slab") {
            RequirePositiveNumber(spec.thickness, label + ".thickness");
        }
        if (spec.objectType == "space") {
            RequirePositiveNumber(spec.height, label + ".height");
        }
        if (spec.objectType == "roof") {
            RequirePositiveNumber(spec.thickness, label + ".thickness");
            if (spec.slope <= 0.0 || spec.slope > 89.0) {
                throw std::invalid_argument(label + ".slope must be > 0 and <= 89 degrees");
            }
            if (spec.overhang < 0.0 || spec.bearingInset < 0.0 || spec.verticalMiter < 0.0) {
                throw std::invalid_argument(label + " roof offsets must be >= 0");
            }
            if (spec.miterType < 1 || spec.miterType > 4) {
                throw std::invalid_argument(label + ".miter_type must be between 1 and 4");
            }
        }
        return;
    }
    if (spec.objectType == "wall") {
        if (spec.x1 == spec.x2 && spec.y1 == spec.y2) {
            throw std::invalid_argument(label + " wall endpoints must not be identical");
        }
        RequirePositiveNumber(spec.thickness, label + ".thickness");
        RequirePositiveNumber(spec.height, label + ".height");
        return;
    }
    if (spec.objectType == "parametric") {
        if (spec.pluginName.empty()) {
            throw std::invalid_argument(label + ".plugin_name is required");
        }
        if (spec.requireWallHost && spec.wallUuid.empty()) {
            throw std::invalid_argument(label + ".wall_uuid is required for hosted placement");
        }
        return;
    }
    if (spec.objectType == "door" || spec.objectType == "window") {
        if (!spec.hasExplicitX || !spec.hasExplicitY) {
            throw std::invalid_argument(label + ".x1 and y1 are required for hosted placement");
        }
        if (!spec.hasExplicitWidth || !spec.hasExplicitHeight) {
            throw std::invalid_argument(label + ".width and height are required");
        }
        RequirePositiveNumber(spec.width, label + ".width");
        RequirePositiveNumber(spec.height, label + ".height");
        if (spec.wallUuid.empty()) {
            throw std::invalid_argument(label + ".wall_uuid is required for hosted placement");
        }
        if (!spec.requireWallHost) {
            throw std::invalid_argument(label + ".require_wall_host must be true");
        }
        const std::string expectedPlugin = spec.objectType == "door" ? "Door" : "Window";
        if (!spec.pluginName.empty() && spec.pluginName != expectedPlugin) {
            throw std::invalid_argument(
                label + ".plugin_name must be the exact universal name " + expectedPlugin);
        }
        if (!spec.parametricValues.empty()) {
            throw std::invalid_argument(
                label + " dedicated opening dimensions cannot be overridden by generic parameters");
        }
        if (spec.descriptorFingerprint.empty()) {
            throw std::invalid_argument(
                label + ".descriptor_fingerprint is required from parametric schema discovery");
        }
        if (spec.objectType == "window" && spec.sillHeight < 0.0) {
            throw std::invalid_argument(label + ".sill_height must be >= 0");
        }
        if (spec.objectType == "window" && !spec.hasExplicitSill) {
            throw std::invalid_argument(label + ".sill_height is required");
        }
        if (spec.objectType == "door" && spec.hasExplicitSill) {
            throw std::invalid_argument(label + ".sill_height is supported only for window");
        }
        return;
    }
    if (spec.objectType == "symbol") {
        if (spec.symbolDefinitionName.empty()) {
            throw std::invalid_argument(label + ".definition_name is required");
        }
        return;
    }
    if (spec.objectType == "text") {
        if (spec.text.empty()) {
            throw std::invalid_argument(label + ".text is required");
        }
        if (spec.width < 0.0) {
            throw std::invalid_argument(label + ".width must be >= 0");
        }
        if (spec.textSize < 0.0) {
            throw std::invalid_argument(label + ".text_size must be >= 0");
        }
        return;
    }
    if (spec.objectType == "linear_dimension") {
        if (!spec.name.empty()) {
            throw std::invalid_argument(
                label + ".name is not supported for native linear dimensions");
        }
        if (spec.x1 == spec.x2 && spec.y1 == spec.y2) {
            throw std::invalid_argument(label + " linear_dimension endpoints must not be identical");
        }
        if (spec.dimensionType < 0 || spec.dimensionType > 2) {
            throw std::invalid_argument(label + ".dimension_type must be 0, 1, or 2");
        }
        return;
    }
    throw std::invalid_argument("unsupported create object type for native bridge: " + spec.objectType);
}

PrimitiveSpec ParsePrimitiveSpec(const Params& params, const std::string& label) {
    PrimitiveSpec spec;
    spec.hasExplicitX = HasParam(params, "x1") || HasParam(params, "start_x");
    spec.hasExplicitY = HasParam(params, "y1") || HasParam(params, "start_y");
    spec.hasExplicitWidth = HasParam(params, "width");
    spec.hasExplicitHeight = HasParam(params, "height");
    spec.hasExplicitSill = HasParam(params, "sill_height") ||
        HasParam(params, "window_sill_height");
    spec.objectType = GetStringParam(params, "object_type");
    if (spec.objectType.empty()) {
        spec.objectType = GetStringParam(params, "type");
    }
    if (spec.objectType.empty()) {
        if (label == "create_wall") {
            spec.objectType = "wall";
        } else if (label == "create_text") {
            spec.objectType = "text";
        } else if (label == "create_linear_dimension") {
            spec.objectType = "linear_dimension";
        }
    }
    spec.objectType = CanonicalCreateObjectType(spec.objectType);
    spec.x1 = GetFiniteNumberParamAlias(params, "x1", "start_x", 0.0);
    spec.y1 = GetFiniteNumberParamAlias(params, "y1", "start_y", 0.0);
    spec.x2 = GetFiniteNumberParamAlias(params, "x2", "end_x", 100.0);
    spec.y2 = GetFiniteNumberParamAlias(params, "y2", "end_y", 100.0);
    spec.radius = GetFiniteNumberParam(params, "radius", 50.0);
    spec.startAngle = GetFiniteNumberParam(params, "start_angle", 0.0);
    spec.sweepAngle = GetFiniteNumberParam(params, "sweep_angle", 90.0);
    spec.height = GetFiniteNumberParam(params, "height", 3000.0);
    spec.thickness = GetFiniteNumberParam(params, "thickness", 200.0);
    spec.elevation = GetFiniteNumberParam(params, "elevation", GetFiniteNumberParam(params, "bearing_height", 0.0));
    spec.slope = GetFiniteNumberParam(params, "slope", 30.0);
    spec.overhang = GetFiniteNumberParam(params, "overhang", 500.0);
    spec.bearingInset = GetFiniteNumberParam(params, "bearing_inset", 0.0);
    spec.verticalMiter = GetFiniteNumberParam(params, "vertical_miter", 0.0);
    spec.miterType = GetBoundedIntParam(params, "miter_type", 1, 1, 4);
    spec.generateGableWalls = GetBoolParam(params, "generate_gable_walls", false);
    spec.pluginName = GetStringParam(params, "plugin_name");
    spec.descriptorFingerprint = GetStringParam(params, "descriptor_fingerprint");
    spec.wallUuid = GetStringParam(params, "wall_uuid");
    spec.requireWallHost = GetBoolParam(params, "require_wall_host", false);
    const int parameterCount = GetBoundedIntParam(params, "parameter_count", 0, 0, 256);
    spec.parametricValues.reserve(static_cast<std::size_t>(parameterCount));
    for (int parameterIndex = 1; parameterIndex <= parameterCount; ++parameterIndex) {
        const std::string prefix = "parameter_" + std::to_string(parameterIndex) + "_";
        ParametricValue value;
        value.universalName = GetStringParam(params, prefix + "name");
        const std::string kind = ToLower(GetStringParam(params, prefix + "type"));
        if (kind == "integer") {
            value.kind = ParametricValueKind::Integer;
            value.integerValue = static_cast<std::int32_t>(GetBoundedIntParam(
                params, prefix + "integer", 0, std::numeric_limits<int>::min(), std::numeric_limits<int>::max()));
        } else if (kind == "boolean") {
            value.kind = ParametricValueKind::Boolean;
            value.booleanValue = GetBoolParam(params, prefix + "boolean", false);
        } else if (kind == "real") {
            value.kind = ParametricValueKind::Real;
            value.realValue = GetFiniteNumberParam(params, prefix + "real", 0.0);
        } else if (kind == "string") {
            value.kind = ParametricValueKind::String;
            value.stringValue = GetStringParam(params, prefix + "string");
        } else {
            throw std::invalid_argument(prefix + "type must be integer, boolean, real, or string");
        }
        spec.parametricValues.push_back(std::move(value));
    }
    spec.width = GetFiniteNumberParam(params, "width", 0.0);
    spec.sillHeight = GetFiniteNumberParam(
        params,
        "sill_height",
        GetFiniteNumberParam(params, "window_sill_height", 0.0));
    spec.rotation = GetFiniteNumberParam(params, "rotation", 0.0);
    spec.textSize = GetFiniteNumberParam(params, "text_size", GetFiniteNumberParam(params, "size", 0.0));
    spec.dimensionOffset = GetFiniteNumberParam(params, "offset", GetFiniteNumberParam(params, "dimension_offset", 300.0));
    spec.dimensionTextOffset = GetFiniteNumberParam(params, "text_offset", 0.0);
    spec.directionX = GetFiniteNumberParam(params, "direction_x", 0.0);
    spec.directionY = GetFiniteNumberParam(params, "direction_y", 0.0);
    spec.dimensionType = GetBoundedIntParam(params, "dimension_type", 1, 0, 2);
    spec.fixedSizeText = GetBoolParam(params, "fixed_size", false);
    spec.wrapText = GetBoolParam(params, "wrap", false);
    spec.closed = GetBoolParam(params, "closed", true);
    if (spec.objectType == "polygon" || spec.objectType == "slab" ||
        spec.objectType == "roof" || spec.objectType == "space") {
        std::string pointsJson;
        const auto points = params.find("points");
        if (points != params.end() && points->second.type != ParamValue::Type::Null) {
            pointsJson = GetRawJsonParam(params, "points");
        } else {
            pointsJson = GetStringParam(params, "points_json");
        }
        if (pointsJson.empty()) {
            throw std::invalid_argument(label + ".points is required");
        }
        spec.points = PointListJsonParser(pointsJson, label + ".points").Parse();
        if (spec.closed && spec.points.size() > 1u &&
            spec.points.front().x == spec.points.back().x &&
            spec.points.front().y == spec.points.back().y) {
            spec.points.pop_back();
        }
    }
    spec.text = GetStringParam(params, "text");
    spec.styleName = GetStringParam(params, "style_name");
    spec.name = GetStringParam(params, "name");
    spec.className = GetStringParam(params, "class_name");
    spec.roomId = GetStringParam(params, "room_id");
    spec.symbolDefinitionName = GetStringParam(params, "definition_name");
    ValidatePrimitiveSpec(spec, label);
    return spec;
}

void ApplyObjectNameAndClass(MCObjectHandle object, const PrimitiveSpec& spec) {
    if (!object) {
        return;
    }
    if (!spec.name.empty()) {
        gSDK->SetObjectName(object, TXString(spec.name.c_str()));
    }
    if (!spec.className.empty()) {
        InternalIndex classId = gSDK->ClassNameToID(TXString(spec.className.c_str()));
        if (!gSDK->ValidClass(classId)) {
            classId = gSDK->AddClass(TXString(spec.className.c_str()));
        }
        if (gSDK->ValidClass(classId)) {
            gSDK->SetObjectClass(object, classId);
        }
    }
}

InternalIndex ResolveOrCreateClass(const std::string& className) {
    InternalIndex classId = gSDK->ClassNameToID(TXString(className.c_str()));
    if (!gSDK->ValidClass(classId)) {
        classId = gSDK->AddClass(TXString(className.c_str()));
    }
    if (!gSDK->ValidClass(classId)) {
        throw std::runtime_error("Vectorworks rejected class: " + className);
    }
    return classId;
}

MCObjectHandle CreateLinearDimensionWithClass(
    const PrimitiveSpec& spec,
    InternalIndex* expectedClassId) {
    InternalIndex requestedClassId = 0;
    const Sint32 previousClassId = static_cast<Sint32>(gSDK->GetDimensionClassID());
    bool changedPreference = false;

    if (!spec.className.empty()) {
        requestedClassId = ResolveOrCreateClass(spec.className);
        const Sint32 requestedClassRef = static_cast<Sint32>(requestedClassId);
        if (requestedClassRef != previousClassId) {
            if (!gSDK->SetProgramVariable(varDefaultDimensionClassID, &requestedClassRef)) {
                throw std::runtime_error(
                    "Vectorworks rejected the requested dimension class preference");
            }
            changedPreference = true;
        }
    }

    MCObjectHandle object = nullptr;
    std::exception_ptr creationFailure;
    try {
        object = gSDK->CreateLinearDimension(
            WorldPt(spec.x1, spec.y1),
            WorldPt(spec.x2, spec.y2),
            spec.dimensionOffset,
            spec.dimensionTextOffset,
            Vector2(spec.directionX, spec.directionY),
            static_cast<short>(spec.dimensionType));
    } catch (...) {
        creationFailure = std::current_exception();
    }

    if (changedPreference &&
        !gSDK->SetProgramVariable(varDefaultDimensionClassID, &previousClassId)) {
        if (object) {
            gSDK->DeleteObject(object, false);
        }
        throw std::runtime_error(
            "Vectorworks did not restore the previous dimension class preference");
    }
    if (creationFailure) {
        std::rethrow_exception(creationFailure);
    }
    if (object && requestedClassId != 0 &&
        gSDK->GetObjectClass(object) != requestedClassId) {
        gSDK->DeleteObject(object, false);
        throw std::runtime_error(
            "Vectorworks did not create the dimension in the requested class");
    }
    if (expectedClassId) {
        *expectedClassId = requestedClassId;
    }
    return object;
}

void ApplyWallStyleIfRequested(MCObjectHandle wall, const PrimitiveSpec& spec, std::vector<std::string>* warnings) {
    if (!wall || spec.styleName.empty()) {
        return;
    }
    InternalIndex styleIndex = 0;
    if (!gSDK->NameToInternalIndexN(TXString(spec.styleName.c_str()), styleIndex) || styleIndex == 0) {
        if (warnings) {
            warnings->push_back("wall style not found: " + spec.styleName);
        }
        return;
    }
    MCObjectHandle styleHandle = gSDK->InternalIndexToHandle(styleIndex);
    if (!styleHandle || gSDK->GetObjectTypeN(styleHandle) != kWallStyleNode) {
        if (warnings) {
            warnings->push_back("resource is not a wall style: " + spec.styleName);
        }
        return;
    }
    if (!gSDK->SetWallStyle(wall, styleIndex, 0, 0) && warnings) {
        warnings->push_back("Vectorworks rejected wall style: " + spec.styleName);
    }
}

MCObjectHandle CreatePrimitiveFromSpec(
    const PrimitiveSpec& spec,
    Transactions::NativeTransaction& transaction,
    Transactions::ArtifactId* transactionArtifact,
    std::vector<std::string>* warnings = nullptr) {
    EnsureWritableLayer();

    MCObjectHandle object = nullptr;
    InternalIndex expectedDimensionClassId = 0;
    std::optional<Transactions::ArtifactId> adoptedArtifact;
    std::optional<Transactions::ExternalMutationId> pendingExternalAfter;
    MCObjectHandle pendingExternalHandle = nullptr;
    if (spec.objectType == "rect") {
        object = gSDK->CreateRectangle(WorldRect(WorldPt(spec.x1, spec.y1), WorldPt(spec.x2, spec.y2)));
    } else if (spec.objectType == "oval") {
        object = gSDK->CreateOval(WorldRect(WorldPt(spec.x1, spec.y1), WorldPt(spec.x2, spec.y2)));
    } else if (spec.objectType == "circle") {
        object = gSDK->CreateOval(WorldRect(WorldPt(spec.x1, spec.y1), spec.radius));
    } else if (spec.objectType == "line") {
        object = gSDK->CreateLine(WorldPt(spec.x1, spec.y1), WorldPt(spec.x2, spec.y2));
    } else if (spec.objectType == "arc") {
        object = gSDK->CreateArcN(WorldRect(WorldPt(spec.x1, spec.y1), spec.radius), spec.startAngle, spec.sweepAngle);
    } else if (spec.objectType == "polygon") {
        object = gSDK->CreatePolyshape();
        if (object) {
            for (const auto& point : spec.points) {
                gSDK->AddVertex(object, point, nullptr, vtCorner, 0, false);
            }
            gSDK->SetPolyShapeClose(object, spec.closed);
            gSDK->ResetObject(object);
        }
    } else if (spec.objectType == "slab") {
        BimObjects::SlabRequest request;
        request.elevation = spec.elevation;
        request.thickness = spec.thickness;
        request.styleName = spec.styleName;
        request.boundary.reserve(spec.points.size());
        for (const auto& point : spec.points) {
            request.boundary.push_back({point.x, point.y});
        }
        const auto receipt = BimObjects::CreateTrueSlab(*gSDK, request, transaction);
        object = receipt.handle;
        adoptedArtifact = receipt.transactionArtifact;
    } else if (spec.objectType == "roof") {
        BimObjects::RoofRequest request;
        request.slopeDegrees = spec.slope;
        request.projection = spec.overhang;
        request.eaveHeight = spec.elevation;
        request.bearingInset = spec.bearingInset;
        request.thickness = spec.thickness;
        request.miterType = static_cast<short>(spec.miterType);
        request.verticalMiter = spec.verticalMiter;
        request.generateGableWalls = spec.generateGableWalls;
        request.boundary.reserve(spec.points.size());
        for (const auto& point : spec.points) {
            request.boundary.push_back({point.x, point.y});
        }
        const auto receipt = BimObjects::CreateTrueRoof(*gSDK, request, transaction);
        object = receipt.handle;
        adoptedArtifact = receipt.transactionArtifact;
    } else if (spec.objectType == "space") {
        SpaceCreateSpec request;
        request.height = spec.height;
        request.name = spec.name;
        request.roomId = spec.roomId;
        request.boundary.reserve(spec.points.size());
        for (const auto& point : spec.points) {
            request.boundary.push_back({point.x, point.y});
        }
        const auto receipt = CreateVerifiedSpace(request, transaction);
        object = receipt.object;
        adoptedArtifact = receipt.transactionArtifact;
    } else if (spec.objectType == "door" || spec.objectType == "window") {
        MCObjectHandle wall = gSDK->GetObjectByUuid(TXString(spec.wallUuid.c_str()));
        if (!wall || gSDK->GetObjectTypeN(wall) != kWallNode) {
            throw std::invalid_argument("host wall UUID was not found or is not a wall");
        }
        const auto wallMutation = transaction.TrackExternalBefore(
            wall, Transactions::ObjectFamily::Wall);
        BuiltInOpeningCreateSpec request;
        request.kind = spec.objectType == "door"
            ? BuiltInParametricKind::Door
            : BuiltInParametricKind::Window;
        request.expectedWall = wall;
        request.x = spec.x1;
        request.y = spec.y1;
        request.rotationDegrees = spec.rotation;
        request.width = spec.width;
        request.height = spec.height;
        request.windowSillHeight = spec.sillHeight;
        request.descriptorFingerprint = spec.descriptorFingerprint;
        const auto receipt = CreateVerifiedBuiltInOpening(request);
        object = receipt.object;
        UnregisteredCreatedObjectGuard openingGuard(object);
        adoptedArtifact = transaction.AdoptFinal(
            object,
            spec.objectType == "door"
                ? Transactions::ObjectFamily::Door
                : Transactions::ObjectFamily::Window,
            kParametricNode,
            [receipt](MCObjectHandle verifiedOpening) {
                VerifyBuiltInOpeningReceipt(verifiedOpening, receipt);
            });
        openingGuard.Release();
        transaction.TrackExternalAfter(wallMutation, wall);
    } else if (spec.objectType == "parametric") {
        MCObjectHandle wall = nullptr;
        std::optional<Transactions::ExternalMutationId> wallMutation;
        if (spec.requireWallHost) {
            wall = gSDK->GetObjectByUuid(TXString(spec.wallUuid.c_str()));
            if (!wall || gSDK->GetObjectTypeN(wall) != kWallNode) {
                throw std::invalid_argument("host wall UUID was not found or is not a wall");
            }
            wallMutation = transaction.TrackExternalBefore(
                wall, Transactions::ObjectFamily::Wall);
        }
        ParametricCreateSpec request;
        request.universalPluginName = spec.pluginName;
        request.x = spec.x1;
        request.y = spec.y1;
        request.rotationDegrees = spec.rotation;
        request.requireWallHost = spec.requireWallHost;
        request.expectedWall = wall;
        request.descriptorFingerprint = spec.descriptorFingerprint;
        request.parameters = spec.parametricValues;
        object = CreateVerifiedParametricObject(request);
        if (wallMutation) {
            pendingExternalAfter = *wallMutation;
            pendingExternalHandle = wall;
        }
    } else if (spec.objectType == "symbol") {
        object = PlaceVerifiedSymbol({
            spec.symbolDefinitionName,
            spec.x1,
            spec.y1,
            spec.rotation,
        });
    } else if (spec.objectType == "wall") {
        object = gSDK->CreateWall(WorldPt(spec.x1, spec.y1), WorldPt(spec.x2, spec.y2), spec.thickness);
        if (object) {
            gSDK->SetWallWidth(object, spec.thickness);
            gSDK->SetWallCornerHeights(object, spec.height, 0, spec.height, 0);
            ApplyWallStyleIfRequested(object, spec, warnings);
        }
    } else if (spec.objectType == "text") {
        object = gSDK->CreateTextBlock(
            TXString(spec.text.c_str()),
            WorldPt(spec.x1, spec.y1),
            spec.fixedSizeText,
            spec.width);
        if (object) {
            if (spec.width > 0.0) {
                gSDK->SetTextWidth(object, spec.width);
            }
            if (spec.wrapText || spec.width > 0.0) {
                gSDK->SetTextWrap(object, true);
            }
            if (spec.rotation != 0.0) {
                gSDK->SetTextOrientationN(object, spec.rotation, 0);
            }
            if (spec.textSize > 0.0) {
                double_gs points = spec.textSize;
                const WorldCoord charSize = gSDK->PagePointsToCoordLength(points);
                gSDK->SetTextSize(object, 0, static_cast<Sint32>(spec.text.size()), charSize);
            }
        }
    } else if (spec.objectType == "linear_dimension") {
        object = CreateLinearDimensionWithClass(spec, &expectedDimensionClassId);
    }

    if (!object) {
        throw std::runtime_error("Vectorworks did not return a handle for created " + spec.objectType);
    }

    UnregisteredCreatedObjectGuard unregisteredGuard(adoptedArtifact ? nullptr : object);
    if (spec.objectType != "linear_dimension") {
        ApplyObjectNameAndClass(object, spec);
    }
    if (!adoptedArtifact) {
        const short expectedNodeType = gSDK->GetObjectTypeN(object);
        Transactions::SemanticVerifier verifier;
        if (spec.objectType == "parametric") {
            const std::string expectedPlugin = spec.pluginName;
            const std::string expectedFingerprint = spec.descriptorFingerprint;
            const bool requiresWall = spec.requireWallHost;
            MCObjectHandle expectedWall = pendingExternalHandle;
            verifier = [expectedPlugin, expectedFingerprint, requiresWall, expectedWall](
                           MCObjectHandle verifiedObject) {
                const auto descriptor = DescribeParametricObject(verifiedObject);
                if (descriptor.universalPluginName != expectedPlugin ||
                    descriptor.descriptorFingerprint != expectedFingerprint) {
                    throw std::runtime_error(
                        "parametric object failed plugin/schema verification at commit");
                }
                if (requiresWall && !IsObjectHostedByWall(verifiedObject, expectedWall)) {
                    throw std::runtime_error(
                        "parametric object failed exact wall-host verification at commit");
                }
            };
        } else if (spec.objectType == "linear_dimension" && expectedDimensionClassId != 0) {
            const InternalIndex expectedClassId = expectedDimensionClassId;
            verifier = [expectedNodeType, expectedClassId](MCObjectHandle verifiedObject) {
                if (!verifiedObject || gSDK->GetObjectTypeN(verifiedObject) != expectedNodeType ||
                    !IsUserVisibleObjectType(expectedNodeType)) {
                    throw std::runtime_error(
                        "created dimension failed semantic node-type verification");
                }
                if (gSDK->GetObjectClass(verifiedObject) != expectedClassId) {
                    throw std::runtime_error(
                        "created dimension failed class verification at commit");
                }
            };
        } else {
            verifier = [expectedNodeType](MCObjectHandle verifiedObject) {
                if (!verifiedObject || gSDK->GetObjectTypeN(verifiedObject) != expectedNodeType ||
                    !IsUserVisibleObjectType(expectedNodeType)) {
                    throw std::runtime_error(
                        "created object failed semantic node-type verification");
                }
            };
        }
        adoptedArtifact = transaction.AdoptFinal(
            object,
            spec.objectType == "wall"
                ? Transactions::ObjectFamily::Wall
                : spec.objectType == "parametric"
                    ? Transactions::ObjectFamily::Parametric
                : spec.objectType == "symbol"
                    ? Transactions::ObjectFamily::Symbol
                    : Transactions::ObjectFamily::Simple,
            expectedNodeType,
            std::move(verifier));
        unregisteredGuard.Release();
    }
    if (pendingExternalAfter) {
        transaction.TrackExternalAfter(*pendingExternalAfter, pendingExternalHandle);
    }
    if (!adoptedArtifact) {
        throw std::runtime_error("created object left the factory without transaction ownership");
    }
    if (transactionArtifact) {
        *transactionArtifact = *adoptedArtifact;
    }
    return object;
}

std::string CreatedPrimitiveJson(const CreatedPrimitive& created) {
    std::string json = "{\"index\":";
    json += std::to_string(created.index);
    json += ",\"type\":";
    json += JsonString(created.objectType);
    json += ",\"handle\":";
    json += JsonString(HandleId(created.handle));
    const auto uuid = ObjectUuidString(created.handle);
    if (!uuid.empty()) {
        json += ",\"uuid\":";
        json += JsonString(uuid);
    }
    if (!created.warnings.empty()) {
        json += ",\"warnings\":";
        json += JsonStringArray(created.warnings);
    }
    if (created.objectType == "polygon") {
        json += ",\"vertex_count\":";
        json += std::to_string(created.vertexCount);
        json += ",\"closed\":";
        json += created.closed ? "true" : "false";
    }
    json += ",\"native_node_type\":";
    json += std::to_string(created.nativeNodeType);
    json += ",\"verified\":";
    json += created.semanticallyVerified ? "true" : "false";
    json += ",\"object\":";
    json += ObjectJson(created.handle);
    json += "}";
    return json;
}

Transactions::ObjectFamily ExternalMutationFamily(MCObjectHandle object) {
    if (!object || !gSDK) {
        return Transactions::ObjectFamily::Simple;
    }
    const short nodeType = gSDK->GetObjectTypeN(object);
    if (nodeType == kSlabNode) {
        return Transactions::ObjectFamily::Slab;
    }
    if (nodeType == kRoofContainerNode) {
        return Transactions::ObjectFamily::Roof;
    }
    if (nodeType == kWallNode) {
        return Transactions::ObjectFamily::Wall;
    }
    if (nodeType != kParametricNode) {
        return Transactions::ObjectFamily::Simple;
    }
    try {
        const std::string plugin = DescribeParametricObject(object).universalPluginName;
        if (plugin == "Space") return Transactions::ObjectFamily::Space;
        if (plugin == "Slab") return Transactions::ObjectFamily::Slab;
        if (plugin == "Door") return Transactions::ObjectFamily::Door;
        if (plugin == "Window") return Transactions::ObjectFamily::Window;
        return Transactions::ObjectFamily::Parametric;
    } catch (...) {
        // An unclassifiable parametric object must retain strict explicit
        // after-state registration instead of inheriting compound BIM policy.
    }
    return Transactions::ObjectFamily::Simple;
}

std::string CreatedPrimitiveListJson(const std::vector<CreatedPrimitive>& created) {
    std::string json = "[";
    for (std::size_t index = 0; index < created.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        json += CreatedPrimitiveJson(created[index]);
    }
    json += "]";
    return json;
}

std::string CompactCreatedPrimitiveListJson(const std::vector<CreatedPrimitive>& created) {
    std::string json = "[";
    for (std::size_t index = 0; index < created.size(); ++index) {
        if (index != 0u) {
            json += ",";
        }
        const auto& item = created[index];
        json += "{\"index\":";
        json += std::to_string(item.index);
        json += ",\"type\":";
        json += JsonString(item.objectType);
        json += ",\"handle\":";
        json += JsonString(HandleId(item.handle));
        json += ",\"native_node_type\":";
        json += std::to_string(item.nativeNodeType);
        json += ",\"verified\":";
        json += item.semanticallyVerified ? "true" : "false";
        if (!item.warnings.empty()) {
            json += ",\"warnings\":";
            json += JsonStringArray(item.warnings);
        }
        if (item.objectType == "polygon") {
            json += ",\"vertex_count\":";
            json += std::to_string(item.vertexCount);
            json += ",\"closed\":";
            json += item.closed ? "true" : "false";
        }
        json += "}";
    }
    json += "]";
    return json;
}

Transactions::TransactionOptions MakeSdkManagedObjectTransactionOptions(
    std::size_t expectedArtifactCount) {
    Transactions::TransactionOptions options;
    options.expectedArtifactCount = expectedArtifactCount;
    options.sdkManagedRegistrationFamilies = {
        Transactions::ObjectFamily::Symbol,
        Transactions::ObjectFamily::Wall,
        Transactions::ObjectFamily::Parametric,
        Transactions::ObjectFamily::Space,
        Transactions::ObjectFamily::Slab,
        Transactions::ObjectFamily::Roof,
        Transactions::ObjectFamily::Door,
        Transactions::ObjectFamily::Window,
    };
    return options;
}

std::string HandleCreateTypedObject(const Params& params, const std::string& label, const TXString& undoName) {
    const PrimitiveSpec spec = ParsePrimitiveSpec(params, label);

    auto options = MakeSdkManagedObjectTransactionOptions(2u);
    Transactions::NativeTransaction transaction(*gSDK, undoName, std::move(options));
    try {
        std::vector<std::string> warnings;
        Transactions::ArtifactId artifact = 0;
        MCObjectHandle object = CreatePrimitiveFromSpec(spec, transaction, &artifact, &warnings);
        const auto transactionReceipt = transaction.Commit();

        std::string json = "{\"type\":";
        json += JsonString(spec.objectType);
        json += ",\"handle\":";
        json += JsonString(HandleId(object));
        const auto uuid = ObjectUuidString(object);
        if (!uuid.empty()) {
            json += ",\"uuid\":";
            json += JsonString(uuid);
        }
        if (!warnings.empty()) {
            json += ",\"warnings\":";
            json += JsonStringArray(warnings);
        }
        if (spec.objectType == "polygon") {
            json += ",\"vertex_count\":";
            json += std::to_string(spec.points.size());
            json += ",\"closed\":";
            json += spec.closed ? "true" : "false";
        }
        json += ",\"object\":";
        json += ObjectJson(object);
        json += ",\"transaction_artifact\":";
        json += std::to_string(artifact);
        json += ",\"undo_committed\":";
        json += transactionReceipt.endUndoEventSucceeded ? "true" : "false";
        json += "}";
        return json;
    } catch (...) {
        transaction.RollbackAndRethrow(std::current_exception());
    }
}

std::string HandleCreateObject(const Params& params) {
    return HandleCreateTypedObject(params, "create_object", TXString("Vectorworks MCP create object"));
}

std::string HandleCreateWall(const Params& params) {
    return HandleCreateTypedObject(params, "create_wall", TXString("Vectorworks MCP create wall"));
}

std::string HandleCreateText(const Params& params) {
    return HandleCreateTypedObject(params, "create_text", TXString("Vectorworks MCP create text"));
}

std::string HandleCreateLinearDimension(const Params& params) {
    return HandleCreateTypedObject(params, "create_linear_dimension", TXString("Vectorworks MCP create dimension"));
}

int ParseIntegerString(const std::string& rawValue, const std::string& label, int minValue, int maxValue) {
    const std::string value = TrimCopy(rawValue);
    if (value.empty()) {
        throw std::invalid_argument(label + " is required");
    }
    std::size_t parsedChars = 0u;
    long parsed = 0;
    try {
        parsed = std::stol(value, &parsedChars, 10);
    } catch (...) {
        throw std::invalid_argument(label + " must be an integer");
    }
    if (parsedChars != value.size()) {
        throw std::invalid_argument(label + " must be an integer");
    }
    if (parsed < minValue || parsed > maxValue) {
        throw std::invalid_argument(label + " must be between " + std::to_string(minValue) + " and " + std::to_string(maxValue));
    }
    return static_cast<int>(parsed);
}

ColorRef ColorRefFromRgbString(const std::string& value) {
    std::vector<int> components;
    std::stringstream input(value);
    std::string part;
    while (std::getline(input, part, ',')) {
        components.push_back(ParseIntegerString(part, "color component", 0, 65535));
    }
    if (components.size() != 3u) {
        throw std::invalid_argument("color must be r,g,b with components in 0..65535");
    }

    RGBColor rgb = {};
    rgb.red = static_cast<unsigned short>(components[0]);
    rgb.green = static_cast<unsigned short>(components[1]);
    rgb.blue = static_cast<unsigned short>(components[2]);
    ColorRef colorRef = 0;
    gSDK->RGBToColorIndexN(rgb, colorRef, false);
    return colorRef;
}

MCObjectHandle ObjectHandleFromSessionId(const std::string& handleId) {
    const std::string value = TrimCopy(handleId);
    if (value.empty()) {
        throw std::invalid_argument("handle is required");
    }
    std::size_t parsedChars = 0u;
    unsigned long long raw = 0u;
    try {
        raw = std::stoull(value, &parsedChars, 0);
    } catch (...) {
        throw std::invalid_argument("handle must be a session handle returned by get_objects");
    }
    if (parsedChars != value.size() || raw == 0u) {
        throw std::invalid_argument("handle must be a session handle returned by get_objects");
    }
    const auto canonicalHandleId = HandleIdFromRaw(static_cast<std::uintptr_t>(raw));
    if (gKnownObjectHandleIds.find(canonicalHandleId) == gKnownObjectHandleIds.end()) {
        throw std::invalid_argument("handle was not returned by this native bridge session; resolve the object with get_objects first");
    }
    return reinterpret_cast<MCObjectHandle>(static_cast<std::uintptr_t>(raw));
}

void ApplyObjectProperty(MCObjectHandle object, const std::string& propertyName, const std::string& value) {
    if (propertyName == "name") {
        const GSError err = gSDK->SetObjectName(object, TXString(value.c_str()));
        if (err != 0) {
            throw std::runtime_error("Vectorworks rejected object name");
        }
        return;
    }
    if (propertyName == "class") {
        if (value.empty()) {
            throw std::invalid_argument("class value is required");
        }
        InternalIndex classId = gSDK->ClassNameToID(TXString(value.c_str()));
        if (!gSDK->ValidClass(classId)) {
            classId = gSDK->AddClass(TXString(value.c_str()));
        }
        if (!gSDK->ValidClass(classId)) {
            throw std::runtime_error("Vectorworks rejected class: " + value);
        }
        gSDK->SetObjectClass(object, classId);
        return;
    }
    if (propertyName == "lineWeight") {
        const int lineWeight = ParseIntegerString(value, "lineWeight", 0, std::numeric_limits<short>::max());
        gSDK->SetLineWeight(object, static_cast<short>(lineWeight));
        return;
    }
    if (propertyName == "fillPattern") {
        const int fillPattern = ParseIntegerString(
            value,
            "fillPattern",
            0,
            std::numeric_limits<short>::max());
        gSDK->SetFillPat(object, static_cast<InternalIndex>(fillPattern));
        return;
    }
    if (propertyName == "opacity") {
        const int opacity = ParseIntegerString(value, "opacity", 0, 100);
        gSDK->SetOpacity(object, static_cast<OpacityRef>(opacity));
        return;
    }
    if (propertyName == "fillColor" || propertyName == "penColor") {
        ObjectColorType colors = {};
        if (!gSDK->GetColor(object, colors)) {
            throw std::runtime_error("Vectorworks could not read object color");
        }
        const ColorRef colorRef = ColorRefFromRgbString(value);
        if (propertyName == "fillColor") {
            colors.fillFore = colorRef;
            colors.fillBack = colorRef;
        } else {
            colors.penFore = colorRef;
            colors.penBack = colorRef;
        }
        gSDK->SetColor(object, colors);
        return;
    }
    throw std::invalid_argument("unsupported property: " + propertyName);
}

std::string HandleSetProperty(const Params& params) {
    const std::string handleId = GetStringParam(params, "handle");
    const std::string propertyName = GetStringParam(params, "property_name");
    const std::string value = GetStringParam(params, "value");
    if (propertyName.empty()) {
        throw std::invalid_argument("property_name is required");
    }
    if (value.size() > kMaxPropertyValueChars) {
        throw std::invalid_argument("property value is too long");
    }

    MCObjectHandle object = ObjectHandleFromSessionId(handleId);
    const short type = gSDK->GetObjectTypeN(object);
    if (!IsUserVisibleObjectType(type)) {
        throw std::invalid_argument("handle does not refer to a user-visible object");
    }

    const std::string before = ObjectJson(object);
    gSDK->SupportUndoAndRemove();
    gSDK->SetUndoMethod(kUndoSwapObjects);
    gSDK->NameUndoEvent(TXString("Vectorworks MCP set property"));
    try {
        if (!gSDK->AddBeforeSwapObject(object)) {
            throw std::runtime_error("Vectorworks failed to register the object before property mutation");
        }
        ApplyObjectProperty(object, propertyName, value);
        gSDK->ResetObject(object);
        if (!gSDK->AddAfterSwapObject(object)) {
            throw std::runtime_error("Vectorworks failed to register the object after property mutation");
        }
        EndUndoEventOrThrow("property mutation");
    } catch (...) {
        gSDK->UndoAndRemove();
        throw;
    }

    std::string json = "{\"changed\":true,\"handle\":";
    json += JsonString(handleId);
    json += ",\"property_name\":";
    json += JsonString(propertyName);
    json += ",\"value\":";
    json += JsonString(value);
    json += ",\"before\":";
    json += before;
    json += ",\"after\":";
    json += ObjectJson(object);
    json += "}";
    return json;
}

std::string ClassListJson() {
    std::vector<std::string> classNames;
    VWClass::ForEachClass(false, [&](const VWClass& clas) {
        const auto name = TxToUtf8(clas.GetName());
        if (!name.empty()) {
            classNames.push_back(name);
        }
    });
    std::sort(classNames.begin(), classNames.end());
    return JsonStringArray(classNames);
}

std::string HandleManageClasses(const Params& params) {
    const std::string action = ToLower(GetStringParam(params, "action", "list"));
    const std::string className = TrimCopy(GetStringParam(params, "class_name"));
    if (action == "list") {
        return ClassListJson();
    }

    if (action != "create" && action != "delete") {
        throw std::invalid_argument("unknown class action. Use: list, create, delete");
    }
    if (className.empty()) {
        throw std::invalid_argument("class_name is required");
    }
    if (className.size() > kMaxPropertyValueChars) {
        throw std::invalid_argument("class_name is too long");
    }

    const TXString txClassName(className.c_str());
    InternalIndex classId = gSDK->ClassNameToID(txClassName);
    const bool existed = gSDK->ValidClass(classId);
    if (action == "create") {
        bool created = false;
        if (!existed) {
            gSDK->SupportUndoAndRemove();
            gSDK->SetUndoMethod(kUndoSwapObjects);
            gSDK->NameUndoEvent(TXString("Vectorworks MCP create class"));
            try {
                classId = gSDK->AddClass(txClassName);
                created = gSDK->ValidClass(classId);
                if (!created) {
                    throw std::runtime_error("Vectorworks rejected class: " + className);
                }
                EndUndoEventOrThrow("class creation");
            } catch (...) {
                gSDK->UndoAndRemove();
                throw;
            }
        }
        std::string json = "{\"action\":\"create\",\"class_name\":";
        json += JsonString(className);
        json += ",\"created\":";
        json += created ? "true" : "false";
        json += ",\"existed\":";
        json += existed ? "true" : "false";
        json += "}";
        return json;
    }

    if (GetStringParam(params, "confirm") != "DELETE_CLASS") {
        throw std::invalid_argument("class deletion requires confirm='DELETE_CLASS'");
    }
    if (!existed) {
        throw std::invalid_argument("class not found: " + className);
    }
    if (className == "None") {
        throw std::invalid_argument("refusing to delete the None class");
    }

    gSDK->SupportUndoAndRemove();
    gSDK->SetUndoMethod(kUndoSwapObjects);
    gSDK->NameUndoEvent(TXString("Vectorworks MCP delete class"));
    bool stillExists = true;
    try {
        gSDK->DeleteClass(txClassName);
        stillExists = gSDK->ValidClass(gSDK->ClassNameToID(txClassName));
        if (stillExists) {
            throw std::runtime_error("Vectorworks did not delete class: " + className);
        }
        EndUndoEventOrThrow("class deletion");
    } catch (...) {
        gSDK->UndoAndRemove();
        throw;
    }
    std::string json = "{\"action\":\"delete\",\"class_name\":";
    json += JsonString(className);
    json += ",\"deleted\":";
    json += stillExists ? "false" : "true";
    json += "}";
    return json;
}

enum class ApplyOperationKind {
    Create,
    SetProperty,
    Transform,
    Reshape,
    UpdateParametric,
    Duplicate,
    Delete,
};

struct ApplyOperation {
    ApplyOperationKind kind = ApplyOperationKind::Create;
    PrimitiveSpec primitive;
    std::string localRef;
    std::string target;
    std::string propertyName;
    std::string propertyValue;
    double deltaX = 0.0;
    double deltaY = 0.0;
    double rotationDegrees = 0.0;
    double scaleX = 1.0;
    double scaleY = 1.0;
    bool hasPivot = false;
    double pivotX = 0.0;
    double pivotY = 0.0;
    double startX = 0.0;
    double startY = 0.0;
    double endX = 0.0;
    double endY = 0.0;
    std::string confirmation;
};

struct ApplyOperationsCacheEntry {
    std::string idempotencyKey;
    std::string documentIdentity;
    std::uint64_t operationsFingerprint = 0u;
    std::string transactionJson;
};

constexpr std::size_t kMaxApplyOperations = 250u;
constexpr std::size_t kMaxApplyOperationsCacheEntries = 128u;
constexpr std::size_t kMaxApplyReferenceChars = 128u;
std::deque<ApplyOperationsCacheEntry> gApplyOperationsCache;

bool IsSupportedPropertyName(const std::string& propertyName) {
    return propertyName == "name" ||
        propertyName == "class" ||
        propertyName == "fillColor" ||
        propertyName == "penColor" ||
        propertyName == "fillPattern" ||
        propertyName == "lineWeight" ||
        propertyName == "opacity";
}

bool IsExplicitExternalObjectReference(const std::string& target) {
    return target.rfind("uuid:", 0u) == 0u ||
        target.rfind("name:", 0u) == 0u ||
        target.rfind("handle:", 0u) == 0u;
}

void ValidateMutationTarget(const ApplyOperation& operation, const std::string& label) {
    if (operation.target.empty()) {
        throw std::invalid_argument(label + ".target is required");
    }
    if (operation.target.size() > kMaxApplyReferenceChars) {
        throw std::invalid_argument(label + ".target is too long");
    }
    if (!IsExplicitExternalObjectReference(operation.target)) {
        throw std::invalid_argument(
            label + ".target must be an explicit uuid:, name:, or handle: object reference");
    }
}

std::string ActiveDocumentIdentity() {
    MCObjectHandle layer = gSDK ? gSDK->GetActiveLayer() : nullptr;
    if (!layer && gSDK) {
        layer = gSDK->GetCurrentLayer();
    }
    if (layer) {
        TXString layerUuid;
        if (gSDK->GetObjectUuid(layer, layerUuid) && !layerUuid.IsEmpty()) {
            return "layer:" + TxToUtf8(layerUuid);
        }
    }
    std::string identity;
    VectorWorks::Filing::IFileIdentifierPtr activeFile(VectorWorks::Filing::IID_FileIdentifier);
    bool saved = false;
    if (activeFile && gSDK->GetActiveDocument(&activeFile, saved)) {
        TXString path;
        TXString name;
        activeFile->GetFileFullPath(path);
        activeFile->GetFileName(name);
        identity = TxToUtf8(path);
        if (identity.empty()) {
            identity = TxToUtf8(name);
        }
    }
    return identity;
}

std::uint64_t ApplyOperationsFingerprint(const Params& params) {
    constexpr std::uint64_t offsetBasis = 14695981039346656037ull;
    constexpr std::uint64_t prime = 1099511628211ull;
    std::uint64_t hash = offsetBasis;
    const int operationCount = GetRequiredBoundedIntParam(
        params,
        "operation_count",
        1,
        static_cast<int>(kMaxApplyOperations));
    auto addText = [&](const std::string& text) {
        for (const unsigned char ch : text) {
            hash ^= static_cast<std::uint64_t>(ch);
            hash *= prime;
        }
        hash ^= 0xffu;
        hash *= prime;
    };
    addText(std::to_string(operationCount));
    for (int index = 1; index <= operationCount; ++index) {
        const std::string key = "operation_" + std::to_string(index) + "_json";
        const std::string operationJson = GetStringParam(params, key);
        if (operationJson.empty()) {
            throw std::invalid_argument(key + " is required");
        }
        addText(operationJson);
    }
    return hash;
}

std::optional<std::string> FindCachedApplyOperations(
    const std::string& idempotencyKey,
    const std::string& documentIdentity,
    std::uint64_t operationsFingerprint) {
    if (idempotencyKey.empty()) {
        return std::nullopt;
    }
    for (const auto& entry : gApplyOperationsCache) {
        if (entry.idempotencyKey == idempotencyKey) {
            if (entry.operationsFingerprint != operationsFingerprint) {
                throw std::invalid_argument(
                    "idempotency_key was already used with a different operations payload");
            }
            if (entry.documentIdentity != documentIdentity) {
                throw std::invalid_argument(
                    "idempotency_key was already used in a different active document");
            }
            return entry.transactionJson;
        }
    }
    return std::nullopt;
}

void StoreCachedApplyOperations(
    const std::string& idempotencyKey,
    const std::string& documentIdentity,
    std::uint64_t operationsFingerprint,
    const std::string& transactionJson) {
    if (idempotencyKey.empty()) {
        return;
    }
    gApplyOperationsCache.erase(
        std::remove_if(
            gApplyOperationsCache.begin(),
            gApplyOperationsCache.end(),
            [&](const ApplyOperationsCacheEntry& entry) {
                return entry.idempotencyKey == idempotencyKey;
            }),
        gApplyOperationsCache.end());
    gApplyOperationsCache.push_back({
        idempotencyKey,
        documentIdentity,
        operationsFingerprint,
        transactionJson,
    });
    while (gApplyOperationsCache.size() > kMaxApplyOperationsCacheEntries) {
        gApplyOperationsCache.pop_front();
    }
}

std::string WrapApplyOperationsResult(
    const std::string& transactionJson,
    const std::string& idempotencyKey,
    bool replayed) {
    std::string json = "{\"ok\":true,\"atomic\":true,\"verified\":true,\"replayed\":";
    json += replayed ? "true" : "false";
    if (!idempotencyKey.empty()) {
        json += ",\"idempotency_key\":";
        json += JsonString(idempotencyKey);
    }
    json += ",\"transaction\":";
    json += transactionJson;
    json += "}";
    return json;
}

std::vector<ApplyOperation> ParseApplyOperations(const Params& params) {
    const int operationCount = GetRequiredBoundedIntParam(
        params,
        "operation_count",
        1,
        static_cast<int>(kMaxApplyOperations));
    std::vector<ApplyOperation> operations;
    operations.reserve(static_cast<std::size_t>(operationCount));
    std::unordered_set<std::string> declaredLocalRefs;

    for (int index = 1; index <= operationCount; ++index) {
        const std::string key = "operation_" + std::to_string(index) + "_json";
        const std::string operationJson = GetStringParam(params, key);
        if (operationJson.empty()) {
            throw std::invalid_argument(key + " is required");
        }
        const Params operationParams = ParseParams(operationJson);
        const std::string op = ToLower(GetStringParam(operationParams, "op"));
        const std::string label = "operation_" + std::to_string(index);
        if (op == "create") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Create;
            operation.primitive = ParsePrimitiveSpec(operationParams, label);
            operation.localRef = GetStringParam(operationParams, "local_ref");
            if (operation.localRef.size() > kMaxApplyReferenceChars) {
                throw std::invalid_argument(label + ".local_ref is too long");
            }
            if (!operation.localRef.empty() && !declaredLocalRefs.insert(operation.localRef).second) {
                throw std::invalid_argument(label + ".local_ref is duplicated: " + operation.localRef);
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "set_property") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::SetProperty;
            operation.target = GetStringParam(operationParams, "target");
            operation.propertyName = GetStringParam(operationParams, "property_name");
            operation.propertyValue = GetStringParam(operationParams, "value");
            if (operation.target.empty()) {
                throw std::invalid_argument(label + ".target is required");
            }
            if (operation.target.size() > kMaxApplyReferenceChars) {
                throw std::invalid_argument(label + ".target is too long");
            }
            if (!IsSupportedPropertyName(operation.propertyName)) {
                throw std::invalid_argument(label + ".property_name is unsupported: " + operation.propertyName);
            }
            if (operation.propertyValue.size() > kMaxPropertyValueChars) {
                throw std::invalid_argument(label + ".value is too long");
            }
            if (operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.transform") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Transform;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.deltaX = GetFiniteNumberParam(operationParams, "delta_x", 0.0);
            operation.deltaY = GetFiniteNumberParam(operationParams, "delta_y", 0.0);
            operation.rotationDegrees = GetFiniteNumberParam(operationParams, "rotation_degrees", 0.0);
            operation.scaleX = GetFiniteNumberParam(operationParams, "scale_x", 1.0);
            operation.scaleY = GetFiniteNumberParam(operationParams, "scale_y", 1.0);
            if (operation.scaleX <= 0.0 || operation.scaleY <= 0.0 ||
                operation.scaleX > 1000000.0 || operation.scaleY > 1000000.0) {
                throw std::invalid_argument(label + ".scale_x and scale_y must be > 0 and <= 1000000");
            }
            if (std::abs(operation.deltaX) > 1000000000.0 ||
                std::abs(operation.deltaY) > 1000000000.0 ||
                std::abs(operation.rotationDegrees) > 360000.0) {
                throw std::invalid_argument(label + " transform values exceed supported bounds");
            }
            const bool hasPivotX = HasParam(operationParams, "pivot_x");
            const bool hasPivotY = HasParam(operationParams, "pivot_y");
            if (hasPivotX != hasPivotY) {
                throw std::invalid_argument(label + ".pivot_x and pivot_y must be provided together");
            }
            operation.hasPivot = hasPivotX;
            if (operation.hasPivot) {
                operation.pivotX = GetFiniteNumberParam(operationParams, "pivot_x", 0.0);
                operation.pivotY = GetFiniteNumberParam(operationParams, "pivot_y", 0.0);
            }
            if (operation.deltaX == 0.0 && operation.deltaY == 0.0 &&
                operation.rotationDegrees == 0.0 &&
                operation.scaleX == 1.0 && operation.scaleY == 1.0) {
                throw std::invalid_argument(label + " transform must change translation, rotation, or scale");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.reshape") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Reshape;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.startX = GetFiniteNumberParam(operationParams, "start_x", 0.0);
            operation.startY = GetFiniteNumberParam(operationParams, "start_y", 0.0);
            operation.endX = GetFiniteNumberParam(operationParams, "end_x", 0.0);
            operation.endY = GetFiniteNumberParam(operationParams, "end_y", 0.0);
            if (operation.startX == operation.endX && operation.startY == operation.endY) {
                throw std::invalid_argument(label + " reshape endpoints must not be identical");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.update_parametric") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::UpdateParametric;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.primitive = ParsePrimitiveSpec(operationParams, label);
            if (operation.primitive.objectType != "parametric" ||
                operation.primitive.parametricValues.empty()) {
                throw std::invalid_argument(
                    label + " object.update_parametric requires object_type=parametric and at least one parameter");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.duplicate") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Duplicate;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                if (declaredLocalRefs.find(localRef) == declaredLocalRefs.end()) {
                    throw std::invalid_argument(label + ".target references an unknown local_ref: " + localRef);
                }
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.localRef = GetStringParam(operationParams, "local_ref");
            if (operation.localRef.empty()) {
                throw std::invalid_argument(label + ".local_ref is required for object.duplicate");
            }
            if (operation.localRef.size() > kMaxApplyReferenceChars) {
                throw std::invalid_argument(label + ".local_ref is too long");
            }
            if (!declaredLocalRefs.insert(operation.localRef).second) {
                throw std::invalid_argument(label + ".local_ref is duplicated: " + operation.localRef);
            }
            operation.deltaX = GetFiniteNumberParam(operationParams, "delta_x", 0.0);
            operation.deltaY = GetFiniteNumberParam(operationParams, "delta_y", 0.0);
            if (std::abs(operation.deltaX) > 1000000000.0 ||
                std::abs(operation.deltaY) > 1000000000.0) {
                throw std::invalid_argument(label + " duplicate offsets exceed supported bounds");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        if (op == "object.delete") {
            ApplyOperation operation;
            operation.kind = ApplyOperationKind::Delete;
            operation.target = GetStringParam(operationParams, "target");
            if (!operation.target.empty() && operation.target.front() == '$') {
                const std::string localRef = operation.target.substr(1);
                const auto found = declaredLocalRefs.find(localRef);
                if (found == declaredLocalRefs.end()) {
                    throw std::invalid_argument(
                        label + ".target references an unknown or deleted local_ref: " + localRef);
                }
                declaredLocalRefs.erase(found);
            } else {
                ValidateMutationTarget(operation, label);
            }
            operation.confirmation = GetStringParam(operationParams, "confirm");
            if (operation.confirmation != "DELETE_OBJECT") {
                throw std::invalid_argument(label + ".confirm must be DELETE_OBJECT");
            }
            operations.push_back(std::move(operation));
            continue;
        }
        throw std::invalid_argument(
            label + ".op must be create, set_property, object.transform, object.reshape, "
            "object.update_parametric, object.duplicate, or object.delete");
    }
    return operations;
}

MCObjectHandle ResolveApplyOperationTarget(
    const std::string& target,
    const std::unordered_map<std::string, MCObjectHandle>& localObjects) {
    if (!target.empty() && target.front() == '$') {
        const auto found = localObjects.find(target.substr(1));
        if (found == localObjects.end() || !found->second) {
            throw std::invalid_argument("local operation target could not be resolved: " + target);
        }
        return found->second;
    }
    constexpr std::string_view uuidPrefix = "uuid:";
    if (target.compare(0, uuidPrefix.size(), uuidPrefix) == 0) {
        const std::string uuid = target.substr(uuidPrefix.size());
        if (uuid.empty()) {
            throw std::invalid_argument("uuid operation target is empty");
        }
        MCObjectHandle object = gSDK->GetObjectByUuid(TXString(uuid.c_str()));
        if (!object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
            throw std::invalid_argument("uuid operation target was not found: " + uuid);
        }
        return object;
    }
    constexpr std::string_view namePrefix = "name:";
    if (target.compare(0, namePrefix.size(), namePrefix) == 0) {
        const std::string name = target.substr(namePrefix.size());
        if (name.empty()) {
            throw std::invalid_argument("name operation target is empty");
        }
        std::vector<MCObjectHandle> matches;
        gSDK->ForEachObjectN(
            allObjects + descendIntoAll + descendIntoViewports + descendIntoAuxLists,
            [&](MCObjectHandle object) {
                if (!object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
                    return;
                }
                TXString objectName;
                gSDK->GetObjectName(object, objectName);
                if (TxToUtf8(objectName) == name) {
                    matches.push_back(object);
                }
            });
        if (matches.empty()) {
            throw std::invalid_argument("name operation target was not found: " + name);
        }
        if (matches.size() != 1u) {
            throw std::invalid_argument("name operation target is ambiguous: " + name);
        }
        return matches.front();
    }
    constexpr std::string_view handlePrefix = "handle:";
    const std::string handleId = target.compare(0, handlePrefix.size(), handlePrefix) == 0
        ? target.substr(handlePrefix.size())
        : target;
    return ObjectHandleFromSessionId(handleId);
}

struct PreparedApplyOperationTargets {
    std::vector<MCObjectHandle> handles;
    std::vector<std::string> deleteUuids;
};

PreparedApplyOperationTargets PrepareApplyOperationTargets(
    const std::vector<ApplyOperation>& operations) {
    PreparedApplyOperationTargets prepared;
    prepared.handles.resize(operations.size(), nullptr);
    prepared.deleteUuids.resize(operations.size());
    const std::unordered_map<std::string, MCObjectHandle> noLocalObjects;
    std::unordered_set<MCObjectHandle> deletedTargets;

    for (std::size_t index = 0; index < operations.size(); ++index) {
        const auto& operation = operations[index];
        if (operation.kind == ApplyOperationKind::Create ||
            ((operation.kind == ApplyOperationKind::SetProperty ||
              operation.kind == ApplyOperationKind::Transform ||
              operation.kind == ApplyOperationKind::Reshape ||
              operation.kind == ApplyOperationKind::UpdateParametric ||
              operation.kind == ApplyOperationKind::Duplicate ||
              operation.kind == ApplyOperationKind::Delete) &&
             !operation.target.empty() && operation.target.front() == '$')) {
            continue;
        }
        MCObjectHandle object = ResolveApplyOperationTarget(operation.target, noLocalObjects);
        if (!object || !IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
            throw std::invalid_argument(
                "operation_" + std::to_string(index + 1u) + ".target is not a user-visible object");
        }
        if (deletedTargets.find(object) != deletedTargets.end()) {
            throw std::invalid_argument(
                "operation_" + std::to_string(index + 1u) +
                ".target refers to an object deleted earlier in the same transaction");
        }
        prepared.handles[index] = object;
        if (operation.kind == ApplyOperationKind::Delete) {
            TXString uuid;
            if (!gSDK->GetObjectUuid(object, uuid) || uuid.IsEmpty()) {
                throw std::invalid_argument(
                    "operation_" + std::to_string(index + 1u) +
                    ".target does not expose a UUID required for verified deletion");
            }
            prepared.deleteUuids[index] = TxToUtf8(uuid);
            deletedTargets.insert(object);
        }
    }
    return prepared;
}

std::string HandleApplyOperations(const Params& params) {
    const std::string idempotencyKey = GetStringParam(params, "idempotency_key");
    if (idempotencyKey.size() > kMaxApplyReferenceChars) {
        throw std::invalid_argument("idempotency_key is too long");
    }
    const auto operations = ParseApplyOperations(params);
    const std::uint64_t operationsFingerprint = ApplyOperationsFingerprint(params);
    if (const auto cached = FindCachedApplyOperations(
            idempotencyKey,
            ActiveDocumentIdentity(),
            operationsFingerprint)) {
        return WrapApplyOperationsResult(*cached, idempotencyKey, true);
    }
    PreparedApplyOperationTargets preparedTargets = PrepareApplyOperationTargets(operations);

    std::unordered_map<std::string, MCObjectHandle> localObjects;
    std::unordered_map<std::string, Transactions::ArtifactId> localArtifacts;
    std::unordered_set<MCObjectHandle> createdHandles;
    std::unordered_set<MCObjectHandle> dirtyHandles;
    std::unordered_set<std::string> deletedExternalUuids;
    std::unordered_map<std::string, std::string> externalBefore;
    std::unordered_map<std::string, MCObjectHandle> externalFinalHandles;
    std::unordered_map<std::string, Transactions::ExternalMutationId> externalMutationIds;
    std::vector<CreatedPrimitive> created;
    std::vector<std::string> operationResults;
    Transactions::TransactionReceipt transactionReceipt;
    created.reserve(operations.size());
    operationResults.reserve(operations.size());

    auto transactionOptions = MakeSdkManagedObjectTransactionOptions(operations.size() * 2u);
    Transactions::NativeTransaction transaction(
        *gSDK,
        TXString("Vectorworks MCP apply operations"),
        std::move(transactionOptions));
    try {
        const auto trackExternalBefore = [&](MCObjectHandle object) -> std::string {
            const std::string uuid = ObjectUuidString(object);
            if (uuid.empty()) {
                throw std::runtime_error(
                    "external mutation target does not expose a stable Vectorworks UUID");
            }
            if (externalMutationIds.find(uuid) == externalMutationIds.end()) {
                externalBefore.emplace(uuid, ObjectJson(object));
                externalFinalHandles.emplace(uuid, object);
                externalMutationIds.emplace(
                    uuid,
                    transaction.TrackExternalBefore(object, ExternalMutationFamily(object)));
            } else {
                externalFinalHandles[uuid] = object;
            }
            return uuid;
        };

        for (std::size_t index = 0; index < operations.size(); ++index) {
            const auto& operation = operations[index];
            if (operation.kind == ApplyOperationKind::Create) {
                std::vector<std::string> warnings;
                Transactions::ArtifactId artifact = 0;
                MCObjectHandle object = CreatePrimitiveFromSpec(
                    operation.primitive, transaction, &artifact, &warnings);
                createdHandles.insert(object);
                if (!operation.localRef.empty()) {
                    localObjects.emplace(operation.localRef, object);
                    localArtifacts.emplace(operation.localRef, artifact);
                }
                created.push_back({
                    static_cast<int>(index + 1u),
                    operation.primitive.objectType,
                    object,
                    warnings,
                    operation.primitive.points.size(),
                    operation.primitive.closed,
                    gSDK->GetObjectTypeN(object),
                    true,
                });
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"create\",\"handle\":" + JsonString(HandleId(object));
                if (!operation.localRef.empty()) {
                    result += ",\"local_ref\":";
                    result += JsonString(operation.localRef);
                }
                result += "}";
                operationResults.push_back(std::move(result));
                continue;
            }

            MCObjectHandle object = preparedTargets.handles[index];
            if (!object) {
                object = ResolveApplyOperationTarget(operation.target, localObjects);
            }
            std::string externalUuid;
            if (operation.kind != ApplyOperationKind::Duplicate &&
                createdHandles.find(object) == createdHandles.end()) {
                externalUuid = trackExternalBefore(object);
            }
            if (operation.kind == ApplyOperationKind::SetProperty) {
                ApplyObjectProperty(object, operation.propertyName, operation.propertyValue);
                dirtyHandles.insert(object);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"set_property\",\"target\":" + JsonString(operation.target) +
                    ",\"property_name\":" + JsonString(operation.propertyName) +
                    ",\"value\":" + JsonString(operation.propertyValue) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::Transform) {
                const short semanticTypeBefore = gSDK->GetObjectTypeN(object);
                const std::string before = ObjectJson(object);
                WorldRect bounds;
                gSDK->GetObjectBounds(object, bounds);
                const WorldPt pivot = operation.hasPivot
                    ? WorldPt(operation.pivotX, operation.pivotY)
                    : WorldPt(
                        (bounds.Left() + bounds.Right()) / 2.0,
                        (bounds.Top() + bounds.Bottom()) / 2.0);
                if (operation.scaleX != 1.0 || operation.scaleY != 1.0) {
                    gSDK->ScaleObjectN(object, pivot, operation.scaleX, operation.scaleY);
                }
                if (operation.rotationDegrees != 0.0) {
                    MCObjectHandle rotatedObject = object;
                    gSDK->RotateObjectN(rotatedObject, pivot, operation.rotationDegrees);
                    if (!rotatedObject) {
                        throw std::runtime_error("Vectorworks returned a null handle after object.transform rotation");
                    }
                    if (rotatedObject != object) {
                        for (auto& preparedHandle : preparedTargets.handles) {
                            if (preparedHandle == object) {
                                preparedHandle = rotatedObject;
                            }
                        }
                        if (dirtyHandles.erase(object) != 0u) {
                            dirtyHandles.insert(rotatedObject);
                        }
                        for (auto& localEntry : localObjects) {
                            if (localEntry.second == object) {
                                localEntry.second = rotatedObject;
                            }
                        }
                        if (createdHandles.erase(object) != 0u) {
                            createdHandles.insert(rotatedObject);
                        }
                        if (!externalUuid.empty()) {
                            externalFinalHandles[externalUuid] = rotatedObject;
                        }
                        object = rotatedObject;
                    }
                }
                if (operation.deltaX != 0.0 || operation.deltaY != 0.0) {
                    gSDK->MoveObject(object, operation.deltaX, operation.deltaY);
                }
                gSDK->ResetObject(object);
                if (gSDK->GetObjectTypeN(object) != semanticTypeBefore) {
                    throw std::runtime_error("object.transform changed the object's semantic node type");
                }
                dirtyHandles.insert(object);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.transform\",\"target\":" + JsonString(operation.target) +
                    ",\"applied\":{\"delta_x\":" + JsonNumber(operation.deltaX) +
                    ",\"delta_y\":" + JsonNumber(operation.deltaY) +
                    ",\"rotation_degrees\":" + JsonNumber(operation.rotationDegrees) +
                    ",\"scale_x\":" + JsonNumber(operation.scaleX) +
                    ",\"scale_y\":" + JsonNumber(operation.scaleY) +
                    ",\"pivot\":[" + JsonNumber(pivot.x) + "," + JsonNumber(pivot.y) + "]}" +
                    ",\"before\":" + before + ",\"after\":" + ObjectJson(object) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::Reshape) {
                const short semanticTypeBefore = gSDK->GetObjectTypeN(object);
                if (semanticTypeBefore != kWallNode && semanticTypeBefore != kLineNode) {
                    throw std::invalid_argument(
                        "object.reshape currently supports native wall and line targets only");
                }
                WorldPt priorStart;
                WorldPt priorEnd;
                gSDK->GetEndPoints(object, priorStart, priorEnd);
                const WorldPt requestedStart(operation.startX, operation.startY);
                const WorldPt requestedEnd(operation.endX, operation.endY);
                gSDK->SetEndPoints(object, requestedStart, requestedEnd);
                gSDK->ResetObject(object);
                WorldPt actualStart;
                WorldPt actualEnd;
                gSDK->GetEndPoints(object, actualStart, actualEnd);
                if (gSDK->GetObjectTypeN(object) != semanticTypeBefore ||
                    !WorldCoordsAreNearlyEqual(actualStart.x, requestedStart.x) ||
                    !WorldCoordsAreNearlyEqual(actualStart.y, requestedStart.y) ||
                    !WorldCoordsAreNearlyEqual(actualEnd.x, requestedEnd.x) ||
                    !WorldCoordsAreNearlyEqual(actualEnd.y, requestedEnd.y)) {
                    throw std::runtime_error("object.reshape endpoint readback mismatch");
                }
                dirtyHandles.insert(object);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.reshape\",\"target\":" + JsonString(operation.target) +
                    ",\"before_endpoints\":[[" + JsonNumber(priorStart.x) + "," + JsonNumber(priorStart.y) +
                    "],[" + JsonNumber(priorEnd.x) + "," + JsonNumber(priorEnd.y) + "]]" +
                    ",\"after_endpoints\":[[" + JsonNumber(actualStart.x) + "," + JsonNumber(actualStart.y) +
                    "],[" + JsonNumber(actualEnd.x) + "," + JsonNumber(actualEnd.y) + "]]}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::UpdateParametric) {
                if (gSDK->GetObjectTypeN(object) != kParametricNode) {
                    throw std::invalid_argument(
                        "object.update_parametric target is not a native parametric object");
                }
                MCObjectHandle parentBefore = gSDK->ParentObject(object);
                const bool hostedByWall = parentBefore && gSDK->GetObjectTypeN(parentBefore) == kWallNode;
                const std::string before = ObjectJson(object);
                UpdateVerifiedParametricObject(
                    object,
                    operation.primitive.pluginName,
                    operation.primitive.descriptorFingerprint,
                    operation.primitive.parametricValues);
                if (hostedByWall && !IsObjectHostedByWall(object, parentBefore)) {
                    throw std::runtime_error(
                        "object.update_parametric changed the exact wall host");
                }
                dirtyHandles.insert(object);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.update_parametric\",\"target\":" + JsonString(operation.target) +
                    ",\"plugin_name\":" + JsonString(operation.primitive.pluginName) +
                    ",\"parameter_count\":" +
                    std::to_string(operation.primitive.parametricValues.size()) +
                    ",\"wall_host_preserved\":" + (hostedByWall ? "true" : "false") +
                    ",\"before\":" + before + ",\"after\":" + ObjectJson(object) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::Duplicate) {
                const short sourceType = gSDK->GetObjectTypeN(object);
                MCObjectHandle duplicate = gSDK->DuplicateObject(object);
                if (!duplicate) {
                    throw std::runtime_error("Vectorworks failed to duplicate the explicit object target");
                }
                UnregisteredCreatedObjectGuard duplicateGuard(duplicate);
                if (!gSDK->InsertObjectAfter(duplicate, object)) {
                    throw std::runtime_error("Vectorworks failed to duplicate and insert the explicit object target");
                }
                if (gSDK->GetObjectTypeN(duplicate) != sourceType) {
                    throw std::runtime_error("object.duplicate changed the object's semantic node type");
                }
                if (operation.deltaX != 0.0 || operation.deltaY != 0.0) {
                    gSDK->MoveObject(duplicate, operation.deltaX, operation.deltaY);
                    gSDK->ResetObject(duplicate);
                }
                TXString sourceUuid;
                TXString duplicateUuid;
                if (!gSDK->GetObjectUuid(object, sourceUuid) || sourceUuid.IsEmpty() ||
                    !gSDK->GetObjectUuid(duplicate, duplicateUuid) || duplicateUuid.IsEmpty() ||
                    sourceUuid == duplicateUuid ||
                    gSDK->ParentObject(duplicate) != gSDK->ParentObject(object)) {
                    throw std::runtime_error("object.duplicate failed semantic identity or container readback");
                }
                const auto artifact = transaction.AdoptFinal(
                    duplicate,
                    Transactions::ObjectFamily::Simple,
                    sourceType,
                    [sourceType](MCObjectHandle verifiedDuplicate) {
                        if (!verifiedDuplicate ||
                            gSDK->GetObjectTypeN(verifiedDuplicate) != sourceType) {
                            throw std::runtime_error(
                                "object.duplicate failed semantic verification at commit");
                        }
                    });
                duplicateGuard.Release();
                createdHandles.insert(duplicate);
                localObjects.emplace(operation.localRef, duplicate);
                localArtifacts.emplace(operation.localRef, artifact);
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.duplicate\",\"target\":" + JsonString(operation.target) +
                    ",\"local_ref\":" + JsonString(operation.localRef) +
                    ",\"delta_x\":" + JsonNumber(operation.deltaX) +
                    ",\"delta_y\":" + JsonNumber(operation.deltaY) +
                    ",\"source\":" + ObjectJson(object) +
                    ",\"duplicate\":" + ObjectJson(duplicate) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            if (operation.kind == ApplyOperationKind::Delete) {
                const std::string before = ObjectJson(object);
                std::string uuid;
                if (!operation.target.empty() && operation.target.front() == '$') {
                    const std::string localRef = operation.target.substr(1);
                    const auto artifactEntry = localArtifacts.find(localRef);
                    if (artifactEntry == localArtifacts.end()) {
                        throw std::runtime_error(
                            "local deletion target has no transaction artifact: " + localRef);
                    }
                    uuid = transaction.Uuid(artifactEntry->second);
                    transaction.DisposeFinal(artifactEntry->second);
                    createdHandles.erase(object);
                    dirtyHandles.erase(object);
                    localObjects.erase(localRef);
                    localArtifacts.erase(artifactEntry);
                } else {
                    uuid = preparedTargets.deleteUuids[index];
                    gSDK->DeleteObject(object, true);
                    if (gSDK->GetObjectByUuid(TXString(uuid.c_str())) != nullptr) {
                        throw std::runtime_error("Vectorworks did not delete the explicit object target");
                    }
                    const auto mutation = externalMutationIds.find(uuid);
                    if (mutation == externalMutationIds.end()) {
                        throw std::runtime_error(
                            "external deletion target was not registered before mutation");
                    }
                    transaction.TrackExternalDeleted(mutation->second);
                    deletedExternalUuids.insert(uuid);
                    dirtyHandles.erase(object);
                }
                std::string result = "{\"index\":" + std::to_string(index + 1u) +
                    ",\"op\":\"object.delete\",\"target\":" + JsonString(operation.target) +
                    ",\"before\":" + before + ",\"deleted\":true,\"uuid\":" + JsonString(uuid) + "}";
                operationResults.push_back(std::move(result));
                continue;
            }
            throw std::runtime_error("unsupported prepared apply operation kind");
        }

        for (MCObjectHandle object : dirtyHandles) {
            gSDK->ResetObject(object);
        }
        for (const auto& entry : externalMutationIds) {
            if (deletedExternalUuids.find(entry.first) != deletedExternalUuids.end()) {
                continue;
            }
            const auto finalHandle = externalFinalHandles.find(entry.first);
            if (finalHandle == externalFinalHandles.end() || !finalHandle->second) {
                throw std::runtime_error(
                    "external mutation target lost its final handle before commit");
            }
            transaction.TrackExternalAfter(entry.second, finalHandle->second);
        }
        transactionReceipt = transaction.Commit();
    } catch (...) {
        transaction.RollbackAndRethrow(std::current_exception());
    }

    std::string transactionJson = "{\"committed\":true,\"verified\":true,\"operation_count\":";
    transactionJson += std::to_string(operations.size());
    transactionJson += ",\"applied_count\":";
    transactionJson += std::to_string(operations.size());
    transactionJson += ",\"operations\":[";
    for (std::size_t index = 0; index < operationResults.size(); ++index) {
        if (index != 0u) {
            transactionJson += ",";
        }
        transactionJson += operationResults[index];
    }
    transactionJson += "],\"created\":";
    // apply_operations already returns one receipt per wire operation. Keep
    // its created summary handle-based so the fast path does not perform
    // duplicate UUID, color, class, bounds, and object snapshot queries.
    transactionJson += CompactCreatedPrimitiveListJson(created);
    transactionJson += ",\"changed\":[";
    bool firstChanged = true;
    for (const auto& entry : externalBefore) {
        if (deletedExternalUuids.find(entry.first) != deletedExternalUuids.end()) {
            continue;
        }
        if (!firstChanged) {
            transactionJson += ",";
        }
        firstChanged = false;
        transactionJson += "{\"before\":";
        transactionJson += entry.second;
        transactionJson += ",\"after\":";
        const auto finalHandle = externalFinalHandles.find(entry.first);
        transactionJson += finalHandle == externalFinalHandles.end()
            ? "null"
            : ObjectJson(finalHandle->second);
        transactionJson += "}";
    }
    transactionJson += "],\"undo_committed\":";
    transactionJson += transactionReceipt.endUndoEventSucceeded ? "true" : "false";
    transactionJson += "}";

    StoreCachedApplyOperations(
        idempotencyKey,
        ActiveDocumentIdentity(),
        operationsFingerprint,
        transactionJson);
    return WrapApplyOperationsResult(transactionJson, idempotencyKey, false);
}

std::string HandleBatchCreateObjects(const Params& params) {
    const int objectCount = GetRequiredBoundedIntParam(params, "object_count", 1, 250);
    std::vector<PrimitiveSpec> specs;
    specs.reserve(static_cast<std::size_t>(objectCount));
    for (int index = 1; index <= objectCount; ++index) {
        const std::string key = "object_" + std::to_string(index) + "_json";
        const std::string objectJson = GetStringParam(params, key);
        if (objectJson.empty()) {
            throw std::invalid_argument(key + " is required");
        }
        specs.push_back(ParsePrimitiveSpec(ParseParams(objectJson), key));
    }

    std::vector<CreatedPrimitive> created;
    created.reserve(specs.size());
    auto options = MakeSdkManagedObjectTransactionOptions(specs.size() * 2u);
    Transactions::NativeTransaction transaction(
        *gSDK,
        TXString("Vectorworks MCP atomic batch create objects"),
        std::move(options));
    try {
        for (std::size_t index = 0; index < specs.size(); ++index) {
            std::vector<std::string> warnings;
            Transactions::ArtifactId artifact = 0;
            MCObjectHandle object = CreatePrimitiveFromSpec(
                specs[index], transaction, &artifact, &warnings);
            created.push_back({
                static_cast<int>(index + 1u),
                specs[index].objectType,
                object,
                warnings,
                specs[index].points.size(),
                specs[index].closed,
                gSDK->GetObjectTypeN(object),
                true,
            });
        }
        transaction.Commit();
    } catch (...) {
        transaction.RollbackAndRethrow(std::current_exception());
    }

    std::string json = "{\"atomic\":true,\"rollback_on_error\":true,\"created_count\":";
    json += std::to_string(created.size());
    json += ",\"created\":";
    json += CreatedPrimitiveListJson(created);
    json += "}";
    return json;
}

#endif

Protocol::ResponseEnvelope DispatchCadRequestOnVectorworksMainContext(const Protocol::RequestEnvelope& request) {
#if VECTORWORKS_MCP_HAS_SDK
    try {
        const Params params = ParseParams(request.paramsJson);
        if (request.action == "get_document_info") {
            return {request.id, true, HandleGetDocumentInfo(), ""};
        }
        if (request.action == "describe_parametric_schema") {
            return {request.id, true, HandleDescribeParametricSchema(params), ""};
        }
        if (request.action == "export_image" || request.action == "capture_view" ||
            request.action == "export_pdf" || request.action == "export_vectorworks_document" ||
            request.action == "import_dwg" || request.action == "export_dwg") {
            return {request.id, true, HandleNativeIO(request.action, params), ""};
        }
        if (request.action == "resources") {
            return {request.id, true, HandleResources(params), ""};
        }
        if (request.action == "symbol") {
            return {request.id, true, HandleSymbol(params), ""};
        }
        if (request.action == "worksheet") {
            return {request.id, true, HandleWorksheet(params), ""};
        }
        if (request.action == "get_view" || request.action == "set_view") {
            return {request.id, true, HandleView(request.action, params), ""};
        }
        if (request.action == "save_document" || request.action == "open_document") {
            return {request.id, true, HandleDocumentLifecycle(request.action, params, request.id), ""};
        }
        if (request.action == "get_layers") {
            return {request.id, true, HandleGetLayers(), ""};
        }
        if (request.action == "get_objects") {
            return {request.id, true, HandleGetObjects(params), ""};
        }
        if (request.action == "drawing_summary") {
            return {request.id, true, HandleDrawingSummary(params), ""};
        }
        if (request.action == "apply_operations") {
            return {request.id, true, HandleApplyOperations(params), ""};
        }
        if (request.action == "find_objects") {
            return {request.id, true, HandleFindObjects(params), ""};
        }
        if (request.action == "selection") {
            return {request.id, true, HandleSelection(params), ""};
        }
        if (request.action == "create_object") {
            return {request.id, true, HandleCreateObject(params), ""};
        }
        if (request.action == "batch_create_objects") {
            return {request.id, true, HandleBatchCreateObjects(params), ""};
        }
        if (request.action == "create_wall") {
            return {request.id, true, HandleCreateWall(params), ""};
        }
        if (request.action == "create_text") {
            return {request.id, true, HandleCreateText(params), ""};
        }
        if (request.action == "create_linear_dimension") {
            return {request.id, true, HandleCreateLinearDimension(params), ""};
        }
        if (request.action == "set_property") {
            return {request.id, true, HandleSetProperty(params), ""};
        }
        if (request.action == "manage_classes") {
            return {request.id, true, HandleManageClasses(params), ""};
        }
        return {request.id, false, "", "unknown native bridge CAD action: " + request.action};
    } catch (const ViewDocument::Error& exc) {
        std::string error = "{\"code\":";
        const bool unknown = exc.State() == ViewDocument::CommitState::Unknown;
        error += JsonString(unknown
            ? "unknown_commit_state"
            : ViewDocument::ErrorCodeName(exc.Code()));
        error += ",\"message\":" + JsonString(exc.what());
        error += ",\"requested_path\":" + JsonString(exc.RequestedPath());
        error += ",\"active_path\":" + JsonString(exc.ActivePath());
        error += ",\"commit_state\":" +
            JsonString(ViewDocument::CommitStateName(exc.State()));
        error += ",\"retryable\":false}";
        return {request.id, false, "", error};
    } catch (const std::exception& exc) {
        return {request.id, false, "", exc.what()};
    } catch (...) {
        return {request.id, false, "", "native bridge CAD handler failed"};
    }
#else
    // Replace this switch with Vectorworks SDK calls after the SDK-backed
    // ObjectExample worktree builds. This function must run only on the
    // Vectorworks main/plugin event context.
    return {
        request.id,
        false,
        "",
        "native bridge CAD handler not implemented yet: " + request.action,
    };
#endif
}

}  // namespace

Protocol::ResponseEnvelope DispatchFromSocketWorker(const Protocol::RequestEnvelope& request);

bool TryStartNativeTransport() {
    std::lock_guard<std::mutex> lock(gTransportStartMutex);
    if (gTransport.IsRunning()) {
        return true;
    }

    const auto options = GetTransportOptionsFromEnvironment();
    try {
        gTransport.Start(
            options,
            DispatchFromSocketWorker,
            MarkDeferredDocumentOpenResponseSent);
        gNextTransportStartAttempt = {};
        AppendTransportStartupDiagnostic(
            "started",
            options.host + ":" + std::to_string(options.port));
        return true;
    } catch (const std::exception& exc) {
        gNextTransportStartAttempt =
            std::chrono::steady_clock::now() + kTransportStartRetryInterval;
        AppendTransportStartupDiagnostic(
            "start_failed",
            options.host + ":" + std::to_string(options.port) + " - " + exc.what());
        return false;
    } catch (...) {
        gNextTransportStartAttempt =
            std::chrono::steady_clock::now() + kTransportStartRetryInterval;
        AppendTransportStartupDiagnostic(
            "start_failed",
            options.host + ":" + std::to_string(options.port) + " - unknown exception");
        return false;
    }
}

void OnPluginLoadStartTransport() {
    gStopRequested.store(false);
    gCadQueue.ResetCancellation();
#if VECTORWORKS_MCP_HAS_SDK
    gApplyOperationsCache.clear();
#endif
#if VECTORWORKS_MCP_HAS_SDK && defined(_WINDOWS)
    if (!PinBridgeModuleForProcessLifetime()) {
        AppendTransportStartupDiagnostic(
            "pin_failed",
            "the native bridge DLL could not be pinned for the Vectorworks process lifetime");
        gStopRequested.store(true);
        gCadQueue.CancelAll("native bridge module lifetime could not be secured");
        return;
    }
#endif
    if (!StartMainContextPump() && kCadHandlersImplemented) {
        AppendTransportStartupDiagnostic(
            "pump_failed",
            "Vectorworks main-context timer window could not be created");
        gStopRequested.store(true);
        StopMainContextPump();
        gCadQueue.CancelAll("native bridge main-context pump failed to start");
        return;
    }
    TryStartNativeTransport();
}

void OnPluginUnloadStopTransport() {
    gStopRequested.store(true);
    StopMainContextPump();
    gCadQueue.CancelAll("native bridge is unloading");
    gTransport.Stop();
#if VECTORWORKS_MCP_HAS_SDK
    ClearDeferredDocumentOpen();
    gApplyOperationsCache.clear();
#endif
}

void OnVectorworksMainPluginEvent() {
    if (gCadQueuePumpActive.exchange(true)) {
        return;
    }
    ScopedAtomicBoolReset resetPumpActive(gCadQueuePumpActive);
    if (!gStopRequested.load() && !gTransport.IsRunning()) {
        const auto now = std::chrono::steady_clock::now();
        if (gNextTransportStartAttempt == std::chrono::steady_clock::time_point{} ||
            now >= gNextTransportStartAttempt) {
            TryStartNativeTransport();
        }
    }
#if VECTORWORKS_MCP_HAS_SDK
    if (auto deferredOpen = TakeReadyDeferredDocumentOpen()) {
        try {
            ViewDocument::LaunchPreparedOpenDocument(*deferredOpen);
        } catch (...) {
        }
        return;
    }
#endif
    constexpr std::size_t kMaxRequestsPerPump = 8u;
    constexpr auto kPumpBudget = std::chrono::milliseconds(8);
    const auto pumpStarted = std::chrono::steady_clock::now();
    std::size_t processed = 0u;
    while (processed < kMaxRequestsPerPump &&
           std::chrono::steady_clock::now() - pumpStarted < kPumpBudget) {
        auto request = gCadQueue.TryDequeueOnVectorworksMainContext();
        if (!request) {
            break;
        }
        const double queueWaitMs = gCadQueue.QueueWaitMillisecondsForDiagnostics(request->id);
        const auto handlerStart = std::chrono::steady_clock::now();
        auto response = DispatchCadRequestOnVectorworksMainContext(*request);
        const double handlerMs = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - handlerStart).count();
        if (response.success &&
            response.resultJson.size() >= 2u &&
            response.resultJson.front() == '{' &&
            response.resultJson.back() == '}') {
            response.resultJson.pop_back();
            response.resultJson += ",\"timing\":{\"queue_wait_ms\":";
            response.resultJson += JsonNumber(queueWaitMs);
            response.resultJson += ",\"handler_ms\":";
            response.resultJson += JsonNumber(handlerMs);
            response.resultJson += ",\"total_native_ms\":";
            response.resultJson += JsonNumber(queueWaitMs + handlerMs);
            response.resultJson += "}}";
        }
        gCadQueue.CompleteFromVectorworksMainContext(response);
        ++processed;
    }
    if (gCadQueue.PendingCountForDiagnostics() > 0u) {
        NotifyMainContextPump();
    }
}

Protocol::ResponseEnvelope DispatchFromSocketWorker(const Protocol::RequestEnvelope& request) {
    if (!RequestAuthAccepted(request)) {
        return {request.id, false, "", "native bridge authentication failed"};
    }
    const ActionSpec* actionSpec = FindActionSpec(request.action);
    if (actionSpec == nullptr) {
        return {request.id, false, "", "unknown native bridge action: " + request.action};
    }
    if (request.action == "ping") {
        return HandlePingOnTransportThread(request);
    }
    if (request.action == "capabilities") {
        return HandleCapabilitiesOnTransportThread(request);
    }
    if (request.action == "stop") {
        gStopRequested.store(true);
        gCadQueue.CancelAll("native bridge stop requested");
        return {request.id, true, R"("Native bridge stop requested")", ""};
    }
    if (actionSpec->context == ExecutionContext::VectorworksMainPluginContext) {
        if (!CadHandlersRuntimeReady()) {
#if VECTORWORKS_MCP_HAS_SDK
            return {
                request.id,
                false,
                "",
                "native bridge CAD handlers are not ready: main context pump is not running",
            };
#else
            return {request.id, false, "", "native bridge phase 0 CAD handlers are not implemented: " + request.action};
#endif
        }
        if (gStopRequested.load()) {
            return {request.id, false, "", "native bridge is stopping"};
        }
        if (auto enqueueFailure = gCadQueue.EnqueueFromSocketThread(request)) {
            return *enqueueFailure;
        }
        NotifyMainContextPump();
        return gCadQueue.WaitForResponseOnSocketThread(
            request.id,
            kCadRequestTimeout,
            actionSpec->mayWriteDocument);
    }
    return {request.id, false, "", "native bridge action has no transport handler: " + request.action};
}

bool StopRequested() {
    return gStopRequested.load();
}

std::uint16_t NativeTransportPortForDiagnostics() {
    return gTransport.Port();
}

}  // namespace VectorworksMCP
