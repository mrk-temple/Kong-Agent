# Kong package format
Place each bundle at .agents/skills/<name>/SKILL.md. Names use lowercase letters, digits and hyphens.
SKILL.md begins with YAML frontmatter containing name and description. The body follows the closing --- line.
Optional metadata.kong keys: tools (tool names), python (distribution names), binaries, optional_binaries, files (relative resources).
Example dependency declaration: metadata: {kong: {tools: [read_file, process_exec], python: [openpyxl]}}.
Use references/ for conditional detail, scripts/ for executable helpers, assets/ for actual output resources.
All resource paths resolve inside that skill directory. Parent traversal and symlink escapes are rejected.
Skill loading never executes scripts, evaluates shell snippets, installs dependencies, grants permissions or spawns agents.
Other harness fields such as allowed-tools/context/hooks are not executed; diagnostics identify unsupported frontmatter fields.
Copy the whole bundle when distributing it. Restart Kong after editing so its immutable catalog reflects the new version.
Qualified IDs builtin:name and project:name keep collisions explicit. Bare names work only when unambiguous.
