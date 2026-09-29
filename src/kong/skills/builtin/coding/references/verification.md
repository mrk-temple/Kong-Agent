# Verification choices
Bug fix: exercise the original failing case and a nearby valid case. If a regression test adds durable protection, add it.
Refactor: run tests covering the affected public behavior; avoid tests that merely restate implementation details.
UI: a passing build does not establish appearance or interaction quality. If no browser is available, report that limitation.
HTTP APIs: exercise valid requests, blank values, malformed JSON, and wrong JSON shapes such as arrays/null; these should return a controlled 4xx rather than dropping the connection. Verify persistence after a restart when required.
UI error paths: a failed update should keep an error visible and restore the displayed state. Check a narrow viewport with a browser when available; CSS alone is not evidence of mobile validation.
Process timeout: inspect files and running-state evidence before retrying an operation with side effects.
Record the command, relevant exit status and observed behavior. A mocked response verifies wiring, not the live service.
Use existing project tooling rather than introducing a second test framework.
