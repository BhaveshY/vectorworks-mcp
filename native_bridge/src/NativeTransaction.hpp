#pragma once

#include "VectorworksSDK.h"

#include <cstddef>
#include <exception>
#include <functional>
#include <string>
#include <vector>

namespace VectorworksMCP::Transactions {

enum class ObjectFamily {
    Simple,
    Space,
    Slab,
    Roof,
    Door,
    Window,
};

enum class ArtifactRole {
    FinalObject,
    TemporaryInput,
};

enum class UndoRegistration {
    Pending,
    Explicit,
    SdkManaged,
    NotApplicable,
};

enum class TransactionState {
    Active,
    Committed,
    RolledBack,
};

enum class ArtifactDisposition {
    Owned,
    RemovedBeforeCommit,
};

using ArtifactId = std::size_t;
using ExternalMutationId = std::size_t;
using SemanticVerifier = std::function<void(MCObjectHandle)>;

struct TransactionOptions {
    std::size_t expectedArtifactCount = 0;
    // AddAfterSwapObject(false) is accepted only for families explicitly
    // listed here after a family-specific live undo/redo proof.
    std::vector<ObjectFamily> sdkManagedRegistrationFamilies;
};

struct ArtifactReceipt {
    ArtifactId id = 0;
    std::string uuid;
    ObjectFamily family = ObjectFamily::Simple;
    ArtifactRole role = ArtifactRole::FinalObject;
    UndoRegistration undoRegistration = UndoRegistration::Pending;
    ArtifactDisposition disposition = ArtifactDisposition::Owned;
    short expectedNodeType = 0;
    short actualNodeType = 0;
    bool semanticallyVerified = false;
    bool absentBeforeCommit = false;
};

struct TransactionReceipt {
    bool committed = false;
    bool endUndoEventSucceeded = false;
    std::vector<ArtifactReceipt> artifacts;
    struct ExternalMutationReceipt {
        ExternalMutationId id = 0;
        std::string uuid;
        short expectedNodeType = 0;
        bool beforeRegistered = false;
        bool afterRegistered = false;
        bool deleted = false;
    };
    std::vector<ExternalMutationReceipt> externalMutations;
};

struct RollbackReceipt {
    bool undoAndRemoveAttempted = false;
    std::vector<std::string> survivorsAfterUndo;
    std::vector<std::string> cleanupFailures;
};

class NativeTransaction {
public:
    NativeTransaction(
        VectorWorks::ISDK& sdk,
        const TXString& undoName,
        TransactionOptions options = {});
    ~NativeTransaction();

    NativeTransaction(const NativeTransaction&) = delete;
    NativeTransaction& operator=(const NativeTransaction&) = delete;
    NativeTransaction(NativeTransaction&&) = delete;
    NativeTransaction& operator=(NativeTransaction&&) = delete;

    // Call while the creator's local deletion guard still owns handle. On a
    // successful return the transaction owns the UUID lifecycle, so the
    // creator must immediately release its local guard.
    ArtifactId AdoptFinal(
        MCObjectHandle handle,
        ObjectFamily family,
        short expectedNodeType,
        SemanticVerifier semanticVerifier);
    ArtifactId AdoptTemporary(MCObjectHandle handle, ObjectFamily family);

    // Supports a create/duplicate followed by deletion through a transaction-
    // local reference. The UUID is deleted and verified immediately and is
    // excluded from semantic verification and commit registration.
    void DisposeFinal(ArtifactId id);

    // Existing objects are registered before their first mutation. Surviving
    // objects are marked after their final mutation and registered at commit;
    // deleted objects deliberately have no AddAfter record.
    ExternalMutationId TrackExternalBefore(MCObjectHandle handle);
    void TrackExternalAfter(ExternalMutationId id, MCObjectHandle handle);
    void TrackExternalDeleted(ExternalMutationId id);

    MCObjectHandle Resolve(ArtifactId id) const;
    const std::string& Uuid(ArtifactId id) const;

    TransactionReceipt Commit();
    RollbackReceipt Rollback();
    [[noreturn]] void RollbackAndRethrow(std::exception_ptr originalFailure);

    TransactionState State() const noexcept { return state_; }

private:
    struct Artifact;
    struct ExternalMutation;

    Artifact& RequireArtifact(ArtifactId id);
    const Artifact& RequireArtifact(ArtifactId id) const;
    bool AllowsSdkManagedRegistration(ObjectFamily family) const;
    void RemoveTemporaryInputs();
    void VerifyFinalObjects();
    void RegisterFinalObjects();
    void RegisterExternalAfterStates();
    RollbackReceipt RollbackImpl() noexcept;
    static std::string DescribeException(std::exception_ptr failure);

    VectorWorks::ISDK& sdk_;
    TransactionOptions options_;
    TransactionState state_ = TransactionState::Active;
    std::vector<Artifact> artifacts_;
    std::vector<ExternalMutation> externalMutations_;
};

}  // namespace VectorworksMCP::Transactions
