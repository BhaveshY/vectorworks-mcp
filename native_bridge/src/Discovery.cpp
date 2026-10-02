#include "StdAfx.h"

#include "Discovery.hpp"

#include "JsonParams.hpp"

#include <cstdlib>
#include <fstream>
#include <mutex>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#endif

namespace VectorworksMCP::Discovery {

namespace {

std::wstring Widen(const std::string& value) {
#ifdef _WIN32
    if (value.empty()) return std::wstring();
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0);
    std::wstring out(static_cast<std::size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), size);
    return out;
#else
    return std::wstring(value.begin(), value.end());
#endif
}

std::string Narrow(const std::wstring& value) {
#ifdef _WIN32
    if (value.empty()) return std::string();
    const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), size, nullptr, nullptr);
    return out;
#else
    return std::string(value.begin(), value.end());
#endif
}

std::uint32_t CurrentPid() {
#ifdef _WIN32
    return static_cast<std::uint32_t>(GetCurrentProcessId());
#else
    return 0u;
#endif
}

std::mutex gDiscoveryMutex;
bool gWritten = false;
std::wstring gWrittenPath;

// Removes the discovery file when the (pinned) module is torn down at
// process exit. Lock-free on purpose: other threads may already be gone.
struct ProcessExitCleanup {
    ~ProcessExitCleanup() {
#ifdef _WIN32
        if (gWritten && !gWrittenPath.empty()) {
            DeleteFileW(gWrittenPath.c_str());
        }
#endif
    }
};
ProcessExitCleanup gProcessExitCleanup;

}  // namespace

std::string StateDirectory() {
#ifdef _WIN32
    wchar_t buffer[32768] = {};
    const DWORD length = GetEnvironmentVariableW(L"USERPROFILE", buffer, 32768);
    if (length == 0 || length >= 32768) return std::string();
    std::wstring path(buffer, length);
    if (!path.empty() && path.back() != L'\\' && path.back() != L'/') path += L"\\";
    path += L".vectorworks-mcp";
    return Narrow(path);
#else
    const char* home = std::getenv("HOME");
    return home ? std::string(home) + "/.vectorworks-mcp" : std::string();
#endif
}

std::string InstancesDirectory() {
    const std::string root = StateDirectory();
    return root.empty() ? root : root + "\\instances";
}

std::string InstanceFilePath() {
    const std::string dir = InstancesDirectory();
    return dir.empty() ? dir : dir + "\\" + std::to_string(CurrentPid()) + ".json";
}

std::string InstanceJson(const InstanceInfo& info) {
    std::string json = "{\"pid\":" + std::to_string(info.pid);
    json += ",\"port\":" + std::to_string(info.port);
    json += ",\"protocol\":" + std::to_string(info.protocol);
    json += ",\"capability_revision\":" + std::to_string(info.capabilityRevision);
    json += ",\"bridge_version\":" + JsonString(info.bridgeVersion);
    json += ",\"build_id\":" + JsonString(info.buildId);
    json += ",\"vectorworks_version\":" + JsonString(info.vectorworksVersion);
    json += ",\"session_id\":" + JsonString(info.sessionId);
    json += ",\"started_at\":" + JsonString(info.startedAt);
    json += "}";
    return json;
}

bool Write(const InstanceInfo& info, std::string* error) {
#ifdef _WIN32
    std::lock_guard<std::mutex> lock(gDiscoveryMutex);
    const std::string root = StateDirectory();
    if (root.empty()) {
        if (error) *error = "USERPROFILE is not set";
        return false;
    }
    const std::wstring rootW = Widen(root);
    const std::wstring dirW = Widen(InstancesDirectory());
    CreateDirectoryW(rootW.c_str(), nullptr);
    CreateDirectoryW(dirW.c_str(), nullptr);
    const std::wstring finalPath = Widen(InstanceFilePath());
    const std::wstring tempPath = finalPath + L".tmp";
    const std::string payload = InstanceJson(info);
    HANDLE file = CreateFileW(tempPath.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        if (error) *error = "could not create discovery temp file";
        return false;
    }
    DWORD written = 0;
    const BOOL ok = WriteFile(file, payload.data(), static_cast<DWORD>(payload.size()), &written, nullptr);
    FlushFileBuffers(file);
    CloseHandle(file);
    if (!ok || written != payload.size()) {
        DeleteFileW(tempPath.c_str());
        if (error) *error = "could not write discovery temp file";
        return false;
    }
    if (!MoveFileExW(tempPath.c_str(), finalPath.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        DeleteFileW(tempPath.c_str());
        if (error) *error = "could not rename discovery file into place";
        return false;
    }
    gWritten = true;
    gWrittenPath = finalPath;
    return true;
#else
    (void) info;
    if (error) *error = "discovery files are only implemented on Windows";
    return false;
#endif
}

void Remove() noexcept {
#ifdef _WIN32
    try {
        std::lock_guard<std::mutex> lock(gDiscoveryMutex);
        if (gWritten && !gWrittenPath.empty()) {
            DeleteFileW(gWrittenPath.c_str());
        }
        gWritten = false;
    } catch (...) {
    }
#endif
}

void AppendLog(const std::string& state, const std::string& detail) noexcept {
    try {
        const std::string root = StateDirectory();
        if (root.empty()) return;
        std::ofstream output(Widen(root + "\\native-bridge-startup.log"), std::ios::app);
        if (output) {
            output << IsoUtcNow() << " pid=" << CurrentPid() << " " << state << ": " << detail << '\n';
        }
    } catch (...) {
    }
}

}  // namespace VectorworksMCP::Discovery
