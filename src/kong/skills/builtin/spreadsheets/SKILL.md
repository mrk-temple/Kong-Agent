---
name: spreadsheets
description: 处理 CSV/TSV 或 Excel .xlsx 数据、公式与表格，验证行数和汇总结果；保留原公式并说明未重算限制。
metadata:
  kong: {"tools":["read_file","write_file","process_exec"],"files":["references/workbooks.md","scripts/inspect_file.py"],"python":["openpyxl"],"optional_binaries":["soffice"]}
---

# Spreadsheet work
Read references/workbooks.md before changing an existing workbook or writing formulas.
Use csv for text tables and openpyxl for XLSX. Inspect sheets, occupied dimensions, formulas and representative rows first.
Run scripts/inspect_file.py through process_exec:
argv = ["python", "<root>/scripts/inspect_file.py", "<workspace-relative-file.xlsx>"].
The helper also accepts CSV/TSV. Use the actual root from skill_load.
Do not replace formulas with cached values. Preserve leading-zero identifiers and distinguish blanks, zeroes and missing data.
Write a new output by default. Reopen it and verify requested cells, dimensions and totals against source values.
openpyxl does not calculate formulas. Without Excel/LibreOffice recalculation, report formula text checks separately from evaluated results.
