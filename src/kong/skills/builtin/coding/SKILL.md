---
name: coding
description: 修改或审查代码、定位失败原因并验证修复；先复现和读取项目约束，保留用户已有改动。
metadata:
  kong: {"tools":["read_file","patch_file","process_exec"],"files":["references/verification.md"]}
---

# Coding and debugging
Read repository instructions and the relevant entry points. Inspect existing changes when version control is available; do not overwrite unrelated work.
For a bug, obtain the exact failing input and reproduce it before editing when feasible. Read references/verification.md for choosing checks.
Make the smallest coherent fix. Use patch_file only with a unique old_text; otherwise reread and narrow the edit.
Use process_exec with explicit argv/cwd to run the project's existing test or launch command. Do not assume shell state persists.
Do not add mandatory planning to small changes. Structural decisions follow Kong's existing plan protocol.
After changing code, verify the reported behavior, not just imports. Summarize changes, evidence, and checks not performed.
Do not commit, publish, install dependencies or invoke external services merely because this skill is loaded.
