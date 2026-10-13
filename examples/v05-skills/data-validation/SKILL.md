---
name: data-validation
description: Candidate procedure for repeatable CSV accounting and a report aligned with exact result rows.
metadata:
  version: "0.5.0-candidate.1"
  lifecycle: candidate
  kong:
    tools: [read_file, write_file, patch_file, process_exec]
    files: [references/checks.md]
---
Read the current input schema and the caller's latest rules. Distinguish cents from dollars, signed values from status, physical-row deduplication from aggregation, and empty groups from zero amounts. Keep source inputs intact. Preserve integer/Decimal precision through intermediate calculations.

Use references/checks.md for full-row and report checks. Produce a re-runnable script, ordered output and a report with the same totals. After appended input or a rule correction, recompute from the current input; do not reuse an old summary as current evidence.

After the final write, run the authorized check and fully read back final artifacts. Respect stage handoffs and the caller's final contract. Never invent validation receipts or replay unknown effects. This procedure provides guidance only and grants no new permissions.
