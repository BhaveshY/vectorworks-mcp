#pragma once

// Documents, identity and object references (contract 1.6 / 1.7).
//
// * Every open document gets a generated fingerprint ("vw-doc-...") that is
//   stable for its whole open lifetime (untitled documents, Save As) and
//   differs between simultaneously open documents. Fingerprints are keyed by
//   the SDK fileRef from GetOpenFilesList plus the drawing-header handle of
//   the active document, and dropped when the document closes.
// * The registry is refreshed on the main thread (pump ticks and before every
//   request); a snapshot is cached for the socket-thread ping.
// * `handle:` references map through a table {handle_id -> uuid, fingerprint};
//   raw MCObjectHandle values are never dereferenced from client input.

#include "JsonParams.hpp"

#include <cstdint>
#include <string>
#include <vector>

#if defined(SDK_VERSION)
#include "VectorworksSDK.h"
#endif

namespace VectorworksMCP::Docs {

struct DocumentInfo {
    std::string fingerprint;
    std::string name;
    std::string path;
    // -1 unknown, 0 clean, 1 dirty
    int dirty = -1;
    bool active = false;
    bool untitled = false;
    std::int32_t fileRef = -1;
};

// Main thread only.
void Refresh(bool force = true);
std::vector<DocumentInfo> List();
bool HasActiveDocument();
std::string ActiveFingerprint();
const DocumentInfo* FindByFingerprint(const std::string& fingerprint);
std::uint64_t Generation();

std::string DocumentJson(const DocumentInfo& info);
std::string ActiveDocumentJson();
std::string OpenDocumentsJson();

// expect_document_fingerprint / activate_document pre-check for every request.
void EnsureExpectedDocument(const Params& params);

// Socket-thread safe cached snapshot (updated by Refresh()).
std::string CachedActiveDocumentJson();
int CachedOpenDocumentCount();

// Lifecycle actions.
std::string HandleListDocuments(const Params& params);
std::string HandleActivateDocument(const Params& params);
std::string HandleNewDocument(const Params& params);
std::string HandleCloseDocument(const Params& params);
std::string HandleOpenDocument(const Params& params);
std::string HandleSaveDocument(const Params& params);

// Windows path normalisation for paths reported by GetOpenFilesList.
std::string NormalizeReportedPath(std::string path);

void ResetForModuleInit();

}  // namespace VectorworksMCP::Docs

#if defined(SDK_VERSION)
namespace VectorworksMCP::Handles {

// Returns the stable handle id for an object in the active document,
// registering it on first use. Objects without a UUID get an id that can
// never be resolved (stale_reference).
std::string IdFor(MCObjectHandle object);
// Resolves a handle id; throws BridgeError("stale_reference") when the id is
// unknown, belongs to another document or the object no longer exists.
MCObjectHandle Resolve(const std::string& handleId);
void ClearForDocument(const std::string& fingerprint);
void ClearAll();
std::size_t Count();

}  // namespace VectorworksMCP::Handles
#endif
