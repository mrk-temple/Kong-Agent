---
name: code-validation
description: Candidate procedure for a small Python repair with preserved user edits and independent behavior checks.
metadata:
  version: "0.5.0-candidate.1"
  lifecycle: candidate
  kong:
    tools: [read_file, search_files, patch_file, process_exec]
    files: [references/checks.md]
---
Read the caller's current requirements and allowed files. Read the existing source and checks before changing them. Keep declared user edits and protected inputs intact. Identify the smallest change that covers all specified behavior, including empty inputs, signs, units and rounding boundaries.

Use references/checks.md to select independent checks. Run only commands authorized by the caller. After a changed input or restored process, inspect the current files and unresolved effects before proceeding. Never replay an unknown write.

Check the final source version with the declared command and fresh imports when requested. Re-read final artifacts after the last write or resource closure, then cite actual action IDs. A stage handoff is an ask under the caller's stage contract; it is not final completion. This procedure provides guidance and grants no permission, approval or completion authority.
