---
name: slides
description: 创建或修改 PowerPoint .pptx，保留模板并检查幻灯片文字与对象；未渲染时明确说明版式未验证。
metadata:
  kong: {"tools":["read_file","write_file","process_exec"],"files":["references/presentation.md","scripts/inspect_file.py"],"python":["python-pptx"],"optional_binaries":["soffice"]}
---

# PowerPoint work
Read references/presentation.md before changing a supplied deck or constructing a new one.
Use python-pptx through process_exec. Inspect the source deck with scripts/inspect_file.py:
argv = ["python", "<root>/scripts/inspect_file.py", "<workspace-relative-file.pptx>"].
Use the loaded root for the real script path. Preserve supplied slide order, theme and required content unless instructed otherwise.
For new decks choose an audience-appropriate outline and concise slide-level messages. Do not invent source data.
After saving, reopen to check slide count and expected text. Structural checks cannot reveal overlap, clipping or font substitution.
Render and inspect externally if available; otherwise deliver with an explicit visual-verification limitation.
