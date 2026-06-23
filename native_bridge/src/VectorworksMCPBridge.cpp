#include "StdAfx.h"

#include "BridgeDispatcher.hpp"
#include "BridgeProtocol.hpp"
#include "CadRequestQueue.hpp"
#include "NativeTransport.hpp"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
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
constexpr auto kCadRequestTimeout = std::chrono::seconds(30);
constexpr bool kCadHandlersImplemented = VECTORWORKS_MCP_HAS_SDK != 0;

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

std::atomic_bool gMainContextPumpReady{false};
HWND gMainContextPumpWindow = nullptr;
ATOM gMainContextPumpWindowClass = 0;
HINSTANCE gMainContextPumpInstance = nullptr;
int gMainContextPumpModuleAnchor = 0;

LRESULT CALLBACK MainContextPumpWndProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    if (message == WM_TIMER && wParam == kMainContextPumpTimerId) {
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

constexpr const char* MainContextPumpName() {
    return "unavailable";
}
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
        SkipValue();
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
    return std::min(value, maxValue);
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

std::string RequiredAuthTokenFromEnvironment() {
    if (const char* token = std::getenv("VW_MCP_AUTH_TOKEN")) {
        if (token[0] != '\0') {
            return token;
        }
    }
    return "";
}

bool RequestAuthAccepted(const Protocol::RequestEnvelope& request) {
    const std::string requiredToken = RequiredAuthTokenFromEnvironment();
    return requiredToken.empty() || request.authToken == requiredToken;
}

Protocol::ResponseEnvelope HandlePingOnTransportThread(const Protocol::RequestEnvelope& request) {
#if VECTORWORKS_MCP_HAS_SDK
    const bool ready = CadHandlersRuntimeReady();
    std::string payload = R"({"pong":true,"version":"native-sdk-bridge-phase1","bridge_kind":"native_sdk_bridge_phase1","dispatch_mode":"native_sdk","handlers":8)";
    payload += ",\"cad_api_safe\":";
    payload += ready ? "true" : "false";
    payload += ",\"transport_only\":";
    payload += ready ? "false" : "true";
    payload += R"(,"native_bridge":true,"native_phase":1)";
    payload += ",\"implemented_actions\":[\"ping\",\"stop\",\"get_document_info\",\"get_layers\",\"get_objects\",\"selection\",\"create_object\",\"batch_create_objects\"]";
    payload += ",\"cad_handlers_implemented\":true";
    payload += ",\"main_context_pump\":";
    payload += JsonString(MainContextPumpName());
    payload += ",\"main_context_pump_ready\":";
    payload += ready ? "true" : "false";
    payload += "}";
    return {
        request.id,
        true,
        payload,
        "",
    };
#else
    return {
        request.id,
        true,
        R"({"pong":true,"version":"native-scaffold-phase0","bridge_kind":"native_sdk_bridge_scaffold","dispatch_mode":"native_sdk","handlers":2,"cad_api_safe":false,"transport_only":true,"native_bridge":true,"native_phase":0,"implemented_actions":["ping","stop"],"cad_handlers_implemented":false})",
        "",
    };
#endif
}

#if VECTORWORKS_MCP_HAS_SDK

std::string TxToUtf8(const TXString& value) {
    return value.GetStdString();
}

std::string HandleId(MCObjectHandle handle) {
    std::ostringstream out;
    out << "0x" << std::hex << reinterpret_cast<std::uintptr_t>(handle);
    return out.str();
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
        case kWallNode:
            return "wall";
        case kSlabNode:
            return "slab";
        case kParametricNode:
            return "parametric";
        default:
            return "type_" + std::to_string(static_cast<int>(type));
    }
}

bool MatchesObjectType(short actualType, std::string requestedType) {
    requestedType = ToLower(requestedType);
    if (requestedType.empty()) {
        return true;
    }
    if (requestedType == "rectangle" || requestedType == "box") {
        requestedType = "rect";
    }
    return ObjectTypeName(actualType) == requestedType;
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

std::string ObjectJson(MCObjectHandle object) {
    const short type = gSDK->GetObjectTypeN(object);
    TXString name;
    gSDK->GetObjectName(object, name);

    std::string json = "{\"handle\":";
    json += JsonString(HandleId(object));
    json += ",\"type\":";
    json += JsonString(ObjectTypeName(type));
    json += ",\"type_id\":";
    json += std::to_string(static_cast<int>(type));
    json += ",\"name\":";
    json += JsonString(TxToUtf8(name));

    const auto layerName = LayerNameForObject(object);
    if (!layerName.empty()) {
        json += ",\"layer\":";
        json += JsonString(layerName);
    }

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
        if (MatchesObjectType(type, objectType)) {
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

std::vector<MCObjectHandle> CollectSelectedObjects() {
    std::vector<MCObjectHandle> selected;
    gSDK->ForEachObjectN(allObjects + descendIntoAll + descendIntoViewports + descendIntoAuxLists, [&](MCObjectHandle object) {
        if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object)) && gSDK->IsSelected(object)) {
            selected.push_back(object);
        }
    });
    return selected;
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
        int matchedCount = 0;
        int selectedCount = 0;
        gSDK->DeselectAll();
        gSDK->ForEachObjectInCriteria(TXString(criteria.c_str()), [&](MCObjectHandle object) {
            if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object))) {
                ++matchedCount;
            }
            if (object && IsUserVisibleObjectType(gSDK->GetObjectTypeN(object)) && selectedCount < limit) {
                gSDK->SelectObject(object, true);
                ++selectedCount;
            }
        });
        return "{\"selected\":" + std::to_string(selectedCount)
            + ",\"matched\":" + std::to_string(matchedCount)
            + ",\"limit\":" + std::to_string(limit)
            + ",\"truncated\":" + (matchedCount > selectedCount ? "true" : "false") + "}";
    }
    if (action == "delete") {
        const auto selected = CollectSelectedObjects();
        if (selected.empty()) {
            return R"({"deleted":0})";
        }
        gSDK->SupportUndoAndRemove();
        gSDK->SetUndoMethod(kUndoSwapObjects);
        gSDK->NameUndoEvent(TXString("Vectorworks MCP delete selection"));
        int deleted = 0;
        try {
            for (MCObjectHandle object : selected) {
                gSDK->AddBeforeSwapObject(object);
                gSDK->DeleteObject(object, true);
                ++deleted;
            }
            gSDK->EndUndoEvent();
        } catch (...) {
            gSDK->UndoAndRemove();
            throw;
        }
        return "{\"deleted\":" + std::to_string(deleted) + "}";
    }
    throw std::invalid_argument("unsupported selection action: " + action);
}

struct PrimitiveSpec {
    std::string objectType;
    double x1 = 0.0;
    double y1 = 0.0;
    double x2 = 100.0;
    double y2 = 100.0;
    double radius = 50.0;
    double startAngle = 0.0;
    double sweepAngle = 90.0;
    std::string name;
    std::string className;
};

struct CreatedPrimitive {
    int index = 0;
    std::string objectType;
    MCObjectHandle handle = nullptr;
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
        layer = gSDK->CreateLayerN(TXString("Vectorworks MCP Layer"), 1.0);
        if (layer) {
            gSDK->AddAfterSwapObject(layer);
        }
    }
    if (!layer) {
        throw std::runtime_error("active Vectorworks document has no writable design layer");
    }
    gSDK->SetCurrentLayer(layer);
    return layer;
}

std::string CanonicalCreateObjectType(std::string objectType) {
    objectType = ToLower(objectType);
    if (objectType == "rectangle" || objectType == "box") {
        return "rect";
    }
    return objectType;
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
    throw std::invalid_argument("unsupported create_object type for native bridge phase 1: " + spec.objectType);
}

PrimitiveSpec ParsePrimitiveSpec(const Params& params, const std::string& label) {
    PrimitiveSpec spec;
    spec.objectType = GetStringParam(params, "object_type");
    if (spec.objectType.empty()) {
        spec.objectType = GetStringParam(params, "type");
    }
    spec.objectType = CanonicalCreateObjectType(spec.objectType);
    spec.x1 = GetFiniteNumberParam(params, "x1", 0.0);
    spec.y1 = GetFiniteNumberParam(params, "y1", 0.0);
    spec.x2 = GetFiniteNumberParam(params, "x2", 100.0);
    spec.y2 = GetFiniteNumberParam(params, "y2", 100.0);
    spec.radius = GetFiniteNumberParam(params, "radius", 50.0);
    spec.startAngle = GetFiniteNumberParam(params, "start_angle", 0.0);
    spec.sweepAngle = GetFiniteNumberParam(params, "sweep_angle", 90.0);
    spec.name = GetStringParam(params, "name");
    spec.className = GetStringParam(params, "class_name");
    ValidatePrimitiveSpec(spec, label);
    return spec;
}

MCObjectHandle CreatePrimitiveFromSpec(const PrimitiveSpec& spec) {
    EnsureWritableLayer();

    MCObjectHandle object = nullptr;
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
    }

    if (!object) {
        throw std::runtime_error("Vectorworks did not return a handle for created " + spec.objectType);
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
    return object;
}

std::string CreatedPrimitiveJson(const CreatedPrimitive& created) {
    std::string json = "{\"index\":";
    json += std::to_string(created.index);
    json += ",\"type\":";
    json += JsonString(created.objectType);
    json += ",\"handle\":";
    json += JsonString(HandleId(created.handle));
    json += "}";
    return json;
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

std::string HandleCreateObject(const Params& params) {
    const PrimitiveSpec spec = ParsePrimitiveSpec(params, "create_object");

    gSDK->SupportUndoAndRemove();
    gSDK->SetUndoMethod(kUndoSwapObjects);
    gSDK->NameUndoEvent(TXString("Vectorworks MCP create object"));

    try {
        MCObjectHandle object = CreatePrimitiveFromSpec(spec);
        gSDK->AddAfterSwapObject(object);
        gSDK->EndUndoEvent();

        std::string json = "{\"type\":";
        json += JsonString(spec.objectType);
        json += ",\"handle\":";
        json += JsonString(HandleId(object));
        json += "}";
        return json;
    } catch (...) {
        gSDK->UndoAndRemove();
        throw;
    }
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

    gSDK->SupportUndoAndRemove();
    gSDK->SetUndoMethod(kUndoSwapObjects);
    gSDK->NameUndoEvent(TXString("Vectorworks MCP atomic batch create objects"));

    std::vector<CreatedPrimitive> created;
    created.reserve(specs.size());
    try {
        for (std::size_t index = 0; index < specs.size(); ++index) {
            MCObjectHandle object = CreatePrimitiveFromSpec(specs[index]);
            gSDK->AddAfterSwapObject(object);
            created.push_back({static_cast<int>(index + 1u), specs[index].objectType, object});
        }
        gSDK->EndUndoEvent();
    } catch (...) {
        gSDK->UndoAndRemove();
        throw;
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
        if (request.action == "get_layers") {
            return {request.id, true, HandleGetLayers(), ""};
        }
        if (request.action == "get_objects") {
            return {request.id, true, HandleGetObjects(params), ""};
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
        return {request.id, false, "", "unknown native bridge CAD action: " + request.action};
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

void OnPluginLoadStartTransport() {
    gStopRequested.store(false);
    gCadQueue.ResetCancellation();
    try {
        StartMainContextPump();
        gTransport.Start(GetTransportOptionsFromEnvironment(), DispatchFromSocketWorker);
    } catch (...) {
        gStopRequested.store(true);
        StopMainContextPump();
        gCadQueue.CancelAll("native bridge transport failed to start");
    }
}

void OnPluginUnloadStopTransport() {
    gStopRequested.store(true);
    StopMainContextPump();
    gCadQueue.CancelAll("native bridge is unloading");
    gTransport.Stop();
}

void OnVectorworksMainPluginEvent() {
    if (gCadQueuePumpActive.exchange(true)) {
        return;
    }
    ScopedAtomicBoolReset resetPumpActive(gCadQueuePumpActive);
    while (auto request = gCadQueue.TryDequeueOnVectorworksMainContext()) {
        gCadQueue.CompleteFromVectorworksMainContext(DispatchCadRequestOnVectorworksMainContext(*request));
    }
}

Protocol::ResponseEnvelope DispatchFromSocketWorker(const Protocol::RequestEnvelope& request) {
    if (!RequestAuthAccepted(request)) {
        return {request.id, false, "", "native bridge authentication failed"};
    }
    if (request.action == "ping") {
        return HandlePingOnTransportThread(request);
    }
    if (request.action == "stop") {
        gStopRequested.store(true);
        gCadQueue.CancelAll("native bridge stop requested");
        return {request.id, true, R"("Native bridge stop requested")", ""};
    }
    if (RequiresCadMainContext(request.action)) {
        const ActionSpec* actionSpec = FindActionSpec(request.action);
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
        return gCadQueue.WaitForResponseOnSocketThread(
            request.id,
            kCadRequestTimeout,
            actionSpec != nullptr && actionSpec->mayWriteDocument);
    }
    return {request.id, false, "", "unknown native bridge action: " + request.action};
}

bool StopRequested() {
    return gStopRequested.load();
}

std::uint16_t NativeTransportPortForDiagnostics() {
    return gTransport.Port();
}

}  // namespace VectorworksMCP
