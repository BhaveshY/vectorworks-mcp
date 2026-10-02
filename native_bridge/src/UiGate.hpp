#pragma once

// Main-thread UI gate (contract 1.4). The WM_TIMER pump is dispatched by any
// message loop on the Vectorworks main thread, including nested loops for
// modal dialogs, menus, drags and file loads. CAD work must only run from the
// top-level idle loop, so every tick first classifies the UI with plain Win32
// calls (no SDK calls) and the scheduler dequeues only when the gate is open.

#include "RequestScheduler.hpp"

#include <atomic>
#include <string>

namespace VectorworksMCP::UiGate {

// Called on the main thread during module init; caches the main frame HWND.
void InitializeOnMainThread();

// Win32-only classification: Modal, Tracking, Loading or Idle. Never returns
// NoDocument (that needs the SDK and is decided by the caller when Idle).
UiState ComputeWin32State(std::string* reason = nullptr);

// Loading bracket for document lifecycle work started by the bridge.
void BeginLoading();
void EndLoading();
class ScopedLoading {
public:
    ScopedLoading() { BeginLoading(); }
    ~ScopedLoading() { EndLoading(); }
    ScopedLoading(const ScopedLoading&) = delete;
    ScopedLoading& operator=(const ScopedLoading&) = delete;
};

// Short settle window after a document switch/close/open was observed.
void NoteDocumentTransition(int settleMs = 250);

// Published state for the socket-thread ping.
void Publish(UiState state, const std::string& reason);
UiState Published();
std::string PublishedReason();

// True while the Vectorworks main frame exists and is visible; used by the
// deinit handler to tell a real application shutdown from a module release.
bool MainWindowAlive();
// True once a visible main frame was observed in this process.
bool MainWindowEverAlive();

}  // namespace VectorworksMCP::UiGate
