#include "StdAfx.h"

#include "UiGate.hpp"

#include <chrono>
#include <mutex>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#endif

namespace VectorworksMCP::UiGate {

namespace {

std::atomic<int> gLoadingDepth{0};
std::atomic<std::int64_t> gSettleUntilMs{0};
std::atomic<int> gPublished{static_cast<int>(UiState::Loading)};
std::mutex gReasonMutex;
std::string gPublishedReason = "starting";
std::atomic_bool gMainWindowEverAlive{false};

#ifdef _WIN32
HWND gMainWindow = nullptr;
DWORD gMainThreadId = 0;

std::int64_t SteadyNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

HWND ResolveMainWindow() {
    if (gMainWindow && IsWindow(gMainWindow)) {
        return gMainWindow;
    }
    HWND window = nullptr;
#if defined(SDK_VERSION)
    try {
        window = ::GS_GetMainHWND(gCBP);
    } catch (...) {
        window = nullptr;
    }
#endif
    if (!window) {
        // Fallback: largest visible unowned top-level window of this thread.
        struct Search {
            HWND best = nullptr;
            LONG bestArea = 0;
        } search;
        EnumThreadWindows(
            GetCurrentThreadId(),
            [](HWND candidate, LPARAM param) -> BOOL {
                auto* state = reinterpret_cast<Search*>(param);
                if (!IsWindowVisible(candidate) || GetWindow(candidate, GW_OWNER) != nullptr) {
                    return TRUE;
                }
                RECT rect{};
                GetWindowRect(candidate, &rect);
                const LONG area = (rect.right - rect.left) * (rect.bottom - rect.top);
                if (area > state->bestArea) {
                    state->bestArea = area;
                    state->best = candidate;
                }
                return TRUE;
            },
            reinterpret_cast<LPARAM>(&search));
        window = search.best;
    }
    gMainWindow = window;
    if (window && IsWindowVisible(window)) gMainWindowEverAlive.store(true);
    return window;
}

bool ForegroundIsOurs() {
    HWND foreground = GetForegroundWindow();
    if (!foreground) return false;
    DWORD pid = 0;
    GetWindowThreadProcessId(foreground, &pid);
    return pid == GetCurrentProcessId();
}
#endif

}  // namespace

void InitializeOnMainThread() {
#ifdef _WIN32
    gMainThreadId = GetCurrentThreadId();
    gMainWindow = nullptr;
    ResolveMainWindow();
#endif
}

UiState ComputeWin32State(std::string* reason) {
#ifdef _WIN32
    const auto setReason = [&](const char* text) {
        if (reason) *reason = text;
    };
    if (gLoadingDepth.load() > 0) {
        setReason("document_lifecycle_in_progress");
        return UiState::Loading;
    }
    if (SteadyNowMs() < gSettleUntilMs.load()) {
        setReason("document_transition_settling");
        return UiState::Loading;
    }
    HWND main = ResolveMainWindow();
    if (!main || !IsWindow(main)) {
        setReason("main_window_unavailable");
        return UiState::Loading;
    }
    if (!IsWindowVisible(main)) {
        setReason("main_window_hidden");
        return UiState::Loading;
    }
    gMainWindowEverAlive.store(true);
    if (!IsWindowEnabled(main)) {
        setReason("main_window_disabled_modal_dialog");
        return UiState::Modal;
    }
    GUITHREADINFO info{};
    info.cbSize = sizeof(info);
    if (GetGUIThreadInfo(GetCurrentThreadId(), &info)) {
        if (info.flags & (GUI_INMENUMODE | GUI_POPUPMENUMODE | GUI_SYSTEMMENUMODE)) {
            setReason("menu_loop");
            return UiState::Tracking;
        }
        if (info.flags & GUI_INMOVESIZE) {
            setReason("window_move_size");
            return UiState::Tracking;
        }
        if (info.hwndCapture) {
            setReason("mouse_capture");
            return UiState::Tracking;
        }
    }
    if (GetCapture() != nullptr) {
        setReason("mouse_capture");
        return UiState::Tracking;
    }
    if (ForegroundIsOurs() &&
        ((GetAsyncKeyState(VK_LBUTTON) & 0x8000) ||
         (GetAsyncKeyState(VK_RBUTTON) & 0x8000) ||
         (GetAsyncKeyState(VK_MBUTTON) & 0x8000))) {
        setReason("mouse_button_down");
        return UiState::Tracking;
    }
    setReason("idle");
    return UiState::Idle;
#else
    if (reason) *reason = "idle";
    return gLoadingDepth.load() > 0 ? UiState::Loading : UiState::Idle;
#endif
}

void BeginLoading() {
    gLoadingDepth.fetch_add(1);
}

void EndLoading() {
    if (gLoadingDepth.fetch_sub(1) <= 1) {
        gLoadingDepth.store(0);
        NoteDocumentTransition();
    }
}

void NoteDocumentTransition(int settleMs) {
#ifdef _WIN32
    const std::int64_t until = SteadyNowMs() + settleMs;
    std::int64_t current = gSettleUntilMs.load();
    while (current < until && !gSettleUntilMs.compare_exchange_weak(current, until)) {
    }
#else
    (void) settleMs;
#endif
}

void Publish(UiState state, const std::string& reason) {
    gPublished.store(static_cast<int>(state));
    std::lock_guard<std::mutex> lock(gReasonMutex);
    gPublishedReason = reason;
}

UiState Published() {
    return static_cast<UiState>(gPublished.load());
}

std::string PublishedReason() {
    std::lock_guard<std::mutex> lock(gReasonMutex);
    return gPublishedReason;
}

bool MainWindowEverAlive() {
    return gMainWindowEverAlive.load();
}

bool MainWindowAlive() {
#ifdef _WIN32
    HWND main = gMainWindow;
    return main && IsWindow(main) && IsWindowVisible(main);
#else
    return true;
#endif
}

}  // namespace VectorworksMCP::UiGate
