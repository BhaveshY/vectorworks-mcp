#pragma once

// WIC (Windows Imaging Component) re-encoding of exported images. Compiled in
// its own translation unit without the Vectorworks SDK headers (the SDK's
// global `UUID` collides with the Windows RPC typedef pulled in by WIC).

#include <string>

namespace VectorworksMCP::ImageTranscode {

// Re-encodes `input` into `output` as `container` ("png" lossless 24-bit,
// "jpeg", "bmp" or "tiff"). Returns false and sets `error` on failure.
bool TranscodeFile(const std::wstring& input, const std::wstring& output, const std::string& container,
                   std::string& error);

}  // namespace VectorworksMCP::ImageTranscode
