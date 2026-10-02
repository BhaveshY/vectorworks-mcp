//
// ModuleMain.cpp - entry point of the VectorworksMCPBridge plug-in module.
//
// The module registers no tools or objects (the SDK sample registrations were
// removed in 1.0). It only starts the native MCP bridge on kPluginModuleInit
// and asks Vectorworks not to cache it, so it initialises on every launch.
//

#include "StdAfx.h"

namespace VectorworksMCP {
void OnPluginLoadStartTransport();
void OnPluginModuleDeinit();
}  // namespace VectorworksMCP

const char* DefaultPluginVWRIdentifier() { return "VectorworksMCPBridge"; }

extern "C" Sint32 GS_EXTERNAL_ENTRY plugin_module_ver() { return SDK_VERSION; }

extern "C" Sint32 GS_EXTERNAL_ENTRY plugin_module_main(
    Sint32 action,
    void* moduleInfo,
    const VWIID& iid,
    IVWUnknown*& inOutInterface,
    CallBackPtr cbp) {
    (void) moduleInfo;
    (void) iid;
    (void) inOutInterface;

    ::GS_InitializeVCOM(cbp);

    Sint32 reply = 0L;
    try {
        if (action == kPluginModuleInit) {
            VectorworksMCP::OnPluginLoadStartTransport();
            reply = kDoNotCache;
        } else if (action == kPluginModuleDeinit) {
            VectorworksMCP::OnPluginModuleDeinit();
        }
    } catch (...) {
        // Never let an exception cross the plug-in boundary.
    }
    return reply;
}
