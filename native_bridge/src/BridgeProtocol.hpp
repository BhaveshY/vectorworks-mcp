#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <string_view>

namespace VectorworksMCP {
namespace Protocol {

constexpr std::uint32_t kMaxFrameBytes = 16u * 1024u * 1024u;
constexpr std::size_t kFrameHeaderBytes = 4u;

constexpr const char* kFieldId = "id";
constexpr const char* kFieldAction = "action";
constexpr const char* kFieldParams = "params";
constexpr const char* kFieldAuthToken = "auth_token";
constexpr const char* kFieldSuccess = "success";
constexpr const char* kFieldResult = "result";
constexpr const char* kFieldError = "error";

constexpr const char* kDispatchModeNativeSdk = "native_sdk";
constexpr const char* kBridgeKindPrefix = "native_sdk_bridge";

struct RequestEnvelope {
    std::string id;
    std::string action;
    std::string paramsJson;
    std::string authToken;
};

struct ResponseEnvelope {
    std::string id;
    bool success = false;
    std::string resultJson;
    std::string error;
};

std::array<std::uint8_t, kFrameHeaderBytes> EncodeFrameHeader(std::uint32_t payloadSize);
std::uint32_t DecodeFrameHeader(const std::array<std::uint8_t, kFrameHeaderBytes>& header);
RequestEnvelope ParseRequestEnvelope(std::string_view payload);
std::string SerializeResponseEnvelope(const ResponseEnvelope& response);

}  // namespace Protocol
}  // namespace VectorworksMCP
