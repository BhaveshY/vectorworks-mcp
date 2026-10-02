#pragma once

// Instance discovery file (contract 1.2):
//   %USERPROFILE%\.vectorworks-mcp\instances\<pid>.json
// written atomically (temp file + rename) after a successful bind and removed
// on module deinit / clean shutdown / process exit.

#include <cstdint>
#include <string>

namespace VectorworksMCP::Discovery {

struct InstanceInfo {
    std::uint32_t pid = 0;
    std::uint16_t port = 0;
    int protocol = 2;
    std::uint32_t capabilityRevision = 6;
    std::string bridgeVersion;
    std::string buildId;
    std::string vectorworksVersion;
    std::string sessionId;
    std::string startedAt;
};

std::string StateDirectory();      // %USERPROFILE%\.vectorworks-mcp
std::string InstancesDirectory();  // ...\instances
std::string InstanceFilePath();    // ...\instances\<pid>.json
std::string InstanceJson(const InstanceInfo& info);
bool Write(const InstanceInfo& info, std::string* error = nullptr);
void Remove() noexcept;
void AppendLog(const std::string& state, const std::string& detail) noexcept;

}  // namespace VectorworksMCP::Discovery
