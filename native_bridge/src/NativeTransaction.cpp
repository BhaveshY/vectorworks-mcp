#include "StdAfx.h"

#include "NativeTransaction.hpp"

#include <algorithm>
#include <sstream>
#include <stdexcept>
#include <utility>

namespace VectorworksMCP::Transactions {

struct NativeTransaction::Artifact {
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
    SemanticVerifier semanticVerifier;
};

struct NativeTransaction::ExternalMutation {
    ExternalMutationId id = 0;
    std::string uuid;
    short expectedNodeType = 0;
    bool beforeRegistered = false;
    bool afterRequired = false;
    bool afterRegistered = false;
    bool deleted = false;
};

namespace {

std::string Join(const std::vector<std::string>& values) {
    std::ostringstream result;
    for (std::size_t index = 0; index < values.size(); ++index) {
        if (index != 0u) {
            result << ", ";
        }
        result << values[index];
    }
    return result.str();
}

}  // namespace

NativeTransaction::NativeTransaction(
    VectorWorks::ISDK& sdk,
    const TXString& undoName,
    TransactionOptions options)
    : sdk_(sdk), options_(std::move(options)) {
    artifacts_.reserve(options_.expectedArtifactCount);
    externalMutations_.reserve(options_.expectedArtifactCount);
    sdk_.SupportUndoAndRemove();
    sdk_.SetUndoMethod(kUndoSwapObjects);
    sdk_.NameUndoEvent(undoName);
}

NativeTransaction::~NativeTransaction() {
    if (state_ == TransactionState::Active) {
        try {
            RollbackImpl();
        } catch (...) {
            // Destructors cannot report rollback diagnostics. Production
            // callers use Commit or RollbackAndRethrow, both of which do.
        }
    }
}

ArtifactId NativeTransaction::AdoptFinal(
    MCObjectHandle handle,
    ObjectFamily family,
    short expectedNodeType,
    SemanticVerifier semanticVerifier) {
    if (state_ != TransactionState::Active) {
        throw std::logic_error("cannot adopt an object outside an active native transaction");
    }
    if (!handle) {
        throw std::invalid_argument("cannot adopt a null final Vectorworks object");
    }
    if (expectedNodeType == 0) {
        throw std::invalid_argument("a final Vectorworks object requires an expected native node type");
    }
    if (!semanticVerifier) {
        throw std::invalid_argument("a final Vectorworks object requires a semantic re-verifier");
    }

    TXString uuidValue;
    if (!sdk_.GetObjectUuid(handle, uuidValue) || uuidValue.IsEmpty()) {
        throw std::runtime_error("Vectorworks did not return a UUID before final-object adoption");
    }
    const std::string uuid = uuidValue.GetStdString();
    auto duplicate = std::find_if(
        artifacts_.begin(),
        artifacts_.end(),
        [&](const Artifact& artifact) { return artifact.uuid == uuid; });
    if (duplicate != artifacts_.end()) {
        // Some SDK compound constructors may consume/convert their source
        // profile in place. Promote that exact UUID instead of retaining a
        // false temporary record that Commit would delete.
        if (duplicate->role == ArtifactRole::TemporaryInput) {
            duplicate->role = ArtifactRole::FinalObject;
            duplicate->family = family;
            duplicate->undoRegistration = UndoRegistration::Pending;
            duplicate->expectedNodeType = expectedNodeType;
            duplicate->semanticVerifier = std::move(semanticVerifier);
            return duplicate->id;
        }
        throw std::logic_error("Vectorworks object UUID was adopted more than once: " + uuid);
    }

    const ArtifactId id = artifacts_.size();
    Artifact artifact;
    artifact.id = id;
    artifact.uuid = uuid;
    artifact.family = family;
    artifact.role = ArtifactRole::FinalObject;
    artifact.expectedNodeType = expectedNodeType;
    artifact.semanticVerifier = std::move(semanticVerifier);
    // The push happens while the creator's local guard is still active. If
    // allocation fails, ownership has not transferred and the guard deletes.
    artifacts_.push_back(std::move(artifact));
    return id;
}

ArtifactId NativeTransaction::AdoptTemporary(
    MCObjectHandle handle,
    ObjectFamily family) {
    if (state_ != TransactionState::Active) {
        throw std::logic_error("cannot adopt an object outside an active native transaction");
    }
    if (!handle) {
        throw std::invalid_argument("cannot adopt a null temporary Vectorworks object");
    }

    TXString uuidValue;
    if (!sdk_.GetObjectUuid(handle, uuidValue) || uuidValue.IsEmpty()) {
        throw std::runtime_error("Vectorworks did not return a UUID before temporary-object adoption");
    }
    const std::string uuid = uuidValue.GetStdString();
    const auto duplicate = std::find_if(
        artifacts_.begin(),
        artifacts_.end(),
        [&](const Artifact& artifact) { return artifact.uuid == uuid; });
    if (duplicate != artifacts_.end()) {
        throw std::logic_error("Vectorworks object UUID was adopted more than once: " + uuid);
    }

    const ArtifactId id = artifacts_.size();
    Artifact artifact;
    artifact.id = id;
    artifact.uuid = uuid;
    artifact.family = family;
    artifact.role = ArtifactRole::TemporaryInput;
    artifact.undoRegistration = UndoRegistration::NotApplicable;
    artifacts_.push_back(std::move(artifact));
    return id;
}

void NativeTransaction::DisposeFinal(ArtifactId id) {
    if (state_ != TransactionState::Active) {
        throw std::logic_error("cannot dispose an object outside an active native transaction");
    }
    Artifact& artifact = RequireArtifact(id);
    if (artifact.role != ArtifactRole::FinalObject) {
        throw std::invalid_argument("only a final transaction object can be explicitly disposed");
    }
    if (artifact.disposition != ArtifactDisposition::Owned) {
        throw std::logic_error("final transaction object was already disposed: " + artifact.uuid);
    }
    MCObjectHandle handle = sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str()));
    if (!handle) {
        throw std::runtime_error(
            "final transaction object disappeared before explicit disposal: " + artifact.uuid);
    }
    sdk_.DeleteObject(handle, false);
    if (sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str())) != nullptr) {
        throw std::runtime_error(
            "Vectorworks did not dispose the exact final-object UUID: " + artifact.uuid);
    }
    artifact.disposition = ArtifactDisposition::RemovedBeforeCommit;
    artifact.undoRegistration = UndoRegistration::NotApplicable;
    artifact.absentBeforeCommit = true;
}

ExternalMutationId NativeTransaction::TrackExternalBefore(MCObjectHandle handle) {
    if (state_ != TransactionState::Active) {
        throw std::logic_error("cannot track an external object outside an active native transaction");
    }
    if (!handle) {
        throw std::invalid_argument("cannot track a null external Vectorworks object");
    }
    TXString uuidValue;
    if (!sdk_.GetObjectUuid(handle, uuidValue) || uuidValue.IsEmpty()) {
        throw std::runtime_error("external Vectorworks object has no UUID for undo tracking");
    }
    const std::string uuid = uuidValue.GetStdString();
    const auto existing = std::find_if(
        externalMutations_.begin(),
        externalMutations_.end(),
        [&](const ExternalMutation& mutation) { return mutation.uuid == uuid; });
    if (existing != externalMutations_.end()) {
        return existing->id;
    }

    const ExternalMutationId id = externalMutations_.size();
    ExternalMutation mutation;
    mutation.id = id;
    mutation.uuid = uuid;
    mutation.expectedNodeType = sdk_.GetObjectTypeN(handle);
    // Persist the identity before AddBefore. A false/throwing AddBefore then
    // still leaves a complete transaction diagnostic and rollback scope.
    externalMutations_.push_back(std::move(mutation));
    if (!sdk_.AddBeforeSwapObject(handle)) {
        throw std::runtime_error(
            "Vectorworks rejected external-object before-state undo registration: " + uuid);
    }
    externalMutations_[id].beforeRegistered = true;
    return id;
}

void NativeTransaction::TrackExternalAfter(
    ExternalMutationId id,
    MCObjectHandle handle) {
    if (state_ != TransactionState::Active) {
        throw std::logic_error("cannot track an external object outside an active native transaction");
    }
    if (id >= externalMutations_.size() || !handle) {
        throw std::invalid_argument("invalid external-object after-state tracking request");
    }
    ExternalMutation& mutation = externalMutations_[id];
    TXString uuidValue;
    if (!sdk_.GetObjectUuid(handle, uuidValue) ||
        uuidValue.GetStdString() != mutation.uuid ||
        sdk_.GetObjectTypeN(handle) != mutation.expectedNodeType) {
        throw std::runtime_error(
            "external Vectorworks object changed semantic identity during mutation: " +
            mutation.uuid);
    }
    if (mutation.deleted) {
        throw std::logic_error("deleted external object cannot have an after state: " + mutation.uuid);
    }
    mutation.afterRequired = true;
}

void NativeTransaction::TrackExternalDeleted(ExternalMutationId id) {
    if (state_ != TransactionState::Active) {
        throw std::logic_error("cannot track an external object outside an active native transaction");
    }
    if (id >= externalMutations_.size()) {
        throw std::invalid_argument("invalid external-object deletion tracking request");
    }
    ExternalMutation& mutation = externalMutations_[id];
    if (sdk_.GetObjectByUuid(TXString(mutation.uuid.c_str())) != nullptr) {
        throw std::runtime_error(
            "external Vectorworks object still exists after requested deletion: " + mutation.uuid);
    }
    mutation.deleted = true;
    mutation.afterRequired = false;
}

MCObjectHandle NativeTransaction::Resolve(ArtifactId id) const {
    const Artifact& artifact = RequireArtifact(id);
    return sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str()));
}

const std::string& NativeTransaction::Uuid(ArtifactId id) const {
    return RequireArtifact(id).uuid;
}

TransactionReceipt NativeTransaction::Commit() {
    if (state_ != TransactionState::Active) {
        throw std::logic_error("native transaction is not active");
    }

    try {
        RemoveTemporaryInputs();
        VerifyFinalObjects();
        RegisterFinalObjects();
        RegisterExternalAfterStates();

        // Build every potentially allocating part of the receipt before the
        // undo event is ended. After a successful EndUndoEvent no operation
        // in this function may throw.
        TransactionReceipt receipt;
        receipt.artifacts.reserve(artifacts_.size());
        for (const Artifact& artifact : artifacts_) {
            receipt.artifacts.push_back({
                artifact.id,
                artifact.uuid,
                artifact.family,
                artifact.role,
                artifact.undoRegistration,
                artifact.disposition,
                artifact.expectedNodeType,
                artifact.actualNodeType,
                artifact.semanticallyVerified,
                artifact.absentBeforeCommit,
            });
        }
        receipt.externalMutations.reserve(externalMutations_.size());
        for (const ExternalMutation& mutation : externalMutations_) {
            receipt.externalMutations.push_back({
                mutation.id,
                mutation.uuid,
                mutation.expectedNodeType,
                mutation.beforeRegistered,
                mutation.afterRegistered,
                mutation.deleted,
            });
        }

        if (!sdk_.EndUndoEvent()) {
            throw std::runtime_error("Vectorworks failed to commit the native undo event");
        }
        state_ = TransactionState::Committed;
        receipt.committed = true;
        receipt.endUndoEventSucceeded = true;
        return receipt;
    } catch (...) {
        RollbackAndRethrow(std::current_exception());
    }
}

RollbackReceipt NativeTransaction::Rollback() {
    RollbackReceipt receipt = RollbackImpl();
    if (!receipt.cleanupFailures.empty()) {
        throw std::runtime_error(
            "native transaction rollback integrity failure; exact UUID cleanup failed for: " +
            Join(receipt.cleanupFailures));
    }
    return receipt;
}

[[noreturn]] void NativeTransaction::RollbackAndRethrow(
    std::exception_ptr originalFailure) {
    if (!originalFailure) {
        originalFailure = std::make_exception_ptr(
            std::runtime_error("native transaction failed without an exception payload"));
    }

    const RollbackReceipt rollback = RollbackImpl();
    if (rollback.cleanupFailures.empty()) {
        std::rethrow_exception(originalFailure);
    }

    const std::string originalMessage = DescribeException(originalFailure);
    const std::string rollbackMessage =
        "native transaction rollback integrity failure after: " + originalMessage +
        "; exact UUID cleanup failed for: " + Join(rollback.cleanupFailures);
    try {
        std::rethrow_exception(originalFailure);
    } catch (...) {
        std::throw_with_nested(std::runtime_error(rollbackMessage));
    }
}

NativeTransaction::Artifact& NativeTransaction::RequireArtifact(ArtifactId id) {
    if (id >= artifacts_.size()) {
        throw std::out_of_range("native transaction artifact ID is out of range");
    }
    return artifacts_[id];
}

const NativeTransaction::Artifact& NativeTransaction::RequireArtifact(
    ArtifactId id) const {
    if (id >= artifacts_.size()) {
        throw std::out_of_range("native transaction artifact ID is out of range");
    }
    return artifacts_[id];
}

bool NativeTransaction::AllowsSdkManagedRegistration(ObjectFamily family) const {
    return std::find(
               options_.sdkManagedRegistrationFamilies.begin(),
               options_.sdkManagedRegistrationFamilies.end(),
               family) != options_.sdkManagedRegistrationFamilies.end();
}

void NativeTransaction::RemoveTemporaryInputs() {
    for (Artifact& artifact : artifacts_) {
        if (artifact.role != ArtifactRole::TemporaryInput) {
            continue;
        }
        MCObjectHandle handle = sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str()));
        if (handle) {
            sdk_.DeleteObject(handle, false);
        }
        if (sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str())) != nullptr) {
            throw std::runtime_error(
                "Vectorworks temporary input survived pre-commit cleanup: " + artifact.uuid);
        }
        artifact.disposition = ArtifactDisposition::RemovedBeforeCommit;
        artifact.absentBeforeCommit = true;
    }
}

void NativeTransaction::VerifyFinalObjects() {
    for (Artifact& artifact : artifacts_) {
        if (artifact.role != ArtifactRole::FinalObject) {
            continue;
        }
        if (artifact.disposition == ArtifactDisposition::RemovedBeforeCommit) {
            continue;
        }
        MCObjectHandle handle = sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str()));
        if (!handle) {
            throw std::runtime_error(
                "Vectorworks final object disappeared before commit: " + artifact.uuid);
        }
        const short actualNodeType = sdk_.GetObjectTypeN(handle);
        if (actualNodeType != artifact.expectedNodeType) {
            throw std::runtime_error(
                "Vectorworks final object changed native node type before commit: " + artifact.uuid);
        }
        artifact.semanticVerifier(handle);

        // A verifier may reset/regenerate a compound object. Re-resolve from
        // UUID instead of trusting the pre-verification raw handle.
        handle = sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str()));
        if (!handle || sdk_.GetObjectTypeN(handle) != artifact.expectedNodeType) {
            throw std::runtime_error(
                "Vectorworks final object failed identity readback after semantic verification: " +
                artifact.uuid);
        }
        artifact.actualNodeType = artifact.expectedNodeType;
        artifact.semanticallyVerified = true;
    }
}

void NativeTransaction::RegisterFinalObjects() {
    for (Artifact& artifact : artifacts_) {
        if (artifact.role != ArtifactRole::FinalObject) {
            continue;
        }
        if (artifact.disposition == ArtifactDisposition::RemovedBeforeCommit) {
            continue;
        }
        MCObjectHandle handle = sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str()));
        if (!handle) {
            throw std::runtime_error(
                "Vectorworks final object disappeared before undo registration: " + artifact.uuid);
        }
        if (sdk_.AddAfterSwapObject(handle)) {
            artifact.undoRegistration = UndoRegistration::Explicit;
            continue;
        }
        if (!AllowsSdkManagedRegistration(artifact.family)) {
            throw std::runtime_error(
                "Vectorworks rejected final-object undo registration for a family without "
                "live-proven SDK-managed ownership: " + artifact.uuid);
        }
        artifact.undoRegistration = UndoRegistration::SdkManaged;
    }
}

void NativeTransaction::RegisterExternalAfterStates() {
    for (ExternalMutation& mutation : externalMutations_) {
        if (mutation.deleted || !mutation.afterRequired) {
            continue;
        }
        MCObjectHandle handle = sdk_.GetObjectByUuid(TXString(mutation.uuid.c_str()));
        if (!handle || sdk_.GetObjectTypeN(handle) != mutation.expectedNodeType) {
            throw std::runtime_error(
                "external Vectorworks object disappeared or changed type before commit: " +
                mutation.uuid);
        }
        if (!sdk_.AddAfterSwapObject(handle)) {
            throw std::runtime_error(
                "Vectorworks rejected external-object after-state undo registration: " +
                mutation.uuid);
        }
        mutation.afterRegistered = true;
    }
}

RollbackReceipt NativeTransaction::RollbackImpl() noexcept {
    RollbackReceipt receipt;
    if (state_ != TransactionState::Active) {
        return receipt;
    }

    // The SDK undo event is authoritative. Never delete a final compound
    // object before UndoAndRemove, because AddAfter(false) may still have
    // partially registered SDK-managed ownership.
    receipt.undoAndRemoveAttempted = true;
    try {
        sdk_.UndoAndRemove();
    } catch (...) {
        try {
            receipt.cleanupFailures.push_back("<UndoAndRemove threw>");
        } catch (...) {
        }
    }

    for (const Artifact& artifact : artifacts_) {
        MCObjectHandle survivor = nullptr;
        try {
            survivor = sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str()));
        } catch (...) {
            try {
                receipt.cleanupFailures.push_back(artifact.uuid + " (resolve after undo threw)");
            } catch (...) {
            }
            continue;
        }
        if (!survivor) {
            continue;
        }
        try {
            receipt.survivorsAfterUndo.push_back(artifact.uuid);
        } catch (...) {
        }
        try {
            sdk_.DeleteObject(survivor, false);
            if (sdk_.GetObjectByUuid(TXString(artifact.uuid.c_str())) != nullptr) {
                receipt.cleanupFailures.push_back(artifact.uuid);
            }
        } catch (...) {
            try {
                receipt.cleanupFailures.push_back(artifact.uuid + " (exact cleanup threw)");
            } catch (...) {
            }
        }
    }
    state_ = TransactionState::RolledBack;
    return receipt;
}

std::string NativeTransaction::DescribeException(std::exception_ptr failure) {
    if (!failure) {
        return "unknown failure";
    }
    try {
        std::rethrow_exception(failure);
    } catch (const std::exception& error) {
        return error.what();
    } catch (...) {
        return "non-standard exception";
    }
}

}  // namespace VectorworksMCP::Transactions
