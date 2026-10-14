// Not using the precompiled header on purpose (see ImageTranscode.hpp).
#include "ImageTranscode.hpp"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#include <wincodec.h>
#include <wrl/client.h>

#include <cstdio>

#pragma comment(lib, "windowscodecs.lib")
#pragma comment(lib, "ole32.lib")
#endif

namespace VectorworksMCP::ImageTranscode {

bool TranscodeFile(const std::wstring& input, const std::wstring& output, const std::string& container,
                   std::string& error) {
#ifdef _WIN32
    GUID format = GUID_ContainerFormatPng;
    if (container == "jpeg") format = GUID_ContainerFormatJpeg;
    else if (container == "bmp") format = GUID_ContainerFormatBmp;
    else if (container == "tiff") format = GUID_ContainerFormatTiff;
    else if (container != "png") {
        error = "unsupported target container";
        return false;
    }
    const HRESULT init = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    const bool balance = SUCCEEDED(init);  // S_OK or S_FALSE need CoUninitialize
    bool ok = false;
    {
        using Microsoft::WRL::ComPtr;
        ComPtr<IWICImagingFactory> factory;
        ComPtr<IWICBitmapDecoder> decoder;
        ComPtr<IWICBitmapFrameDecode> frame;
        ComPtr<IWICFormatConverter> converter;
        ComPtr<IWICStream> stream;
        ComPtr<IWICBitmapEncoder> encoder;
        ComPtr<IWICBitmapFrameEncode> frameEncode;
        ComPtr<IPropertyBag2> properties;
        UINT width = 0, height = 0;
        WICPixelFormatGUID pixelFormat = GUID_WICPixelFormat24bppBGR;
        HRESULT hr = CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&factory));
        if (SUCCEEDED(hr)) {
            hr = factory->CreateDecoderFromFilename(input.c_str(), nullptr, GENERIC_READ,
                                                    WICDecodeMetadataCacheOnDemand, &decoder);
        }
        if (SUCCEEDED(hr)) hr = decoder->GetFrame(0, &frame);
        if (SUCCEEDED(hr)) hr = frame->GetSize(&width, &height);
        if (SUCCEEDED(hr)) hr = factory->CreateFormatConverter(&converter);
        if (SUCCEEDED(hr)) {
            hr = converter->Initialize(frame.Get(), GUID_WICPixelFormat24bppBGR, WICBitmapDitherTypeNone, nullptr, 0.0,
                                       WICBitmapPaletteTypeCustom);
        }
        if (SUCCEEDED(hr)) hr = factory->CreateStream(&stream);
        if (SUCCEEDED(hr)) hr = stream->InitializeFromFilename(output.c_str(), GENERIC_WRITE);
        if (SUCCEEDED(hr)) hr = factory->CreateEncoder(format, nullptr, &encoder);
        if (SUCCEEDED(hr)) hr = encoder->Initialize(stream.Get(), WICBitmapEncoderNoCache);
        if (SUCCEEDED(hr)) hr = encoder->CreateNewFrame(&frameEncode, &properties);
        if (SUCCEEDED(hr)) hr = frameEncode->Initialize(properties.Get());
        if (SUCCEEDED(hr)) hr = frameEncode->SetSize(width, height);
        if (SUCCEEDED(hr)) hr = frameEncode->SetPixelFormat(&pixelFormat);
        if (SUCCEEDED(hr) && !IsEqualGUID(pixelFormat, GUID_WICPixelFormat24bppBGR)) hr = E_FAIL;
        if (SUCCEEDED(hr)) hr = frameEncode->WriteSource(converter.Get(), nullptr);
        if (SUCCEEDED(hr)) hr = frameEncode->Commit();
        if (SUCCEEDED(hr)) hr = encoder->Commit();
        ok = SUCCEEDED(hr);
        if (!ok) {
            char buffer[48];
            std::snprintf(buffer, sizeof(buffer), "WIC transcode failed: 0x%08lX", static_cast<unsigned long>(hr));
            error = buffer;
        }
    }
    if (balance) CoUninitialize();
    return ok;
#else
    (void) input;
    (void) output;
    (void) container;
    error = "transcoding is only available on Windows";
    return false;
#endif
}

}  // namespace VectorworksMCP::ImageTranscode
