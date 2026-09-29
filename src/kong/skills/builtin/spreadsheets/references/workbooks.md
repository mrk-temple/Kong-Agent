# Workbook rules
Read editable workbooks with load_workbook(path, data_only=False). A data_only=True read gives cached results, which may be absent or stale.
Do not save a data_only=True workbook over the original if formulas must survive.
For macro workbooks preservation may require keep_vba=True and the correct extension; advanced Excel features may not survive a generic round trip.
For CSV, explicitly choose delimiter and encoding (UTF-8-SIG handles BOM). Preserve quoted fields using csv, not string splitting.
Treat identifiers as text when leading zeroes matter. Report parsing failures instead of silently coercing them to zero.
When importing untrusted text into a spreadsheet, ensure formula-like strings are intentional before allowing them to become formulas.
For totals, cross-check against original numeric records; distinguish a formula string from its calculated value.
The bundled inspector samples bounded rows/columns and reports truncation. It is not a full workbook verification.
Official reference: https://openpyxl.readthedocs.io/en/stable/tutorial.html
