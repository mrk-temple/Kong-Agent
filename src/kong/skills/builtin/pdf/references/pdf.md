# PDF handling
PdfReader(path) exposes pages; page.extract_text() may return empty or unusual reading order.
Use PdfWriter.append for combining documents or add_page for selected pages; write to a distinct output.
Validate indices and requested page order before writing, then reopen and check the expected page count.
Encrypted PDFs need authorized credentials. Do not try to remove protection without a user request and legitimate access.
Scanned pages may have no text layer; report OCR as a missing capability rather than concluding the document has no content.
Tables are often positioned glyphs, not semantic cells. Verify extracted numbers against source pages where possible.
The inspector bounds pages and text returned, but PDF decompression can still consume memory; process_exec supplies a time limit, not a memory sandbox.
Official reference: https://pypdf.readthedocs.io/en/stable/user/extract-text.html
