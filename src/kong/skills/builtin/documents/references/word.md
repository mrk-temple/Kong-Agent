# DOCX workflow
Open existing files with Document(path); create new documents with Document(). Add semantic headings with add_heading, paragraphs with add_paragraph, and tables with add_table.
Avoid replacing paragraph.text when preserving mixed formatting, hyperlinks or fields matters: it discards run-level structure.
Changes to complex templates, tracked revisions and floating drawings need format-aware handling beyond basic python-docx.
Keep source documents unchanged by default. Save to a separate named output, reopen, then verify expected paragraphs and tables.
For text extraction, inspect tables as well as paragraphs. The bundled inspector does not include every header, footer, textbox or revision.
If LibreOffice is available, a separate output directory can be used for PDF conversion. Rendering availability is optional and is not automatically invoked.
Official reference: https://python-docx.readthedocs.io/en/latest/user/quickstart.html
