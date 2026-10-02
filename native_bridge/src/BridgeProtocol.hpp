#pragma once

#include <array>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace VectorworksMCP {
namespace Protocol {

// Wire protocol v2 (capability_revision 6). Framing is unchanged from v1:
// 4-byte big-endian length + UTF-8 JSON, max 16 MiB per frame.
constexpr int kWireProtocolVersion = 2;
constexpr std::uint32_t kMaxFrameBytes = 16u * 1024u * 1024u;
constexpr std::size_t kFrameHeaderBytes = 4u;

constexpr const char* kFieldId = "id";
constexpr const char* kFieldAction = "action";
constexpr const char* kFieldParams = "params";
constexpr const char* kFieldAuthToken = "auth_token";
constexpr const char* kFieldTimeoutMs = "timeout_ms";
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
    // Top-level `timeout_ms`; -1 when absent.
    std::int64_t timeoutMs = -1;
};

// retryable: -1 = omit, 0 = false, 1 = true.
struct ResponseEnvelope {
    std::string id;
    bool success = false;
    std::string resultJson;
    std::string error;
    std::string errorCode;
    int retryable = -1;
    std::string commitState;
    std::string detailsJson;
};

// Structured failure thrown by handlers. The dispatcher converts it into a
// v2 failure envelope: {error, error_code, retryable, commit_state, details}.
class BridgeError : public std::runtime_error {
public:
    BridgeError(
        std::string code,
        const std::string& message,
        bool retryable = false,
        std::string commitState = "not_started",
        std::string detailsJson = "")
        : std::runtime_error(message),
          code_(std::move(code)),
          retryable_(retryable),
          commitState_(std::move(commitState)),
          detailsJson_(std::move(detailsJson)) {}

    const std::string& Code() const noexcept { return code_; }
    bool Retryable() const noexcept { return retryable_; }
    const std::string& CommitState() const noexcept { return commitState_; }
    const std::string& DetailsJson() const noexcept { return detailsJson_; }

private:
    std::string code_;
    bool retryable_;
    std::string commitState_;
    std::string detailsJson_;
};

ResponseEnvelope Ok(const std::string& id, std::string resultJson);
ResponseEnvelope Fail(
    const std::string& id,
    const std::string& code,
    const std::string& message,
    bool retryable = false,
    const std::string& commitState = "not_started",
    const std::string& detailsJson = "");
ResponseEnvelope FromBridgeError(const std::string& id, const BridgeError& error);

std::array<std::uint8_t, kFrameHeaderBytes> EncodeFrameHeader(std::uint32_t payloadSize);
std::uint32_t DecodeFrameHeader(const std::array<std::uint8_t, kFrameHeaderBytes>& header);
RequestEnvelope ParseRequestEnvelope(std::string_view payload);
std::string SerializeResponseEnvelope(const ResponseEnvelope& response);

// Shared JSON string escaping (UTF-8 passes through unchanged).
std::string EscapeJsonString(std::string_view value);
// Appends the UTF-8 encoding of a Unicode scalar value.
void AppendUtf8(std::string& out, std::uint32_t codepoint);

}  // namespace Protocol
}  // namespace VectorworksMCP
