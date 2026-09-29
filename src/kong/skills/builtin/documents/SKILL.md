---
name: documents
description: 读取、创建或修改 Word .docx 文档，检查段落与表格结构，保留现有样式；不承诺未渲染的版式质量。
metadata:
  kong: {"tools":["read_file","write_file","process_exec"],"files":["references/word.md","scripts/inspect_file.py"],"python":["python-docx"],"optional_binaries":["soffice"]}
---

# Word documents
Read references/word.md before editing an existing DOCX or creating a structured report.
Use python-docx through process_exec. The helper scripts/inspect_file.py produces a bounded structural/text inventory:
argv = ["python", "<root>/scripts/inspect_file.py", "<workspace-relative-file.docx>"].
Use the absolute root returned by skill_load; do not guess where installed resources live.
Preserve supplied templates, headings, tables and user content. Write a new output file unless overwriting is requested.
After saving, reopen the document and check required text/table contents. Extracted text is structural evidence only.
If a renderer is unavailable, say that pagination and visual layout remain unverified. Never claim visual inspection from XML checks.
