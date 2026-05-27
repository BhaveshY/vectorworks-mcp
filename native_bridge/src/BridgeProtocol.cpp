#include "BridgeProtocol.hpp"

#include <stdexcept>

namespace VectorworksMCP {
namespace Protocol {

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

}  // namespace Protocol
}  // namespace VectorworksMCP
