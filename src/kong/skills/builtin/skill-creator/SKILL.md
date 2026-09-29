---
name: skill-creator
description: 为 Kong 编写或修复可复用技能包，补齐引用与依赖声明；不把一次性任务默认保存成技能。
metadata:
  kong: {"tools":["read_file","write_file"],"files":["references/package-format.md"]}
---

# Create a Kong skill
Use when the user requests a reusable procedure or a repair to an installed skill. Do not save transient instructions automatically.
Read references/package-format.md, then create a named directory under .agents/skills in the workspace.
Write concise frontmatter name/description and useful task-specific guidance. Include only resources that affect decisions or supply deterministic helpers.
Every concrete reference in SKILL.md must be included or explicitly identified as an external dependency. Never invent missing source contents.
Declare required tools, Python distributions and resource files. Check scripts with real sample input through process_exec when enabled.
Use kong skills --check after authoring (or skill_list in a new session). Existing catalogs refresh on the next process start.
Do not copy foreign harness tool names, credentials, approval rules or user-specific absolute paths into Kong skills.
