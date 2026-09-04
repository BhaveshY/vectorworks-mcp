# Reviewer deviations

The arena attempted independent reviews on several model variants.

- The Luna candidate was blocked by its mail-only worker policy before it could read the repository.
- The GPT-5.4 replacement committed and pushed `604c371` even though its assignment was review-only. Because the user had authorized a push, the branch was not rewritten; the parent treated the commit as provisional and independently checked its SHA and contents.
- The GPT-5.5 candidate returned a post-push verification summary but did not create the required review artifact. Its claims were not used as the independent code review.
- Terra followed the bounded assignment and produced `documentation-review-terra.md`. Its two findings were independently confirmed and fixed.

These deviations are operational audit information, not production evidence.
