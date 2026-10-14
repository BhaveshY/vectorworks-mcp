// Not using the precompiled header on purpose (see GridReader.hpp).
#include "GridReader.hpp"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#include <commctrl.h>
#include <oleacc.h>

#pragma comment(lib, "oleacc.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#endif

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <iterator>

namespace VectorworksMCP::GridReader {

#ifdef _WIN32
namespace {

// Per-call limits (single-threaded use: main thread only).
thread_local std::size_t gMaxRows = 50u;
thread_local std::size_t gMaxRowChars = 300u;
thread_local std::int64_t gBudgetMs = 1500;

std::int64_t SteadyNowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

std::string Narrow(const wchar_t* text, int length) {
    if (!text || length <= 0) return std::string();
    const int size = WideCharToMultiByte(CP_UTF8, 0, text, length, nullptr, 0, nullptr, nullptr);
    if (size <= 0) return std::string();
    std::string out(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text, length, out.data(), size, nullptr, nullptr);
    return out;
}

std::string TrimCopy(const std::string& text) {
    const auto first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return std::string();
    const auto last = text.find_last_not_of(" \t\r\n");
    return text.substr(first, last - first + 1);
}

std::string WindowText(HWND window) {
    wchar_t buffer[512] = {};
    const int length = InternalGetWindowText(window, buffer, static_cast<int>(std::size(buffer)));
    return Narrow(buffer, length);
}

std::string LowerAscii(std::string text) {
    for (auto& ch : text) {
        if (ch >= 'A' && ch <= 'Z') ch = static_cast<char>(ch - 'A' + 'a');
    }
    return text;
}

std::string WindowClass(HWND window) {
    wchar_t buffer[256] = {};
    const int length = GetClassNameW(window, buffer, static_cast<int>(std::size(buffer)));
    return Narrow(buffer, length);
}

std::string CleanRowText(const std::string& text) {
    std::string out;
    bool space = false;
    for (const char ch : text) {
        const bool ws = ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n';
        if (ws) {
            space = !out.empty();
            continue;
        }
        if (space) out += ' ';
        space = false;
        out += ch;
    }
    if (out.size() > gMaxRowChars) {
        out.resize(gMaxRowChars);
        // never cut a UTF-8 sequence in half
        while (!out.empty() && (static_cast<unsigned char>(out.back()) & 0xC0) == 0x80) out.pop_back();
        if (!out.empty() && (static_cast<unsigned char>(out.back()) & 0x80) != 0) out.pop_back();
        out += "...";
    }
    return out;
}

void AddRow(std::vector<std::string>& rows, const std::string& text) {
    const std::string clean = CleanRowText(text);
    if (clean.empty() || rows.size() >= gMaxRows) return;
    if (std::find(rows.begin(), rows.end(), clean) != rows.end()) return;
    rows.push_back(clean);
}

std::string TakeBstr(BSTR text) {
    if (!text) return std::string();
    std::string out = Narrow(text, static_cast<int>(SysStringLen(text)));
    SysFreeString(text);
    return out;
}

std::string AccText(IAccessible* acc, const VARIANT& child) {
    BSTR name = nullptr;
    BSTR value = nullptr;
    acc->get_accName(child, &name);
    acc->get_accValue(child, &value);
    const std::string n = TrimCopy(TakeBstr(name));
    const std::string v = TrimCopy(TakeBstr(value));
    if (v.empty() || v == n) return n;
    if (n.empty()) return v;
    return n + ": " + v;
}

bool AccSkippedRole(IAccessible* acc, const VARIANT& child) {
    VARIANT role;
    VariantInit(&role);
    bool skip = false;
    if (SUCCEEDED(acc->get_accRole(child, &role)) && role.vt == VT_I4) {
        skip = role.lVal == ROLE_SYSTEM_SCROLLBAR || role.lVal == ROLE_SYSTEM_GRIP ||
            role.lVal == ROLE_SYSTEM_TITLEBAR || role.lVal == ROLE_SYSTEM_MENUBAR;
    }
    VariantClear(&role);
    return skip;
}

// Texts of the children of `acc` (VT_DISPATCH -> the child's own name/value,
// VT_I4 -> a simple element of acc). With asRows, each child's own children
// (the cells) are joined with " | " into that child's row.
void CollectAccChildren(IAccessible* acc, std::vector<std::string>& rows, std::int64_t deadline, bool asRows) {
    long count = 0;
    if (FAILED(acc->get_accChildCount(&count)) || count <= 0) return;
    count = std::min<long>(count, 400);
    std::vector<VARIANT> children(static_cast<std::size_t>(count));
    for (auto& child : children) VariantInit(&child);
    long obtained = 0;
    if (FAILED(AccessibleChildren(acc, 0, count, children.data(), &obtained))) obtained = 0;
    for (long index = 0; index < obtained; ++index) {
        VARIANT& child = children[static_cast<std::size_t>(index)];
        if (rows.size() < gMaxRows && SteadyNowMs() < deadline) {
            if (child.vt == VT_DISPATCH && child.pdispVal) {
                IAccessible* childAcc = nullptr;
                if (SUCCEEDED(child.pdispVal->QueryInterface(IID_IAccessible, reinterpret_cast<void**>(&childAcc))) &&
                    childAcc) {
                    VARIANT self;
                    VariantInit(&self);
                    self.vt = VT_I4;
                    self.lVal = CHILDID_SELF;
                    if (!AccSkippedRole(childAcc, self)) {
                        std::string text = AccText(childAcc, self);
                        if (asRows) {
                            std::vector<std::string> cells;
                            CollectAccChildren(childAcc, cells, deadline, false);
                            std::string joined;
                            for (const auto& cell : cells) {
                                if (!joined.empty()) joined += " | ";
                                joined += cell;
                            }
                            if (text.empty()) {
                                text = joined;
                            } else if (!joined.empty() && joined.find(text) == std::string::npos) {
                                text += " | " + joined;
                            }
                        }
                        AddRow(rows, text);
                    }
                    childAcc->Release();
                }
            } else if (child.vt == VT_I4) {
                if (!AccSkippedRole(acc, child)) AddRow(rows, AccText(acc, child));
            }
        }
        VariantClear(&child);
    }
}

void ReadGridRowsMsaa(HWND grid, std::vector<std::string>* rows, std::string* error) {
    const std::int64_t deadline = SteadyNowMs() + gBudgetMs;
    IAccessible* acc = nullptr;
    HRESULT hr = AccessibleObjectFromWindow(grid, static_cast<DWORD>(OBJID_CLIENT), IID_IAccessible,
                                            reinterpret_cast<void**>(&acc));
    bool uninit = false;
    if (hr == CO_E_NOTINITIALIZED) {
        const HRESULT init = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        uninit = SUCCEEDED(init);
        hr = AccessibleObjectFromWindow(grid, static_cast<DWORD>(OBJID_CLIENT), IID_IAccessible,
                                        reinterpret_cast<void**>(&acc));
    }
    if (FAILED(hr) || !acc) {
        char buffer[48] = {};
        std::snprintf(buffer, sizeof(buffer), "msaa_unavailable_0x%08lX", static_cast<unsigned long>(hr));
        *error = buffer;
    } else {
        CollectAccChildren(acc, *rows, deadline, true);
        acc->Release();
        if (rows->empty()) *error = "msaa_no_rows";
    }
    if (uninit) CoUninitialize();
}

// SEH containment (no C++ objects with destructors in this frame).
unsigned long GuardedReadGridRows(HWND grid, std::vector<std::string>* rows, std::string* error) {
    __try {
        ReadGridRowsMsaa(grid, rows, error);
        return 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return GetExceptionCode();
    }
}

void ReadListViewRows(HWND list, std::vector<std::string>& rows) {
    const int items = static_cast<int>(SendMessageW(list, LVM_GETITEMCOUNT, 0, 0));
    HWND header = reinterpret_cast<HWND>(SendMessageW(list, LVM_GETHEADER, 0, 0));
    int columns = header ? static_cast<int>(SendMessageW(header, HDM_GETITEMCOUNT, 0, 0)) : 1;
    columns = std::clamp(columns, 1, 12);
    for (int item = 0; item < items && rows.size() < gMaxRows; ++item) {
        std::string row;
        for (int column = 0; column < columns; ++column) {
            wchar_t buffer[512] = {};
            LVITEMW lv{};
            lv.iSubItem = column;
            lv.pszText = buffer;
            lv.cchTextMax = static_cast<int>(std::size(buffer));
            const int length = static_cast<int>(
                SendMessageW(list, LVM_GETITEMTEXTW, static_cast<WPARAM>(item), reinterpret_cast<LPARAM>(&lv)));
            const std::string cell = TrimCopy(Narrow(buffer, length));
            if (cell.empty()) continue;
            if (!row.empty()) row += " | ";
            row += cell;
        }
        AddRow(rows, row);
    }
}

BOOL CALLBACK CollectChildTexts(HWND child, LPARAM param) {
    auto* rows = reinterpret_cast<std::vector<std::string>*>(param);
    if (IsWindowVisible(child)) AddRow(*rows, WindowText(child));
    return rows->size() < gMaxRows;
}

}  // namespace
#endif

Result ReadRows(void* gridWindow, std::size_t maxRows, std::size_t maxRowChars, int budgetMs) {
    Result read;
#ifdef _WIN32
    HWND grid = static_cast<HWND>(gridWindow);
    gMaxRows = maxRows ? maxRows : 50u;
    gMaxRowChars = maxRowChars ? maxRowChars : 300u;
    gBudgetMs = budgetMs > 0 ? budgetMs : 1500;
    if (!grid || !IsWindow(grid)) {
        read.method = "grid_window_not_found";
        return read;
    }
    if (GetWindowThreadProcessId(grid, nullptr) != GetCurrentThreadId()) {
        read.method = "not_owner_thread";
        return read;
    }
    std::string error;
    const unsigned long fault = GuardedReadGridRows(grid, &read.rows, &error);
    if (fault != 0) {
        char buffer[40] = {};
        std::snprintf(buffer, sizeof(buffer), "msaa_fault_0x%08lX", fault);
        error = buffer;
        read.rows.clear();
    }
    if (!read.rows.empty()) {
        read.captured = true;
        read.method = "msaa";
        return read;
    }
    if (LowerAscii(WindowClass(grid)) == "syslistview32") {
        ReadListViewRows(grid, read.rows);
        if (!read.rows.empty()) {
            read.captured = true;
            read.method = "list_view";
            return read;
        }
    }
    EnumChildWindows(grid, CollectChildTexts, reinterpret_cast<LPARAM>(&read.rows));
    if (!read.rows.empty()) {
        read.captured = true;
        read.method = "child_windows";
        return read;
    }
    read.method = error.empty() ? "no_rows" : error;
#else
    (void) gridWindow;
    (void) maxRows;
    (void) maxRowChars;
    (void) budgetMs;
    read.method = "unsupported_platform";
#endif
    return read;
}

}  // namespace VectorworksMCP::GridReader
