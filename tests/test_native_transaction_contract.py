from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "native_bridge" / "src" / "NativeTransaction.hpp"
SOURCE = ROOT / "native_bridge" / "src" / "NativeTransaction.cpp"


class NativeTransactionContractTests(unittest.TestCase):
    def test_transaction_owns_event_boundaries_and_checks_commit(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("sdk_.SupportUndoAndRemove()", source)
        self.assertIn("sdk_.SetUndoMethod(kUndoSwapObjects)", source)
        self.assertIn("sdk_.NameUndoEvent(undoName)", source)
        self.assertIn("if (!sdk_.EndUndoEvent())", source)
        self.assertLess(source.index("RemoveTemporaryInputs();"), source.index("sdk_.EndUndoEvent()"))
        self.assertLess(source.index("VerifyFinalObjects();"), source.index("sdk_.EndUndoEvent()"))

    def test_rollback_undoes_before_exact_uuid_cleanup(self):
        source = SOURCE.read_text(encoding="utf-8")
        rollback = source[source.index("RollbackReceipt NativeTransaction::RollbackImpl") :]

        self.assertLess(rollback.index("sdk_.UndoAndRemove()"), rollback.index("for (const Artifact& artifact"))
        self.assertIn("sdk_.GetObjectByUuid", rollback)
        self.assertIn("sdk_.DeleteObject(survivor, false)", rollback)
        self.assertIn("std::throw_with_nested", source)

    def test_sdk_managed_registration_is_explicitly_family_scoped(self):
        header = HEADER.read_text(encoding="utf-8")
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("sdkManagedRegistrationFamilies", header)
        self.assertIn("Parametric", header)
        self.assertIn("Symbol", header)
        self.assertIn("AllowsSdkManagedRegistration(artifact.family)", source)
        self.assertIn("UndoRegistration::SdkManaged", source)
        self.assertIn("live-proven SDK-managed ownership", source)

    def test_local_create_delete_is_net_zero_at_commit(self):
        header = HEADER.read_text(encoding="utf-8")
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("void DisposeFinal(ArtifactId id)", header)
        self.assertIn("ArtifactDisposition::RemovedBeforeCommit", source)
        self.assertIn("Vectorworks did not dispose the exact final-object UUID", source)

    def test_external_mutations_have_checked_before_and_after_states(self):
        header = HEADER.read_text(encoding="utf-8")
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("TrackExternalBefore", header)
        self.assertIn("TrackExternalAfter", header)
        self.assertIn("TrackExternalDeleted", header)
        self.assertIn("if (!sdk_.AddBeforeSwapObject(handle))", source)
        self.assertIn("if (sdk_.AddAfterSwapObject(handle))", source)
        self.assertIn("AllowsSdkManagedRegistration(mutation.family)", source)
        self.assertIn("mutation.afterSdkManaged = true", source)
        self.assertIn("Unknown\n        // and simple objects remain fail-closed", source)
        self.assertNotIn("externalMutations_", source[source.index("for (const Artifact& artifact : artifacts_)", source.index("RollbackReceipt NativeTransaction::RollbackImpl")) :])

    def test_handlers_do_not_own_undo_event_boundaries(self):
        for name in ("SpaceObjectHandlers.cpp", "BimObjectHandlers.cpp"):
            source = (ROOT / "native_bridge" / "src" / name).read_text(encoding="utf-8")
            self.assertNotIn("SupportUndoAndRemove", source)
            self.assertNotIn("EndUndoEvent", source)
            self.assertNotIn("UndoAndRemove", source)

    def test_all_scaffold_source_lists_include_transaction_module(self):
        for name in (
            "build-native-bridge.ps1",
            "copy-native-bridge-scaffold.ps1",
            "wire-native-bridge-project.ps1",
            "test-native-bridge-scaffold.ps1",
        ):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("NativeTransaction.hpp", source)
            self.assertIn("NativeTransaction.cpp", source)


if __name__ == "__main__":
    unittest.main()
