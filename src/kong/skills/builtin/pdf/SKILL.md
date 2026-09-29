---
name: pdf
description: 提取 PDF 文字、检查页数、拆分或合并已有 PDF；识别扫描件限制，不把无文字误报为空白文档。
metadata:
  kong: {"tools":["read_file","write_file","process_exec"],"files":["references/pdf.md","scripts/inspect_file.py"],"python":["pypdf"],"optional_binaries":["pdftoppm","tesseract"]}
---

# PDF inspection and transformations
Read references/pdf.md for extraction limits and preservation rules.
Use scripts/inspect_file.py through process_exec for a bounded page/text inventory:
argv = ["python", "<root>/scripts/inspect_file.py", "<workspace-relative-file.pdf>"].
The loaded root identifies the real installed script. Missing references or dependencies must be resolved before depending on them.
Inspect page count and whether meaningful text can be extracted. A scanned image needs OCR; pypdf does not perform OCR.
For merge/split operations use pypdf and save to a new file. Reopen the result, check page order/count, and inspect relevant text.
Text extraction alone does not verify tables, figures or visual layout. Do not fabricate unreadable content or silently bypass encryption.
