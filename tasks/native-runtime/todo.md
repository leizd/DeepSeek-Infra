<!-- BEGIN CURRENT_NATIVE_CLOUD_CHECKPOINT -->
## Current checkpoint — 2026-10-16

Branch `codex/native-runtime-pr/20261014`, HEAD `b0316d09811d66ae3e47fbc4bbea3b4ddc43d4ca`.
That commit is docs-only. `372c30451dfa7137ca81acd326a309285226dcaa` is its parent.
Uncommitted: credential transport repair and runtime admission. See
`tasks/native-runtime/continuation.md`. Status stays **NOT_READY / 未完成**.

- [x] Read the failed exact runs `38501431635` and `38501553143`. Rust public
  driver aborted on a bare listen address. Python 3.10/3.11/3.12 failed the
  90-minute race budget check, already adjusted on this HEAD by `2c84f61c`.
- [x] Map the worker listen address to the gateway's private HTTPS origin in
  both process probes, and require the request credential on every controlled
  provider call. Offline: 107 passed in `tests/test_native_agent_public_driver.py`.
- [x] Admit retained runtime payload updates, resume/rerun, and same-effect
  tool continuation. Offline: 73 passed in
  `tests/test_native_agent_runtime_admission.py`. This is not a provider
  re-run and not 完成验收.
- [x] Mint a successor operation id for an admitted `agent_reset` only when
  the prior effect is settled and the reset shares the fence. Keep the old
  id. `EFFECT_UNKNOWN` does not mint or execute. The driver holds the id and
  does not call the worker with it.
- [x] Persist that id beside the original intent. Execute it only after
  `SuccessorExecution` admits a newer epoch from claim or takeover. Offline
  oracle and source wiring: 79 passed in
  `tests/test_native_agent_runtime_admission.py`. Rust and Go were not
  compiled, and no provider call was observed. This is not 完成验收.
- [x] Public resume, rerun, and payload update classify the retained event
  stream and pass it into `admit`. A settled effect still completes. An
  unknown, unconfirmed, missing, timed-out, or absent record refuses with
  `EFFECT_UNKNOWN` and appends nothing. Offline: 93 passed in
  `tests/test_native_agent_runtime_admission.py`. Rust was not compiled.
  The worker SQL journal is not queried. This is not 完成验收.
- [x] Project a queried journal row into that public stream. Unknown,
  dispatching, and empty states append `node_effect`. A missing row, timeout,
  or absent log is an observation, not state `NOT_APPLIED`. A confirmed read
  is not appended and resume still completes. Offline: 96 passed. The HTTP
  route still does not query SQLite. A resume before the driver has observed
  the row remains invisible. Rust was not compiled. This is not 完成验收.
- [x] Return the dispatcher envelope from a legal same-effect tool
  continuation. `generate_chart` matches `execute_tool_call`. A missing row,
  timeout, or absent log refuses and is not state `NOT_APPLIED`. A later
  `agent_output` still completes resume. At that step the worker stored
  `toolResults` before confirm. Publish keeps `AWAITING_TOOLS`. Offline: 99
  collected, run exited 0. Rust was not compiled. This is not 完成验收.
- [x] Pass the chat workspace into that continuation and confirm the tool
  request before any tool store write. A retained `list_reminders` call reads
  the supplied root and leaves the file unchanged. A dead lease and a missing
  row return no result. Publish reads the stored result and keeps
  `AWAITING_TOOLS`. A confirmed replay does not dispatch. Offline: 100
  collected, run exited 0. Rust was not compiled. This is not 完成验收.
- [x] Resume and rerun copy `toolResults` already on the public event and do
  not dispatch the tool or append `CONFIRMED`. A missing result stays
  `awaiting_tools` and is not state `NOT_APPLIED`. A second resume does not
  append the same result again. Offline: 103 collected, run exited 0. Rust
  was not compiled. This is not 完成验收.
- [x] The Go driver, on `AWAITING_TOOLS`, returns scheduling copies of stored
  tool results and does not re-execute the tool or append `CONFIRMED`. A
  missing result stays `errAgentDriverTools` and is not state `NOT_APPLIED`.
  Offline: 106 collected, run exited 0. `gofmt` formatted the two Go files.
  Rust was not compiled. Go tests were not run. A missing control client
  writes no marker. At that checkpoint the copies were not consumed by a
  model round. This is not 完成验收.
- [x] The worker's next model turn reads already-saved `toolResults` and does
  not call the tool. A missing result file does not start that turn, is not
  state `NOT_APPLIED`, and does not append `CONFIRMED`. Offline: 109
  collected, run exited 0. Rust was not compiled. Go was not edited. The
  follow-up answer is a sidecar under `retained-model-turns` and does not
  settle the Go task. This is not 完成验收.
- [x] Publish attaches a saved follow-up with no further tool calls as
  `publishedAnswer` and sets metadata `ANSWER_PUBLISHED`. Resume of that
  public event leaves `awaiting_tools`. A missing answer file stays
  `AWAITING_TOOLS` and is not state `NOT_APPLIED` or `CONFIRMED`. The driver
  returns the effect and does not `SettleTask`, so the run stays running.
  Offline: 112 collected, run exited 0. `gofmt` formatted the driver. Rust
  was not compiled. Go tests were not run. This is not 完成验收.
- [x] Settlement admits a published answer only on the installed epoch.
  A larger epoch is `FENCE_MISMATCH` and is not installed. A missing answer
  stays unsettled and is not state `NOT_APPLIED`. The worker signs the
  existing custody receipt over the unchanged tool-request row and does not
  install an epoch. `tasks` calls `RecordOutcome` then `SettleTask` only when
  the receipt and `TaskOutput` are present. Offline: 114 collected, run
  exited 0. `gofmt` formatted the driver and the worker client. Rust was not
  compiled. Go tests were not run. The Go transaction was not executed. This
  is not 完成验收.
- [x] A published follow-up can supply a dependent task output.
  `published_dependency_output` copies a direct answer and, for a tool
  request, returns the saved follow-up only when it publishes. A missing
  file stays unresolved and is not state `NOT_APPLIED`. The executor reads
  that file before `exchange_fenced_turn` and does not mark the node unknown
  for `AGENT_DEPENDENCY_UNPUBLISHED`. Offline: 116 collected, run exited 0.
  Rust was not compiled. Go was not edited. The payload builders were not
  executed. This is not 完成验收.
- [x] A published dependency output is placed in the dependent provider body.
  The Python oracle calls `build_prior_context`, `agent_messages`, and
  `synthesis_messages`, and the saved answer text is in both. A missing file
  returns `AGENT_DEPENDENCY_UNPUBLISHED` before those builders and does not
  mark the node unknown. `provider_body_from_outputs` calls `worker_payload`
  and `synthesis_payload` when outputs are present. Offline: 117 collected,
  run exited 0. Rust was not compiled. Go was not edited. The provider
  exchange was not executed. This is not 完成验收.
- [x] A dead execution lease returns before the provider send.
  `execution_send_lease` denies a deadline that has arrived with
  `AGENT_EXECUTION_LEASE_DENIED`, marks the admitted node unknown, and does
  not store a provider effect or state `NOT_APPLIED`. A later deadline is
  allowed. Offline: 118 collected, run exited 0. `rustfmt --edition 2024`
  `--check` passed on the policy admission file and the task executor. Rust
  was not compiled. Go was not edited. The provider exchange was not
  executed. This is not 完成验收.
- [x] A live lease is the only provider send, and a lost receipt stays unknown.
  `admitted_provider_send` forwards only a clean live lease.
  `provider_exchange_unknown` returns `AGENT_PROVIDER_RESULT_UNKNOWN` for a
  missing receipt, a timeout, a missing object, and an absent log. That
  refusal stores no effect and is not state `NOT_APPLIED`. Offline: 119
  collected, run exited 0. `rustfmt --edition 2024 --check` passed on the
  policy admission file and the task executor. Rust was not compiled. Go was
  not edited. The provider exchange was not executed. The second follow-up
  exchange still ignores a provider error. This is not 完成验收.
- [x] The follow-up send uses the live lease and does not store a lost receipt.
  `follow_up_exchange` stores only when the lease is allowed and a receipt
  is present. A dead clock and a lost receipt do not store and do not mark
  the confirmed row unknown. Offline: 120 collected, run exited 0.
  `rustfmt --edition 2024 --check` passed on the policy admission file and
  the task executor. Rust was not compiled. Go was not edited. The provider
  exchange was not executed. This is not 完成验收.
- [x] The workspace tool flag comes only from the live execution lease.
  `workspace_tool_lease` passes `live` only for a later deadline.
  `continue_retained_tools` then returns the chart result. A dead or
  non-positive clock passes `expired`, returns `LEASE_NOT_HELD`, and does
  not call the tool. Offline: 121 collected, run exited 0.
  `rustfmt --edition 2024 --check` passed on the policy admission file and
  the task executor. Rust was not compiled. Go was not edited. The provider
  exchange was not executed. This is not 完成验收.
- [x] The send deadline is the minimum of the grant expiry, the claim lease,
  and the writer lease. An elapsed or non-positive minimum is refused by
  `execution_send_lease` before the provider send and before the live
  workspace flag. A later minimum still lets `continue_retained_tools` return
  the chart result. Offline: 122 collected, run exited 0.
  `rustfmt --edition 2024 --check` passed on the policy admission file and
  the task executor. Rust was not compiled. Go was not edited. The provider
  exchange was not executed. This is not 完成验收.
- [x] The provider timeout is the remaining whole seconds of that live minimum.
  `execution_send_budget` arms only after `execution_send_lease` allows it.
  A smaller cap wins. An elapsed or non-positive remainder does not arm the
  timeout, and the expired workspace flag does not call the tool. Offline:
  123 collected, run exited 0. `rustfmt --edition 2024 --check` passed on
  the policy admission file and the task executor. Rust was not compiled.
  Go was not edited. The provider exchange was not executed. This is not
  完成验收.
- [x] `POST /api/file-text` returns decoded text for a legal text upload.
  `admit_upload_body` refuses an empty body before `parse_multipart`.
  `admit_file_text_part` refuses an empty part and whitespace-only text
  before `write_cache`. Corrupt image bytes with OCR on refuse before
  `ocr_image`. A recognized image header continues and does not invent OCR
  text. PDF bytes stay on `extract_pdf_document`. Offline: 8 collected in
  `tests/test_native_file_text_admission.py`, run exited 0. The 123-test
  admission file was not re-run. `rustfmt --edition 2024 --check` passed on
  `file_upload.rs` and `file_text_route.rs`. Rust was not compiled. Go was
  not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches decoded text only when the native
  writer owns `project_metadata_store`. An empty body, an empty part,
  whitespace-only text, and a corrupt image refuse before metadata or cache
  writes. A Python-owned store, including `go_authoritative` and `shadow`,
  returns 409 and writes nothing. PDF and a recognized image header stay
  on the existing extractor. Offline: 5 collected in
  `tests/test_native_project_file_admission.py`, run exited 0. The 8-test
  file-text file and the 123-test admission file were not re-run.
  `rustfmt --edition 2024 --check` passed on `projects.rs` and
  `project_files_route.rs` before the Python-only wire-code fix. Rust was
  not compiled. Go was not edited. No epoch was installed. This is not
  完成验收.
- [x] A legal selectable PDF returns the shipped `extract_pdf_text` result.
  `admit_selectable_pdf` disables OCR. Corrupt bytes and a truncated `%PDF`
  header refuse with `Could not extract text from this PDF` before a cache
  write. `extract_uploaded_file` still calls `extract_pdf_document` before
  `write_cache`. `pdftoppm` is not on PATH, so page images were not proven.
  Offline: 3 collected in `tests/test_native_pdf_text_admission.py`, run
  exited 0. The 5-test, 8-test, and 123-test files were not re-run. Rust
  was not compiled. Go was not edited. No epoch was installed. This is not
  完成验收.
- [x] `POST /api/file-text` returns shipped HTML text for a readable page.
  `admit_file_text_part` calls `extract_html_text` before
  `normalize_extracted_text`. `cacheWrite` is true only for that text, and
  the test writes the cache file only then. An empty document, a whitespace
  paragraph, and a script-only page refuse before `write_cache`. Offline:
  10 collected in `tests/test_native_file_text_admission.py`, run exited 0.
  The project-files file was re-run: 5 collected, run exited 0. The previous
  8-test run was not this run. `rustfmt --edition 2024 --check` passed on
  `file_upload.rs`. Rust was not compiled. Go was not edited. No epoch was
  installed. This is not 完成验收.
- [x] `POST /api/file-text` returns shipped DOCX text for a legal package.
  `admit_file_text_part` calls `extract_docx_text` before
  `normalize_extracted_text`. `cacheWrite` is true only for that text, and
  the test writes the cache file only then. Bytes `PK` refuse with
  `Invalid docx file`. A zip whose `document.xml` is not XML refuses with
  `Invalid docx XML`. A paragraph with no readable text refuses before
  `write_cache`. Offline: 12 collected in
  `tests/test_native_file_text_admission.py`, run exited 0. The
  project-files file was re-run: 5 collected, run exited 0. The previous
  10-test HTML run was not this run. `rustfmt --edition 2024 --check` passed
  on `file_upload.rs` after the DOCX edit. Rust was not compiled. Go was not
  edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-text` returns shipped PPTX text for a legal package.
  `admit_file_text_part` calls `extract_pptx_text` before
  `normalize_extracted_text`. `cacheWrite` is true only for that text, and
  the test writes the cache file only then. Bytes `PK` refuse with
  `Invalid pptx file`. A zip whose slide XML is not XML refuses with
  `Invalid pptx XML`. A whitespace slide refuses before `write_cache`.
  Offline: 14 collected in `tests/test_native_file_text_admission.py`, run
  exited 0. The project-files file was re-run: 5 collected, run exited 0.
  The previous 12-test DOCX run was not this run. `rustfmt --edition 2024
  --check` passed on `file_upload.rs` after the PPTX edit. Rust was not
  compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-text` returns shipped XLSX text for a legal workbook.
  `admit_file_text_part` calls `extract_xlsx_text` before
  `normalize_extracted_text`. `cacheWrite` is true only for that text, and
  the test writes the cache file only then. Bytes `PK` and a zip that is not
  a workbook refuse with `Invalid xlsx file`. An empty workbook and a
  whitespace cell refuse before `write_cache`. Offline: 16 collected in
  `tests/test_native_file_text_admission.py`, run exited 0. The
  project-files file was re-run: 5 collected, run exited 0. The previous
  14-test PPTX run was not this run. `rustfmt --edition 2024 --check` passed
  on `file_upload.rs` after the XLSX edit. Rust was not compiled. Go was not
  edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-text` returns shipped EPUB text for a legal package.
  `admit_file_text_part` calls `extract_epub_text` before
  `normalize_extracted_text`. `cacheWrite` is true only for that text, and
  the test writes the cache file only then. `nav.xhtml` and a script secret
  are absent. Bytes `PK` refuse with `Invalid epub file`. A nav-only package,
  a whitespace chapter, and a script-only chapter refuse before
  `write_cache`. Offline: 18 collected in
  `tests/test_native_file_text_admission.py`, run exited 0. The
  project-files file was re-run: 5 collected, run exited 0. The previous
  16-test XLSX run was not this run. `rustfmt --edition 2024 --check` passed
  on `file_upload.rs` after the EPUB edit. Rust was not compiled. Go was not
  edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped EPUB text only for the native
  project-metadata writer. `admit_project_file_part` calls
  `admit_file_text_part`, which calls `extract_epub_text`. Both write flags
  are true only for that text, and the test writes `project.json` only then.
  `nav.xhtml` and a script secret are absent. Bytes `PK` refuse with
  `Invalid epub file` before metadata or cache writes. A nav-only package, a
  whitespace chapter, and a script-only chapter refuse before those writes.
  Non-owner modes, including `go_authoritative` and `shadow`, return 409 and
  do not enter the patched writers. PDF still continues. Offline: 7 collected
  in `tests/test_native_project_file_admission.py`, run exited 0. The previous
  5-test project-files run was not this run. The 18-test file-text file was
  not re-run. `rustfmt --edition 2024 --check` passed on `projects.rs` after
  the comment edit. Rust was not compiled. Go was not edited. No epoch was
  installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped DOCX text only for the native
  project-metadata writer. `admit_project_file_part` calls
  `admit_file_text_part`, which calls `extract_docx_text`. Both write flags
  are true only for that text, and the test writes `project.json` only then.
  Bytes `PK` refuse with `Invalid docx file`. A zip whose `document.xml` is
  not XML refuses with `Invalid docx XML`. A whitespace paragraph refuses
  before metadata or cache writes. Non-owner modes, including
  `go_authoritative` and `shadow`, return 409 and do not enter the patched
  writers. PDF still continues. Offline: 9 collected in
  `tests/test_native_project_file_admission.py`, run exited 0. The previous
  7-test project-files run was not this run. The 18-test file-text file was
  not re-run. Rust files were not edited, so rustfmt was not run. Rust was
  not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped PPTX text only for the native
  project-metadata writer. `admit_project_file_part` calls
  `admit_file_text_part`, which calls `extract_pptx_text`. Both write flags
  are true only for that text, and the test writes `project.json` only then.
  Bytes `PK` refuse with `Invalid pptx file`. A zip whose slide XML is not
  XML refuses with `Invalid pptx XML`. A namespace-valid whitespace slide
  refuses before metadata or cache writes. Non-owner modes, including
  `go_authoritative` and `shadow`, return 409 and do not enter the patched
  writers. PDF still continues. Offline: 11 collected in
  `tests/test_native_project_file_admission.py`, run exited 0. The previous
  9-test project-files run was not this run. The 18-test file-text file was
  not re-run. Rust files were not edited, so rustfmt was not run. Rust was
  not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped XLSX text only for the native
  project-metadata writer. `admit_project_file_part` calls
  `admit_file_text_part`, which calls `extract_xlsx_text`. Both write flags
  are true only for that text, and the test writes `project.json` only then.
  Bytes `PK` and a zip that is not a workbook refuse with `Invalid xlsx file`.
  An empty workbook and a whitespace cell refuse before metadata or cache
  writes. Non-owner modes, including `go_authoritative` and `shadow`, return
  409 and do not enter the patched writers. PDF still continues. Offline: 13
  collected in `tests/test_native_project_file_admission.py`, run exited 0.
  The previous 11-test project-files run was not this run. The 18-test
  file-text file was not re-run. Rust files were not edited, so rustfmt was
  not run. Rust was not compiled. Go was not edited. No epoch was installed.
  This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped HTML for a `.htm` part with kind `htm` only for the native `project_metadata_store` writer. A script-only page and a whitespace paragraph refuse before metadata or cache writes. PDF still continues. The first run collected 15 tests and exited 0. The later 19-test `-rA` run passed the two HTM tests again. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped Markdown for `Notes.MD` with kind `md` only for that native writer. Whitespace-only markdown refuses before metadata or cache writes. PDF still continues. The 19-test `-rA` run passed these two tests. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped CSV for `sheet.CSV` with kind `csv` only for that native writer. Whitespace-only CSV refuses before metadata or cache writes. PDF still continues. Offline: 19 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed. Rust was not compiled. Cargo, `go test`, and Docker were not started. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped JSON for `notes.JSON` with kind `json` only for the native writer. Whitespace-only JSON refuses before metadata or cache writes. PDF still continues. Offline: 21 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped YAML for `config.YAML` with kind `yaml` only for the native writer. Whitespace-only YAML refuses before metadata or cache writes. PDF still continues. Offline: 23 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `config.YML` with kind `yml` only for the native writer. Whitespace-only `.yml` refuses before metadata or cache writes. PDF still continues. Offline: 25 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `notes.MARKDOWN` with kind `markdown` only for the native writer. Whitespace-only `.markdown` refuses before metadata or cache writes. PDF still continues. Offline: 27 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `sheet.TSV` with kind `tsv` only for the native writer. Whitespace-only `.tsv` refuses before metadata or cache writes. PDF still continues. Offline: 29 passed, listed by `-rA`, and the run exited 0. The later 31-test run passed these two tests again. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `rows.JSONL` with kind `jsonl` only for the native writer. Whitespace-only `.jsonl` refuses before metadata or cache writes. PDF still continues. Offline: 31 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped XML text for `notes.XML` with kind `xml` only for the native writer. The tags stay in the text. `extract_html_text` is not entered. Whitespace-only `.xml` refuses before metadata or cache writes. PDF still continues. Offline: 33 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped CSS text for `theme.CSS` with kind `css` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.css` refuses before metadata or cache writes. PDF still continues. Offline: 35 passed, listed by `-rA`, and the run exited 0. The later 37-test and 39-test runs passed these two tests again. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped JavaScript for `app.JS` with kind `js` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.js` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".js",`. Offline: 37 passed, listed by `-rA`, and the run exited 0. The later 39-test run passed these two tests again. Ruff and three-file mypy passed. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `app.MJS` with kind `mjs` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.mjs` refuses before metadata or cache writes. PDF still continues. Offline: 39 passed, listed by `-rA`, and the run exited 0. The later 41-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `app.CJS` with kind `cjs` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.cjs` refuses before metadata or cache writes. PDF still continues. Offline: 41 passed, listed by `-rA`, and the run exited 0. The later 43-test and 45-test runs passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `app.TS` with kind `ts` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.ts` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".ts",` so `.tsx` does not satisfy it. Offline: 43 passed, listed by `-rA`, and the run exited 0. The later 45-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `view.TSX` with kind `tsx` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.tsx` refuses before metadata or cache writes. PDF still continues. Offline: 45 passed, listed by `-rA`, and the run exited 0. The later 47-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `view.JSX` with kind `jsx` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.jsx` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".jsx",`. Offline: 47 passed, listed by `-rA`, and the run exited 0. The later 49-test and 51-test runs passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `notes.PY` with kind `py` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.py` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".py",`. Offline: 49 passed, listed by `-rA`, and the run exited 0. The later 51-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `Note.JAVA` with kind `java` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.java` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".java",`. Offline: 51 passed, listed by `-rA`, and the run exited 0. The later 53-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `main.C` with kind `c` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.c` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".c",` so `.cpp`, `.cjs`, `.cs`, and `.csv` do not satisfy it. Offline: 53 passed, listed by `-rA`, and the run exited 0. The later 55-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `main.CPP` with kind `cpp` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.cpp` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".cpp",`. Offline: 55 passed, listed by `-rA`, and the run exited 0. The later 57-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.H` with kind `h` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.h` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".h",` so `.hpp` and `.html` do not satisfy it. Offline: 57 passed, listed by `-rA`, and the run exited 0. The later 59-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.HPP` with kind `hpp` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.hpp` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".hpp",`. Offline: 59 passed, listed by `-rA`, and the run exited 0. The later 61-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `Note.CS` with kind `cs` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.cs` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".cs",` so `.css` does not satisfy it. Offline: 61 passed, listed by `-rA`, and the run exited 0. The later 63-test and 65-test runs passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.GO` with kind `go` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.go` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".go",`. Offline: 63 passed, listed by `-rA`, and the run exited 0. The later 65-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.RS` with kind `rs` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.rs` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".rs",`. Offline: 65 passed, listed by `-rA`, and the run exited 0. The later 67-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.PHP` with kind `php` only for the native writer. The opening `<?php` tag stays in the text. `extract_html_text` is not entered. Whitespace-only `.php` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".php",`. Offline: 67 passed, listed by `-rA`, and the run exited 0. The later 69-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.RB` with kind `rb` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.rb` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".rb",`. Offline: 69 passed, listed by `-rA`, and the run exited 0. The later 71-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.SWIFT` with kind `swift` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.swift` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".swift",`. Offline: 71 passed, listed by `-rA`, and the run exited 0. The later 73-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.KT` with kind `kt` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.kt` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".kt",`. Offline: 73 passed, listed by `-rA`, and the run exited 0. The later 75-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `notes.SQL` with kind `sql` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.sql` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".sql",`. Offline: 75 passed, listed by `-rA`, and the run exited 0. The later 77-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `run.SH` with kind `sh` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.sh` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".sh",`. Offline: 77 passed, listed by `-rA`, and the run exited 0. The later 79-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `run.PS1` with kind `ps1` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.ps1` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".ps1",`. Offline: 79 passed, listed by `-rA`, and the run exited 0. The later 81-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `run.BAT` with kind `bat` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.bat` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".bat",`. Offline: 81 passed, listed by `-rA`, and the run exited 0. The later 83-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `app.LOG` with kind `log` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.log` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".log",`. Offline: 83 passed, listed by `-rA`, and the run exited 0. The later 85-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `app.INI` with kind `ini` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.ini` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".ini",`. Offline: 85 passed, listed by `-rA`, and the run exited 0. The later 87-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `config.TOML` with kind `toml` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.toml` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".toml",`. Offline: 87 passed, listed by `-rA`, and the run exited 0. The later 89-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `app.ENV` with kind `env` only for the native writer. `extract_html_text` is not entered. Whitespace-only `.env` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".env",`. Offline: 89 passed, listed by `-rA`, and the run exited 0. The later 91-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `note.RTF` with kind `rtf` only for the native writer. The `{\rtf1` control word stays in the text. `extract_html_text` is not entered. Whitespace-only `.rtf` refuses before metadata or cache writes. PDF still continues. The source check requires the token `".rtf",`. Offline: 91 passed, listed by `-rA`, and the run exited 0. The later 93-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.PNG` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires the token `".png",` inside `IMAGE_EXTENSIONS`. Offline: 93 passed, listed by `-rA`, and the run exited 0. The later 95-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.JPG` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires the token `".jpg",` inside `IMAGE_EXTENSIONS`. Offline: 95 passed, listed by `-rA`, and the run exited 0. The later 97-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.JPEG` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires the token `".jpeg",` inside `IMAGE_EXTENSIONS`. Offline: 97 passed, listed by `-rA`, and the run exited 0. The later 99-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.WEBP` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires the token `".webp",` inside `IMAGE_EXTENSIONS`. Offline: 99 passed, listed by `-rA`, and the run exited 0. The later 101-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.BMP` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires the token `".bmp",` inside `IMAGE_EXTENSIONS`. Offline: 101 passed, listed by `-rA`, and the run exited 0. The later 103-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.TIF` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires the token `".tif",` inside `IMAGE_EXTENSIONS`, so `.tiff` does not satisfy it. Offline: 103 passed, listed by `-rA`, and the run exited 0. The later 105-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.TIFF` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires the token `".tiff",` inside `IMAGE_EXTENSIONS`. Offline: 105 passed, listed by `-rA`, and the run exited 0. The later 107-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.GIF` `GIF89a` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires the token `".gif",` inside `IMAGE_EXTENSIONS`. Offline: 107 passed, listed by `-rA`, and the run exited 0. The later 109-test and 111-test runs passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` classifies `scan.BIN` as an image only through the normalized content type ` Image/GIF ; charset=binary`. `.bin` is not an image extension. OCR off returns `ocr_required` and status 415 before metadata or cache writes, and the image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same `GIF89a` header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check shows `is_image_file` splits on `;`, trims, lowercases, then uses `starts_with("image/")`. Offline: 109 passed, listed by `-rA`, and the run exited 0. The later 111-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses a recognized `scan.GIF` `GIF87a` header when OCR is off with `ocr_required` and status 415 before metadata or cache writes. The image-magic checker is not entered. Non-owners return 409 and do not enter `admit_file_text_part`. The same header with OCR on continues with empty text and both write flags false. OCR text is not invented. PDF still continues. The source check requires `b"GIF87a"` and `b"GIF89a"` inside `image_magic_ok`. Offline: 111 passed, listed by `-rA`, and the run exited 0. The later 113-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped decoded text for `note.BIN` with content type `text/plain; charset=utf-8` and kind `bin`, only for the native writer. `.bin` is not in `TEXT_EXTENSIONS`. The raw `text/` prefix classifies it before the NUL scan. `extract_html_text` and the image-magic checker are not entered. Whitespace-only bytes refuse before metadata or cache writes. Non-owners return 409 and do not enter `decode_text_file`. PDF still continues. Offline: 113 passed, listed by `-rA`, and the run exited 0. The later 116-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` sniffs `note.BIN` with content type `application/octet-stream`. Bytes without a NUL attach shipped text with kind `bin` only for the native writer. Whitespace without a NUL refuses before metadata or cache writes. A NUL continues with empty text and both write flags false, and `decode_text_file` is not entered. The same NUL with content type `text/plain` still attaches, and normalization strips that NUL. `extract_html_text` and the image-magic checker are not entered. Non-owners return 409. PDF still continues. Offline: 116 passed, listed by `-rA`, and the run exited 0. The later 118-test run passed these three tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` keeps the 2048-byte NUL window for `note.BIN` with content type `application/octet-stream`. A NUL at index 2047 continues with empty text and both write flags false, and `decode_text_file` is not entered. A NUL at index 2048 still attaches shipped text with kind `bin` only for the native writer. Normalization strips that NUL and keeps the readable bytes. The Python sniff stays `sample = data[:2048]` and Rust stays `take(2048)`. The window size was not changed. PDF still continues. Offline: 118 passed, listed by `-rA`, and the run exited 0. The later 120-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped decoded text for a dotless name `Notes` with content type `text/plain` and kind `text`, only for the native writer. The kind comes from the empty-extension branch. `extract_html_text` and the image-magic checker are not entered. Whitespace-only `blank` refuses before metadata or cache writes. Non-owners return 409 and do not enter `decode_text_file`. PDF still continues. Offline: 120 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` treats a leading-dot name `.ENV` as an empty extension. With content type `text/plain` it attaches shipped text and kind `text`, not `env`. The token `".env",` stays in `TEXT_EXTENSIONS` and does not classify this name. Whitespace-only `.env` refuses before metadata or cache writes. Non-owners return 409 and do not enter `decode_text_file`. PDF still continues. Rust `extension_of` keeps `index > 0`. Offline: 122 passed, listed by `-rA`, and the run exited 0. The later 124-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped text for `.env.TXT` with content type `text/plain` and kind `txt`, not `env` or `text`, only for the native writer. The last dot supplies `.txt`. Whitespace-only `.env.txt` refuses before metadata or cache writes. Non-owners return 409 and do not enter `decode_text_file`. PDF still continues. The source check requires `".txt",` in `TEXT_EXTENSIONS`. Offline: 124 passed, listed by `-rA`, and the run exited 0. The later 126-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches shipped decoded text for a trailing-dot name `Notes.` with content type `text/plain` and kind `text`, not `"."`, only for the native writer. Python `Path.suffix` for that name is empty. Whitespace-only `blank.` refuses before metadata or cache writes. Non-owners return 409 and do not enter `decode_text_file`. PDF still continues. Rust text admission trims a leading dot before the empty-kind branch. Offline: 126 passed, listed by `-rA`, and the run exited 0. The later 127-test run passed these two tests again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` does not trim a text content type. For `note.BIN`, ` text/plain` with a NUL continues with empty text and both write flags false, and `decode_text_file` is not entered. The same payload with `text/plain` still attaches kind `bin` only for the native writer. `extract_html_text` and the image-magic checker are not entered. Non-owners return 409. PDF still continues. Rust `is_text_file` has no `trim()` before `starts_with("text/")`. Offline: 127 passed, listed by `-rA`, and the run exited 0. The later 128-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` does not lowercase a text content type. For `note.BIN`, `TEXT/PLAIN` with a NUL continues with empty text and both write flags false, and `decode_text_file` is not entered. The same payload with `text/plain` still attaches kind `bin` only for the native writer. `extract_html_text` and the image-magic checker are not entered. Non-owners return 409. PDF still continues. Rust `is_text_file` and `admit_file_text_part` have no `to_ascii_lowercase()` before `starts_with("text/")`. Offline: 128 passed, listed by `-rA`, and the run exited 0. The later 129-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` selects HTML extraction by extension. For `note.BIN`, content type `text/html` attaches shipped decoded text and kind `bin`, keeps the markup, and does not enter `extract_html_text`. Whitespace-only bytes refuse before metadata or cache writes. Non-owners return 409. PDF still continues. The HTML arm does not consult `content_type`. Offline: 129 passed, listed by `-rA`, and the run exited 0. The later 130-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` does not treat `.XHTML` as HTML. With `application/octet-stream` and no NUL, it attaches shipped decoded text and kind `xhtml`, keeps the markup, and does not enter `extract_html_text`. Whitespace-only `blank.xhtml` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Rust `HTML_EXTENSIONS` has `".html",` and `".htm"]` and not `".xhtml"`. Offline: 130 passed, listed by `-rA`, and the run exited 0. The later 131-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` still decodes `page.XHTML` when the content type is `text/html`. Kind stays `xhtml`, markup stays in the text, and `extract_html_text` is not entered. Whitespace-only `blank.xhtml` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 131 passed, listed by `-rA`, and the run exited 0. The later 132-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` decides the raw `text/` prefix before the NUL scan for `page.XHTML`. `text/html` with a NUL still attaches kind `xhtml` only for the native writer, and normalization strips that NUL. The same NUL with `application/octet-stream` continues, and `decode_text_file` is not entered. `extract_html_text` is not entered. Non-owners return 409. PDF still continues. Offline: 132 passed, listed by `-rA`, and the run exited 0. The later 133-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` does not trim `text/html` on `page.XHTML`. ` text/html` with a NUL continues, and `decode_text_file` is not entered. The same payload with `text/html` still attaches kind `xhtml` only for the native writer. `extract_html_text` is not entered. Non-owners return 409. PDF still continues. Offline: 133 passed, listed by `-rA`, and the run exited 0. The later 134-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` does not lowercase `text/html` on `page.XHTML`. `TEXT/HTML` with a NUL continues, and `decode_text_file` is not entered. The same payload with `text/html` still attaches kind `xhtml` only for the native writer. `extract_html_text` is not entered. Non-owners return 409. PDF still continues. Offline: 134 passed, listed by `-rA`, and the run exited 0. The later 135-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` keeps `text/html; charset=utf-8` on the raw prefix for `page.XHTML`. A NUL still attaches kind `xhtml` only for the native writer. The text path does not split on `;`. The same NUL with `application/xhtml+xml` continues, and `decode_text_file` is not entered. `extract_html_text` is not entered. Non-owners return 409. PDF still continues. Offline: 135 passed, listed by `-rA`, and the run exited 0. The later 136-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues `notes.PDF` before a `text/plain` content type. Readable bytes continue, and `decode_text_file` is not entered. The same bytes named `note.BIN` with `text/plain` still attach kind `bin` only for the native writer. Non-owners return 409. A lowercase `notes.pdf` still continues. Offline: 136 passed, listed by `-rA`, and the run exited 0. The later 137-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` classifies `scan.GIF` before `text/plain`. OCR off returns `ocr_required` and status 415 before `decode_text_file` and before the image-magic checker. The same `GIF89a` bytes named `note.BIN` with `text/plain` still attach kind `bin` only for the native writer. OCR on continues without invented text. Non-owners return 409. PDF still continues. Offline: 137 passed, listed by `-rA`, and the run exited 0. The later 138-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` does not decode a corrupt `scan.GIF` as text. With `text/plain` and OCR on, unrecognized bytes return `Uploaded image is corrupt.` and status 400 before `decode_text_file`. The same bytes named `note.BIN` with `text/plain` still attach kind `bin` only for the native writer. OCR off still returns `ocr_required` before magic. Non-owners return 409. PDF still continues. Offline: 138 passed, listed by `-rA`, and the run exited 0. The later 139-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` before `text/plain`. `extract_html_text` runs and the admission `decode_text_file` is not entered. `files.decode_text_file` stays unpatched because the HTML extractor calls that global. The same markup named `note.BIN` with `text/plain` attaches kind `bin` and keeps the markup. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 139 passed, listed by `-rA`, and the run exited 0. The later 140-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` when the content type is `application/octet-stream`. `extract_html_text` runs and the admission `decode_text_file` is not entered. The same markup named `note.BIN` with no NUL attaches kind `bin` and keeps the markup. The HTML extractor is not entered for that bin name. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 140 passed, listed by `-rA`, and the run exited 0. The later 141-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` when a NUL is inside the 2048-byte window and the content type is `application/octet-stream`. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` continue with empty text and both write flags false. The Rust HTML arm does not contain `take(2048)`. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 141 passed, listed by `-rA`, and the run exited 0. The later 142-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` classifies ` Image/GIF ; charset=binary` on `page.HTML` before HTML extraction. OCR off returns 415 before magic. OCR on with a non-image payload returns `Uploaded image is corrupt.` before `extract_html_text`. `blank.html` with that image type returns 415, not 422. The same markup named `note.BIN` with `application/octet-stream` and no NUL still attaches kind `bin`. Non-owners return 409. PDF still continues. Offline: 142 passed, listed by `-rA`, and the run exited 0. The later 143-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues a recognized `GIF89a` header named `page.HTML` when the content type is ` Image/GIF ; charset=binary` and OCR is on. `extract_html_text` is not entered and no OCR text is invented. OCR off still returns 415 before magic. The same header named `note.BIN` with `application/octet-stream` and no NUL still attaches kind `bin`. Non-owners return 409. PDF still continues. Offline: 143 passed, listed by `-rA`, and the run exited 0. The later 144-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` with content type `text/plain` even when the bytes are a `GIF89a` header. The image-magic checker is not entered. The admission `decode_text_file` is not entered. The same header named `note.BIN` with `text/plain` still attaches kind `bin`. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. The Rust HTML arm does not contain `image_magic_ok(`. Offline: 144 passed, listed by `-rA`, and the run exited 0. The later 145-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTM` with content type `text/plain` and a `GIF89a` header. The kind is `htm`. The image-magic checker is not entered. The admission `decode_text_file` is not entered. The same header named `note.BIN` with `text/plain` still attaches kind `bin`. Whitespace-only `blank.HTM` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 145 passed, listed by `-rA`, and the run exited 0. The later 146-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` classifies ` Image/GIF ; charset=binary` on `page.HTM` before HTML extraction. OCR off returns 415 before magic. OCR on with `GIF89aMARKERgif` continues and does not invent OCR text. The same header named `note.BIN` with `text/plain` still attaches kind `bin`. Non-owners return 409. PDF still continues. Offline: 146 passed, listed by `-rA`, and the run exited 0. The later 147-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` does not decode a corrupt image named `page.HTM`. With ` Image/GIF ; charset=binary` and OCR on, unrecognized bytes return `Uploaded image is corrupt.` before `extract_html_text`. `blank.HTM` with that image type and OCR off returns 415, not 422. The same markup named `note.BIN` with `text/plain` still attaches kind `bin`. Non-owners return 409. PDF still continues. Offline: 147 passed, listed by `-rA`, and the run exited 0. The later 148-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTM` with content type `text/html` and a `GIF89a` header. The kind is `htm`. The image-magic checker is not entered. The admission `decode_text_file` is not entered. The same header named `note.BIN` with `text/html` still attaches kind `bin` and does not enter `extract_html_text`. Whitespace-only `blank.HTM` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 148 passed, listed by `-rA`, and the run exited 0. The later 149-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` with content type `text/html` and a `GIF89a` header. The kind is `html`. The image-magic checker is not entered. The admission `decode_text_file` is not entered. The same header named `note.BIN` with `text/html` still attaches kind `bin` and does not enter `extract_html_text`. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 149 passed, listed by `-rA`, and the run exited 0. The later 150-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` with content type `TEXT/HTML` and a NUL inside the first 2048 bytes. The kind is `html`. The image-magic checker is not entered. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` with `TEXT/HTML` continue because the raw prefix is not `text/` and the NUL blocks the sniffer. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 150 passed, listed by `-rA`, and the run exited 0. The later 151-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` with content type ` text/html` and a NUL inside the first 2048 bytes. The kind is `html`. The text path does not trim. The image-magic checker is not entered. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` with ` text/html` continue because the raw prefix is not `text/` and the NUL blocks the sniffer. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 151 passed, listed by `-rA`, and the run exited 0. The later 152-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` with content type `text/html; charset=utf-8` and a NUL inside the first 2048 bytes. The kind is `html`. The HTML arm does not split on `;`. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` still attach kind `bin` because the raw prefix is `text/` before the NUL scan. `application/xhtml+xml` on `note.BIN` continues. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 152 passed, listed by `-rA`, and the run exited 0. The later 153-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTM` with content type `TEXT/HTML` and a NUL inside the first 2048 bytes. The kind is `htm`, not `html`. The image-magic checker is not entered. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` with `TEXT/HTML` continue because the raw prefix is not `text/` and the NUL blocks the sniffer. Whitespace-only `blank.HTM` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 153 passed, listed by `-rA`, and the run exited 0. The later 154-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTM` with content type ` text/html` and a NUL inside the first 2048 bytes. The kind is `htm`, not `html`. The text path does not trim. The image-magic checker is not entered. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` with ` text/html` continue because the raw prefix is not `text/` and the NUL blocks the sniffer. Whitespace-only `blank.HTM` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 154 passed, listed by `-rA`, and the run exited 0. The later 155-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTM` with content type `text/html; charset=utf-8` and a NUL inside the first 2048 bytes. The kind is `htm`, not `html`. The HTML arm does not split on `;`. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` still attach kind `bin` because the raw prefix is `text/` before the NUL scan. `application/xhtml+xml` on `note.BIN` continues. Whitespace-only `blank.HTM` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 155 passed, listed by `-rA`, and the run exited 0. The later 156-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTML` with exact content type `text/html` and a NUL inside the first 2048 bytes. The kind is `html`. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` still attach kind `bin` because the raw prefix is `text/` before the NUL scan. Whitespace-only `blank.html` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 156 passed, listed by `-rA`, and the run exited 0. The later 157-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` extracts `page.HTM` with exact content type `text/html` and a NUL inside the first 2048 bytes. The kind is `htm`, not `html`. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` still attach kind `bin` because the raw prefix is `text/` before the NUL scan. Whitespace-only `blank.HTM` refuses before metadata or cache writes. Non-owners return 409. PDF still continues. Offline: 157 passed, listed by `-rA`, and the run exited 0. The later 158-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` classifies `scan.GIF` before exact `text/html`. OCR off returns 415 before magic. OCR on with `GIF89a` plus a NUL continues without invented text. The same bytes named `note.BIN` still attach kind `bin` because the raw prefix is `text/` before the NUL scan. Non-owners return 409. PDF still continues. Offline: 158 passed, listed by `-rA`, and the run exited 0. The later 159-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` does not decode a corrupt `scan.GIF` when the content type is exact `text/html`. OCR on returns `Uploaded image is corrupt.` before `decode_text_file` and before `extract_html_text`. OCR off returns 415 before magic. The same bytes named `note.BIN` still attach kind `bin`. Non-owners return 409. PDF still continues. Offline: 159 passed, listed by `-rA`, and the run exited 0. The later 160-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues `notes.PDF` before exact `text/html` even when a NUL is inside the first 2048 bytes. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` still attach kind `bin` because the raw prefix is `text/` before the NUL scan. A lowercase `notes.pdf` also continues. Non-owners return 409. Offline: 160 passed, listed by `-rA`, and the run exited 0. The later 161-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues `notes.PDF` when the content type is `TEXT/HTML` and a NUL is inside the first 2048 bytes. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` continue because the raw prefix is not `text/` and the NUL blocks the sniffer. A lowercase `notes.pdf` also continues. Non-owners return 409. No `project.json` or `cache.txt` is written. Offline: 161 passed, listed by `-rA`, and the run exited 0. The later 162-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues `notes.PDF` when the content type is ` text/html` and a NUL is inside the first 2048 bytes. The text path does not trim. The admission `decode_text_file` is not entered. The same bytes named `note.BIN` continue because the raw prefix is not `text/` and the NUL blocks the sniffer. A lowercase `notes.pdf` also continues. Non-owners return 409. No `project.json` or `cache.txt` is written. Offline: 162 passed, listed by `-rA`, and the run exited 0. The later 163-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues `notes.PDF` when the content type is `text/html; charset=utf-8` and a NUL is inside the first 2048 bytes. The admission `decode_text_file` is not entered for that PDF name. The same bytes named `note.BIN` attach kind `bin` because the raw prefix is `text/` before the NUL scan. `application/xhtml+xml` on `note.BIN` continues. A lowercase `notes.pdf` also continues. Non-owners return 409. Offline: 163 passed, listed by `-rA`, and the run exited 0. The later 164-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues `notes.PDF` when the content type is ` Image/GIF ; charset=binary`, with OCR off and with OCR on. The PDF arm returns before the image arm. Image magic is not entered. The same bytes named `note.BIN` with OCR off return 415 before magic. A lowercase `notes.pdf` also continues. Non-owners return 409. No `project.json` or `cache.txt` is written. Offline: 164 passed, listed by `-rA`, and the run exited 0. The later 165-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues `notes.PDF` when the content type is ` Image/GIF ; charset=binary` and the bytes are not a recognized image header. OCR on does not return `Uploaded image is corrupt.` for that PDF name. The same bytes named `note.BIN` with OCR on do. OCR off on `note.BIN` returns 415 before magic. A lowercase `notes.pdf` also continues. Non-owners return 409. No `project.json` or `cache.txt` is written. Offline: 165 passed, listed by `-rA`, and the run exited 0. The later 166-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` refuses an empty `notes.PDF` before the PDF continue arm. The message keeps the original filename. An image content type and OCR on do not change that empty refusal. An empty `note.BIN` uses its own filename. Non-owners return 409. No `project.json` or `cache.txt` is written. Offline: 166 passed, listed by `-rA`, and the run exited 0. The later empty-order log passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] The project-file source gate pins the empty check before the PDF arm. Rust `data.is_empty()` comes before `extension == ".pdf"`. Python `payload == b""` comes before `_CONTINUED_EXTENSIONS`. Offline: 166 passed, listed by `-rA`, and the run exited 0. The later 167-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues a whitespace-only `notes.PDF`. The same bytes named `note.BIN` with `text/plain` return `No readable text found in this file`. A lowercase `notes.pdf` also continues. Non-owners return 409. No `project.json` or `cache.txt` is written. Offline: 167 passed, listed by `-rA`, and the run exited 0. The later 168-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` continues a whitespace-only `notes.PDF` when the content type is `None`. The same bytes named `note.BIN` with content type `None` return `No readable text found in this file`. A lowercase `notes.pdf` also continues. Non-owners return 409. No `project.json` or `cache.txt` is written. Offline: 168 passed, listed by `-rA`, and the run exited 0. The later 169-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches readable text when the filename is `None`. The kind is `text`. Both write flags are true only for the native writer. The admission decoder is entered once. Non-owners return 409. Offline: 169 passed, listed by `-rA`, and the run exited 0. The later 170-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` attaches readable text when the filename and content type are empty strings. The kind is `text`. Both write flags are true only for the native writer. The admission decoder is entered once. Non-owners return 409. Offline: 170 passed, listed by `-rA`, and the run exited 0. The later 171-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with an empty content type and a NUL continues. The admission decoder is not entered. The same bytes with `text/plain` attach kind `text` and set both write flags only for the native writer. Non-owners return 409 and write nothing. Offline: 171 passed, listed by `-rA`, and the run exited 0. The later 172-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type `TEXT/PLAIN` and a NUL continues because the raw prefix is not `text/` and the NUL blocks the sniffer. The text path does not lowercase the content type. The admission decoder is not entered. Non-owners return 409. No metadata or cache file is written. Offline: 172 passed, listed by `-rA`, and the run exited 0. The later 173-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type ` text/plain` and a NUL continues because the text path does not trim and the NUL blocks the sniffer. The admission decoder is not entered. Non-owners return 409. No metadata or cache file is written. Offline: 173 passed, listed by `-rA`, and the run exited 0. The later 174-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type `text/plain; charset=utf-8` and a NUL attaches kind `text` because the raw prefix is `text/` before the NUL scan. The text path does not split on `;`. The admission decoder is entered once. `application/octet-stream` on the same bytes continues, and the decoder is not entered for that call. Both write flags are true only for the native writer. Non-owners return 409. Offline: 174 passed, listed by `-rA`, and the run exited 0. The later 175-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type ` text/plain; charset=utf-8` and a NUL continues because the text path does not trim and does not split on `;`. The NUL blocks the sniffer. The admission decoder is not entered. Non-owners return 409. No metadata or cache file is written. Offline: 175 passed, listed by `-rA`, and the run exited 0. The later 176-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type `TEXT/PLAIN; charset=utf-8` and a NUL continues because the text path does not lowercase and does not split on `;`. The NUL blocks the sniffer. The admission decoder is not entered. Non-owners return 409. No metadata or cache file is written. Offline: 176 passed, listed by `-rA`, and the run exited 0. The later 177-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with a tab before `text/plain` and a NUL continues because the text path does not trim. The NUL blocks the sniffer. The admission decoder is not entered. Non-owners return 409. No metadata or cache file is written. Offline: 177 passed, listed by `-rA`, and the run exited 0. The later 178-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with `text/plain` followed by a tab and a NUL attaches kind `text` because the raw prefix is `text/` before the NUL scan. A trailing tab does not stop that prefix. The admission decoder is entered once. `application/octet-stream` on the same bytes continues, and the decoder is not entered for that call. Both write flags are true only for the native writer. Non-owners return 409. Offline: 178 passed, listed by `-rA`, and the run exited 0. The later 179-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type exactly `text/` and a NUL attaches kind `text` because the raw prefix is `text/` before the NUL scan. No subtype is required. The admission decoder is entered once. `application/octet-stream` on the same bytes continues, and the decoder is not entered for that call. Both write flags are true only for the native writer. Non-owners return 409. Offline: 179 passed, listed by `-rA`, and the run exited 0. The later 180-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type `text` and no slash, plus a NUL, continues because the raw prefix is not `text/`. The slash is required. The admission decoder is not entered. Non-owners return 409. No metadata or cache file is written. Offline: 180 passed, listed by `-rA`, and the run exited 0. The later 181-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type `Text/` and a NUL continues because the text path does not lowercase. The NUL blocks the sniffer. The admission decoder is not entered. Non-owners return 409. No metadata or cache file is written. Offline: 181 passed, listed by `-rA`, and the run exited 0. The later 182-test run passed this test again. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] An empty filename with content type `TEXT/` and a NUL continues because the text path does not lowercase. The slash is present. The NUL blocks the sniffer. The admission decoder is not entered. Non-owners return 409. No metadata or cache file is written. Offline: 182 passed, listed by `-rA`, and the run exited 0. Ruff and three-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `admit_selectable_pdf` refuses a textless PDF with the shipped OCR-required error and does not enter OCR. A selectable PDF still returns shipped text and the test writes `cache.txt` only then. `POST /api/project-files` continues `notes.PDF` and `notes.pdf` without entering `extract_pdf_text`. Non-owners return 409. No `project.json` is written. Offline: 4 passed, listed by `-rA`, and the run exited 0. The later 5-test run passed this test again. Ruff and four-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `render_pdf_page_layout` returns shipped geometry for page 1 of a one-page PDF: index 1, page count 1, width 200, height 200. Page 5 clamps to index 1. Non-PDF bytes return 415. The PNG renderer is not entered. `pdftoppm` is not called. Offline: 5 passed, listed by `-rA`, and the run exited 0. Ruff and four-file mypy passed in that log. Rust was not compiled. Go was not edited. No epoch was installed. This is not 完成验收.
- [x] `GET /api/file-page-layout` on the prebuilt native gateway refuses a cached non-PDF with `Page text layout is only available for PDF files` before the renderer message. A cached one-page PDF requested at page 5 returns index 1, page count 1, width 612, height 792, and text `Hello pdf\nWorld` with the shipped word boxes. A PDF index whose bytes are not a PDF returns the renderer message `PDF page text layout is unavailable`. The cache directory is unchanged and no PNG is written. Python `file_page_layout`, `render_pdf_page_layout`, and `render_pdf_page_png` are patched and are not entered. `pdftoppm` is not called. Offline: 6 passed, listed by `-rA`, and the run exited 0. Ruff, four-file mypy, and `rustfmt --check` passed in that log. An earlier rustfmt check failed on wrapping; the recorded log is the rerun after formatting. Cargo was not started, so the new Rust unit test was not executed. The gateway binary is the 2026-10-07 prebuilt `deepseek-gateway.exe`, 106881024 bytes, SHA-256 `BE20445DA7B186421DED7F12D458D0C8EA22860767A7FD067686566505286371`, not a compile of this HEAD. No epoch was installed. This is not 完成验收.
- [x] `GET /api/file-page-search` on the same prebuilt native gateway refuses a blank query and a whitespace query with `Search query is required` and status 400. Those responses have no `matches` field. Query `beta` returns two matches, pages 1 and 2, and the matched text `beta`. Query `strasse` matches the cached `Straße` page. The cache file is unchanged and no PNG is written. Python `file_page_search` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-page-text` on the same prebuilt native gateway returns `page one` for page 1 and `page two` for page 2. Page 9 clamps to index 2 and still returns `page two`. A 32-hex id with no index returns status 410, code `file_index_expired`, and `Uploaded file index has expired or is missing`. The cache names and source bytes stay unchanged, and the missing id writes no index. Python `file_page_text` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-reader` on the same prebuilt native gateway returns the first stored chunk at start 1 and the second stored chunk, including `BETA-MARKER`, at start 2. A non-numeric `chunkStart` returns status 400 and `Invalid reader start`. A missing 32-hex id returns status 410 and `Uploaded file index has expired or is missing` before that start check. The returned chunk has no `vector`. The cache names and source bytes stay unchanged. Python `file_reader_window` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-chunk` on the same prebuilt native gateway returns the writer's first raw chunk at index 1 and the second raw chunk, including `BETA-MARKER` and `vector`, at index 2. An index past the end returns status 404, code `not_found`, and `Chunk not found`. A missing 32-hex id with numeric index 1 returns status 410, code `file_index_expired`, and `Uploaded file index has expired or is missing`. The same missing id with `chunkIndex` `bad` returns status 400 and `Invalid chunk index`, because the index is parsed before the cache load. The cache names and source bytes stay unchanged. Python `load_cached_file` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /api/file-source` on the same prebuilt native gateway returns the source bytes `cache_file_chunks` wrote. The content type is `text/plain; charset=utf-8`. `download=false` stays inline. `download=1` is an attachment. A missing 32-hex id returns status 410 and `Uploaded file index has expired or is missing`. After the source file is removed, the same id returns status 410 and `Original uploaded file has expired or is missing`. The index bytes stay unchanged. Python `cached_file_source` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-text` on the same prebuilt native gateway extracts `notes.txt` as kind `txt` with text `hello\nworld`. The gateway writes `{fileId}.json` and `{fileId}.source`. `GET /api/file-source` returns those source bytes. A non-multipart body returns status 400 and `Expected multipart/form-data`. An empty file returns status 400 and `Uploaded file is empty: empty.txt`. Both refusals leave the cache empty. Python `extract_uploaded_file` and `cache_file_chunks` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-text` on the same prebuilt native gateway extracts `page.html` as kind `html` with text `Hello`. The script text is absent from that text and present in the stored source. `page.htm` returns kind `htm`, not `html`, and the same text. `GET /api/file-source` returns the original HTML for both ids. A script-only `page.html` returns status 422 and `No readable text found in this file` and writes nothing. Python `extract_uploaded_file` and `cache_file_chunks` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/file-text` on the same prebuilt native gateway returns kind `htm`, not `html`, for `page.HTM`. The text is `Hello`, and the stored source still contains the script. A whitespace-only `blank.html` returns status 422 and `No readable text found in this file` and writes nothing. Python `extract_uploaded_file` and `cache_file_chunks` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` on the same prebuilt native gateway returns status 409 and `NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` when the mode is `python_authoritative`. `project.json` bytes stay unchanged and no files directory is created. With `DEEPSEEK_RUNTIME_MODE=python_disabled`, `notes.txt` is stored as kind `txt` with preview `hello project`. The source bytes match the upload. `GET /api/workspace/projects/proj-demo` reads that document back. `POST /api/file-reader` and `GET /api/file-source` with the project id return `hello project`. `.local-rag` is not created. Python `admit_project_file_part` and `write_project` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` with `python_disabled` returns status 415, code `unsupported_file`, and the shipped unsupported-type message for `blob.bin` containing a NUL. `project.json` stays equal to the seed and no files directory is created. A missing project id returns status 404, code `not_found`, and `Project not found`, and no project directory is created. Python `admit_project_file_part` and `write_project` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` with `python_disabled` and auth enabled returns status 401, code `UNAUTHORIZED`, and `Auth required` when no token is sent. `project.json` stays equal to the seed and no files directory is created. The same upload with the configured bearer token returns `ok`, kind `txt`, preview `hello project`, and the source bytes. `AUTH_DISABLED` is unset. Python `admit_project_file_part` and `write_project` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` with `python_disabled` and the bearer token returns status 400, code `invalid_payload`, and `No file uploaded` for a multipart body that contains a field and no file. `project.json` stays equal to the seed and no files directory is created. `AUTH_DISABLED` is unset. Python `admit_project_file_part` and `write_project` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/project-files` with `python_disabled` and the bearer token returns status 400, code `invalid_payload`, and `Uploaded file is empty: notes.txt` for an empty `notes.txt`. `project.json` stays equal to the seed and no files directory is created. `AUTH_DISABLED` is unset. Python `admit_project_file_part` and `write_project` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /api/download` on the same prebuilt native gateway returns the markdown bytes `store_generated_file` wrote, with content type `text/markdown; charset=utf-8` and `attachment; filename="notes.md"`. `inline=1` does not make markdown inline. An SVG with `inline=false` stays `attachment; filename="mindmap.svg"`. `inline=1` makes that SVG inline. A missing 32-hex id returns status 404, code `not_found`, and `File does not exist or has expired`. The generated files are unchanged. Python `store_generated_file` is patched after the write and is not entered by the HTTP calls. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. An earlier mypy check failed because `write_bytes` returns an int; the recorded log is the rerun after the writers return None. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /api/download` on the same prebuilt native gateway treats SVG `inline=yes` and `inline=on` as `inline; filename="mindmap.svg"` with content type `image/svg+xml`. `inline=0` stays `attachment; filename="mindmap.svg"`. Both return the SVG bytes `store_generated_file` wrote and send `Cache-Control: no-store`. The stored file is unchanged. Python `store_generated_file` is patched after the write and is not entered by the HTTP calls. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /api/config` on the same prebuilt native gateway returns version `4.8.0`, no server key, no search key, default model `deepseek-v4-pro`, the two shipped models, upload limits 200000000 / 220000000 / 20, MCP `2025-06-18`, and A2A `0.3.0`. The response has no `ocr` key. `AUTH_DISABLED=1` returns that document with no token. With auth enabled, a missing token and a wrong bearer token return status 401 and the nested `UNAUTHORIZED` / `Auth required` body. The configured bearer token returns the same document. Current source `api_config` also emits `ocr`; this binary does not, so that field is not proven here. The first pytest failed on the extra `ocr` expectation. The recorded log is the rerun. Python `create_status_router` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /policy/url` on the same prebuilt native gateway allows `https://example.com` with code `allowed`, capability `NetworkFetch`, and risk `High`. `http://127.0.0.1/` returns code `localhost_blocked` and `private or local ip is not allowed: 127.0.0.1`. An empty object returns code `invalid_policy_request` and `url is required`. `file:///etc/passwd` returns code `unsupported_scheme` and `scheme not allowed: file`. No bearer token is sent. Decision ids start with `pd_`. Python `evaluate_url_safety` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /policy/path` on the same prebuilt native gateway allows `project/file.txt` under `/workspace` with code `allowed`, capability `ReadFile`, and risk `High`. `../secret.txt` returns code `path_traversal` and `parent directory traversal is not allowed`. `project/.git/config` returns code `protected_path` and `path targets a protected location`. An empty object returns code `invalid_policy_request` and `root is required`. No bearer token is sent. Python `check_path` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /policy/capability` on the same prebuilt native gateway allows `ReadFile` granted to itself with `max_risk` `Low`. The code is `allowed`, the capability is `ReadFile`, and the risk is `Low`. `ShellExec` granted only `ReadFile` returns code `missing_capability` and `required capability was not granted`, with capability `ShellExec` and risk `Critical`. `ShellExec` granted to itself with `max_risk` `Low` returns code `risk_limit_exceeded` and `capability risk exceeds the configured limit`. An empty object returns code `invalid_policy_request` and `requested capability is invalid`. No bearer token is sent. Python `check_capability` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /rag/query/normalize` on the same prebuilt native gateway returns status 200, normalized `rust 语言`, and tokens `rust`, `语言` for `  Rust 语言  `. An empty query returns status 400 and `{"error": "query is empty"}`. No bearer token is sent. Python `normalize_query` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /rag/chunks/score` on the same prebuilt native gateway ranks query `deepseek infra` as ids `exact`, `partial`, `empty`. The scores are `(10 + 1 + 1) * 1.1`, `1 * 1.1`, and `0`. An empty query returns status 400 and `{"error": "query is empty"}`. No bearer token is sent. Python `score_chunks` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /rag/vectors/rank` on the same prebuilt native gateway returns index `1` and similarity `1.0` for query `[1.0, 0.0]` against `[[0.5, 0.0], [1.0, 0.0], [1.0, 0.0]]`. The same query against `[[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]` keeps index `0`. Orthogonal and empty candidates return index `null` and similarity `0.0`. No bearer token is sent. Python `rank_vectors` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /rag/vectors/rank-binary` on the same prebuilt native gateway returns 24 bytes for magic `DSVRNK01`: response magic `DSVRSP01`, index `1`, pad `0`, and similarity `1.0`. The content type is `application/vnd.deepseek.vector-rank.v1+octet-stream`. An orthogonal candidate returns index `4294967295` and similarity `0.0`. Sixteen `X` bytes return status 400 and `invalid_binary_magic`. `Content-Type: application/json` returns status 415 and `invalid_content_type`. No bearer token is sent. Python `rank_vectors` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /rag/citation/format` on the same prebuilt native gateway returns citation `docs/example.md:L10-L20` for lines 10 through 20. Source `  docs/example.md  ` with start line 5 and no end line returns `docs/example.md:L5`. Start 20 and end 10 return status 400 and `{"error": "invalid line range"}`. No bearer token is sent. Python `format_citation` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /rag/index/validate` on the same prebuilt native gateway returns status 200 and `{"valid": true}` for one chunk id `a` with text `first`. Two chunks with id `a` return `valid` false and `duplicate chunk id: a`. Id `blank` with empty text returns `invalid chunk blank: chunk text is empty`. No bearer token is sent. Python `validate_index` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /rag/documents/prepare` on the same prebuilt native gateway returns `ok` true for document `doc-1` and text `中文🚀abc` with chunk size 3 and overlap 1. The character count is 6 and the chunk count is 3. Chunk texts are `中文🚀`, `🚀ab`, and `bc`, with starts 0, 2, and 4. Metadata keeps `notes.txt` and `text/plain`. `chunkChars` 4 with `chunkOverlap` 4 returns `ok` false and `chunk_overlap_too_large`. No bearer token is sent. Python `prepare_document` is patched and is not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /api/tool-policy` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. The configured bearer token returns `ok` true, `toolPolicy.enabled` true, `toolPolicy.enforceSchema` false, at least 28 tools starting with `web_search`, and an `audit` array that contains the seeded `.tool-audit/audit.jsonl` object. `limit=not-a-number` returns the same policy and that audit object. The audit file bytes stay unchanged. `AUTH_DISABLED` is unset. Python `create_status_router`, `tool_policy_status`, and `read_recent_audit` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /api/budget` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. The configured bearer token returns `ok` true, `budget.enabled` true, scope `global`, policy `none` with zero limits, the shipped model prices, an empty `today` spend view, and `overBudget.exceeded` false. `scope=agent` returns scope `agent`. A whitespace scope stays `global`. `.budget/budget.db` is not created. `AUTH_DISABLED` is unset. Python `create_status_router`, `budget_status`, and `daily_spend` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /api/rag/status` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. The configured bearer token returns `ok` true, `localRag.enabled` true, backend `sqlite_vec`, `sqliteVecAvailable` false, embedding provider `hash`, dimensions `64`, `hybridSearch` `bm25+vector`, and zero indexed counts. `.local-rag/rag.sqlite3` is not created. `LOCAL_RAG_ENABLED=false` returns `enabled` false and still creates no database. `AUTH_DISABLED` is unset. Python `create_status_router` and local RAG `status` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /api/gateway/status` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. The configured bearer token returns `ok` true, context manager enabled, `stableJson` true, `toolOrder` `function.name`, and `slidingWindowMessages` 36. The request queue reports `ported` false and `native request queue is not ported`, with no `counts`. The scheduler reports `ported` false and `python job scheduler is not ported`. `GATEWAY_CONTEXT_MANAGER_ENABLED=false` with window `12` returns enabled false and window 12. `AUTH_DISABLED` is unset. Python `create_status_router` and `gateway_status` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The request queue and scheduler remain unported.
- [x] `GET /api/taint` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. The configured bearer token returns `ok` true, taint enabled, search and file hardening on, escalate confirm on, trust levels `trusted` and `untrusted`, the ten shipped sources, three exfiltration patterns, three tool-directive patterns, and a sensitive tool list that includes `fetch_url`. `TAINT_ENABLED=false` returns `enabled` false. `AUTH_DISABLED` is unset. Python `create_status_router` and `taint_status` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/title` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. A non-empty `userMessage` with no API key returns status 400, `Missing DeepSeek API Key.`, and `missing_api_key`. An API key with an empty, whitespace, or omitted `userMessage` returns status 200 and `{"title": ""}`. `DEEPSEEK_API_URL` points at a closed port, and those empty titles do not depend on it. `AUTH_DISABLED` is unset. Python `create_chat_router` and `generate_title_payload` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. A non-empty message with a key still needs a real upstream title response.
- [x] `POST /api/conversations/search` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. A whitespace query returns `{"results": []}` even when `conversations` is not a list. An empty query returns the same body. Query `beta` with an object `conversations` returns status 400, `conversations must be a list`, and `invalid_payload`. Query `  BETA  ` returns `conv-1` for title `Beta marker` and `conv-msg` for message `m1` with snippet `user see BETA-MARKER here`. A non-object list entry is skipped. `AUTH_DISABLED` is unset. Python `create_chat_router`, `conversation_search`, and `conversation_search_matches` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /healthz`, `GET /readyz`, and `GET /v1/models` on the same prebuilt native gateway. With auth enabled and no API key, `/healthz` needs no token and returns `ok` true, service `deepseek-gateway-rs`, version `4.8.0`, and `auth_enabled` true. `/readyz` returns `ready`, tracing `disabled`, and model provider `unconfigured`. `/v1/models` returns 401 without a token and the two shipped model ids with the bearer token. `AUTH_DISABLED=1` with a probe API key returns `auth_enabled` false, provider `configured`, and the same catalog with no token. Python `healthz`, `readyz`, and `create_chat_router` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /.well-known/agent-card.json` on the same prebuilt native gateway needs no token when `A2A_ENABLED` is unset. The card is protocol `0.3.0`, name `DeepSeek Infra Orchestrator`, URL `http://127.0.0.1:8000/a2a/agents/orchestrator`, transport `JSONRPC`, version `4.8.0`, and skill `orchestrator.respond` with tags `orchestrator` and `full_tool_surface`. The same process returns 401 for `GET /api/config` with no token. `A2A_ENABLED=false` returns status 403 and `A2A mesh is disabled`. `AUTH_DISABLED` is unset. Python `create_a2a_router` and `agent_card` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `GET /a2a/agents` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. The bearer token returns `ok` true and the five shipped agents: orchestrator, researcher, coder, reasoner, and critic, each at `http://127.0.0.1:8000/a2a/agents/{id}` with protocol `0.3.0` and version `4.8.0`. `A2A_ENABLED=false` still returns 401 without a token, and the bearer token returns status 403 and `A2A mesh is disabled`. Python `create_a2a_router` and `agent_cards` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /a2a` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. The bearer `message/send` with no params, id `a2a-1`, returns HTTP 200, JSON-RPC `-32602`, and `message.parts must contain non-empty text`, with no `result`. An empty `parts` array, id `a2a-empty`, returns the same error. `A2A_ENABLED=false` still returns 401 without a token, and the bearer token returns status 403 and `A2A mesh is disabled`. Python `create_a2a_router` and `handle_a2a_message` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. A successful A2A `message/send` remains unproven.
- [x] `POST /mcp` on the same prebuilt native gateway returns status 401 and the nested `UNAUTHORIZED` / `Auth required` body for a missing token and a wrong bearer token. Bearer `initialize` returns protocol `2025-06-18`, server `deepseek-infra`, and version `4.8.0`. Bearer `ping` returns an empty result. Bearer `tools/list` includes `python_eval`, `web_search`, and `create_pptx`. Bearer `python_eval` of `2+2` returns `isError` false and result `4`. A `tools/call` with no name returns JSON-RPC `-32602` and `name is required`. Python `create_mcp_router`, `handle_mcp_message`, and `python_eval` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `python_eval` on the same prebuilt native gateway returns `720` for `factorial(6)`, `7` for `1 + 2 * 3`, and `2.0` for `math.sqrt(4)`. Empty and whitespace expressions return `python_eval expression is empty`. A 1001-character expression returns `python_eval expression is too long`. `__import__('os')` returns `Function is not allowed: __import__`. `not_a_tool` returns code `forbidden` and `Tool 'not_a_tool' was blocked by tool policy (unknown_tool)`. A missing token returns 401. The first pytest failed on the unknown-tool code. The recorded log is the rerun. Python `create_mcp_router`, `handle_mcp_message`, and `python_eval` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` resources and prompts on the same prebuilt native gateway return `runtime://capabilities` and read capability `full`. An empty URI returns `-32602` and `uri is required`. `prompts/list` returns `slides-outline` then `research-brief`. Method `nope/method` returns `-32601`. `notifications/initialized` returns HTTP 202 and an empty body. A missing token returns 401. Python `create_mcp_router` and `handle_mcp_message` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `prompts/get` on the same prebuilt native gateway returns the shipped `slides-outline` message for topic `季度回顾` and audience `工程团队`, and the shipped `research-brief` message for topic `Rust 网关`. An empty name returns `-32602` and `name is required`. Prompt `missing-prompt` returns `Prompt not found` with data code `not_found`. A non-object `arguments` value returns `arguments must be an object`. A missing token returns 401. Python `create_mcp_router` and `handle_mcp_message` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /a2a` task reads on the same prebuilt native gateway return status 401 for a missing token and a wrong bearer token. Bearer `tasks/list` returns `{"tasks": []}`. `tasks/get` with a blank id returns `-32602` and `id is required`. `tasks/get` for `task_missing` returns `-32001` and `Task not found`. `tasks/cancel` with no id returns `-32602` and `id is required`. `A2A_ENABLED=false` still returns 401 without a token, and the bearer token returns 403 and `A2A mesh is disabled`. Python `create_a2a_router` and `handle_a2a_message` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. A successful A2A `message/send` remains unproven.
- [x] `POST /a2a` `agent/getAuthenticatedExtendedCard` on the same prebuilt native gateway returns status 401 for a missing token and a wrong bearer token. The bearer token returns the orchestrator card: protocol `0.3.0`, name `DeepSeek Infra Orchestrator`, URL `http://127.0.0.1:8000/a2a/agents/orchestrator`, version `4.8.0`, and skill `orchestrator.respond`. `POST /a2a/agents/no-such-agent` returns `-32001` and `Unknown agent: no-such-agent`. `A2A_ENABLED=false` still returns 401 without a token, and the bearer token returns 403. Python `create_a2a_router`, `handle_a2a_message`, and `agent_card` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `data_transform` on the same prebuilt native gateway returns count `4`, min `1`, max `4`, sum `10`, mean `2.5`, and median `2.5` for `number_summary` of `1 2 3 4`. `no digits here` returns count `0` and no `min`. Operation `nope` returns `Unsupported data_transform operation`. An empty `extract_regex` pattern returns `Regex pattern is required`. A missing token and a wrong bearer token return 401. Python `create_mcp_router`, `handle_mcp_message`, and `data_transform` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `data_transform` `json_path` on the same prebuilt native gateway returns value `20` for `$.a.b[1]` and value `10` for `a.b[0]` on `{"a": {"b": [10, 20]}}`. Path `$.nope` returns code `not_found` and `JSON path not found`. Path `$.a[*]` returns `Unsupported JSON path`. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `data_transform` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `data_transform` `extract_regex` on the same prebuilt native gateway returns two matches for pattern `\d` on `a1b2`: `1` at start `1` end `2`, and `2` at start `3` end `4`. Pattern `(` returns `Invalid regex: (`. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `data_transform` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `data_transform` `csv_summary` on the same prebuilt native gateway returns rows `2`, columns `name` and `value`, and numeric column `value` with count `2` and sum `3.5` for input `name,value` / `x,1` / `y,2.5`. An empty input returns rows `0` and columns `[]`. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `data_transform` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `generate_chart` on the same prebuilt native gateway returns type `line`, title `Revenue`, and markdown `| label | value |\n|---|---:|\n| Q1 | 1.0 |\n| Q2 | 2.5 |` for points `Q1`/`1` and `Q2`/`2.5`. An empty `data` list returns `Chart data must be a non-empty list`. Data `[{"value": 1}]` returns `Chart data has no valid points`. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `generate_chart` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `generate_chart` defaults on the same prebuilt native gateway return type `bar` and title `Chart` for data `[{"label": "a", "value": 1}]` with no type and no title. Type `radar` returns type `bar`. Label `a|b` is escaped as `a\|b` in the markdown table. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `generate_chart` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `generate_chart` keeps only the first 12 raw items on the same prebuilt native gateway. Six unusable items plus `ok`/`"3"` plus `p0` through `p19` return seven points: `ok` value `3.0`, then `p0` through `p5`. `p6` is absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `generate_chart` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /a2a` method `nope/method` on the same prebuilt native gateway returns JSON-RPC `-32601` and `Method not found: nope/method` for the bearer token. A missing token and a wrong bearer token return 401. `A2A_ENABLED=false` returns 401 without a token and 403 with the bearer token. No text part is sent. Python `create_a2a_router` and `handle_a2a_message` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. A successful A2A `message/send` remains unproven.
- [x] `POST /mcp` with `MCP_EXPOSE_RESOURCES=false` and `MCP_EXPOSE_PROMPTS=false` on the same prebuilt native gateway keeps `tools` on `initialize` and omits `resources` and `prompts`. `resources/list` and `prompts/list` are empty. `resources/read` returns `MCP resources are disabled`. `prompts/get` of `slides-outline` returns `MCP prompts are disabled`. Both refusals are JSON-RPC `-32602` with data code `forbidden`. A missing token returns 401. Python `create_mcp_router` and `handle_mcp_message` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /mcp` `list_reminders` on the same prebuilt native gateway reads a seeded `.reminders/reminders.json`. Status `active` returns id `a1`. Status `  NOTIFIED  ` returns id `n1`. Status `bogus` becomes `active` with count `1`. Status `all` returns count `2`. The seeded file bytes stay unchanged. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `list_reminders_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.reminders`.
- [x] `POST /mcp` `list_reminders` on the same prebuilt native gateway returns 50 reminder objects and count `60` for 60 seeded active reminders `a0` through `a59`. Status stays `active`. The seeded file bytes stay unchanged. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `list_reminders_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.reminders`.
- [x] `POST /mcp` `recall_memory` on the same prebuilt native gateway against an empty root returns query `React`, scopes `["global"]`, and `memories` `[]`. Query `   ` returns query `memory`. `.memory/memories.json` is not created. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `recall_memory_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.memory`.
- [x] `POST /mcp` `recall_memory` on the same prebuilt native gateway reads a seeded memory. Query `React` returns id `m1`, content `我用 React`, category `preference`, scope `global`, and `updatedAt` `2026-01-02T00:00:00+00:00`, with exactly those five fields. Query `completely-different-term` on an unrelated memory returns no memories. Both seeded files keep their bytes. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `recall_memory_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.memory`.
- [x] `POST /mcp` `recall_memory` on the same prebuilt native gateway ranks a seeded store. Query `sky` returns ids `pinned`, then `pref`, then `plain`. The seeded file bytes stay unchanged. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `recall_memory_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.memory`.
- [x] `POST /mcp` `recall_memory` on the same prebuilt native gateway keeps project scope `p1` out of a global query. Query `sky` with no scope returns only id `g1` and scopes `["global"]`. The seeded file bytes stay unchanged. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `recall_memory_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.memory`.
- [x] `POST /mcp` `forget_memory` on the same prebuilt native gateway rejects query `   ` with code `invalid_payload` and `forget_memory query is required`. The seeded memories file bytes stay unchanged. A missing token returns 401. No deleting query was sent. Python `create_mcp_router`, `handle_mcp_message`, and `forget_memory_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. A successful delete remains unproven.
- [x] `POST /mcp` `recall_memory` with explicit scope `project:abc` on the same prebuilt native gateway returns scopes `["project:abc"]` and only id `p1` for content `sky`. The seeded file also contains global id `g1` with content `sky`, and those bytes stay unchanged. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `recall_memory_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.memory`.
- [x] `POST /mcp` `recall_memory` on the same prebuilt native gateway returns at most 12 memories. Thirteen global memories `m0` through `m12` with content `sky` and `updatedAt` from `2026-01-13T00:00:00+00:00` down to `2026-01-01T00:00:00+00:00` return ids `m0` through `m11`. Id `m12` is absent. Scopes stay `["global"]`. The seeded file bytes stay unchanged. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `recall_memory_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.memory`.
- [x] `POST /mcp` `list_project_files` on the same prebuilt native gateway lists nothing and does not create `.projects` when that directory is absent. After seeding `proj` / `notes.txt`, `projectId` `proj` and `  proj  ` each return that file and count `1`. `projectId` `nope` returns `not_found` / `Project not found`. `projectId` `x` returns `invalid_payload` / `Invalid project id`. The seeded `project.json` bytes stay unchanged and `.projects` contains only `proj`. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `list_project_files_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.projects`.
- [x] `POST /mcp` `read_file_chunk` on the same prebuilt native gateway returns the seeded `.file-cache` chunk for index `1`, index `0`, and an omitted index. The chunk text is `hello`, lines are `1` through `2`, kind is `text`, and `projectId` is empty. Index `2` returns `not_found` / `Chunk not found`. File id `nope` returns `invalid_payload` / `Invalid file id`. A missing 32-hex id returns `file_index_expired`. The seeded bytes stay unchanged and `.file-cache` contains only that json file. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `read_file_chunk_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.file-cache`.
- [x] `POST /mcp` `read_file_chunk` on the same prebuilt native gateway reads `.projects/proj/files/<32 a>.json`. `projectId` `proj` and `  proj  ` each return text `scoped`, lines `3` through `4`, name `g`, kind `text`, and `projectId` `proj`. No `projectId` and a blank `projectId` return `file_index_expired`. `projectId` `x` returns `invalid_payload` / `Invalid project id`. The seeded bytes stay unchanged, `.file-cache` stays absent, and `.projects` contains only `proj`. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `read_file_chunk_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not the writer of `.projects` or `.file-cache`.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway, against an empty root, rejects query `   ` with `invalid_payload` / `search_files query is empty`. Query `  sky  ` returns query `sky`, `matches` `[]`, and `searchedFiles` `0`. `.file-cache`, `.projects`, and `.local-rag` stay absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. A seeded hit remains unproven.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway returns the seeded global `react-notes.txt` chunk and the `proj_1` `project-guide.txt` chunk for query `useMemo`. `searchedFiles` is `2`. The global `projectId` is empty. Both snippets equal the stored text and both retrieval sources are `json_hybrid`. Both seeded files keep their bytes. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway skips `.file-cache/not-json.json` and `.file-cache/array.json`. The `proj` chunk `needle text` is the only match, `searchedFiles` is `1`, file id is `file`, and retrieval source is `json_hybrid`. The three seeded files keep their bytes. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway caps `limit` `50` at 10 matches. Eleven global caches `n00` through `n10` with chunk text `needle` return names `n00` through `n09`. Name `n10` is absent. `searchedFiles` is `11`. Each match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway uses the default limit of 5. Eleven global caches `n00` through `n10` with chunk text `needle` and no `limit` return names `n00` through `n04`. Name `n05` is absent. `searchedFiles` is `11`. Each match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway floors `limit` `0` at one match. Eleven global caches `n00` through `n10` with chunk text `needle` return only name `n00`. Name `n01` is absent. `searchedFiles` is `11`. The match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway accepts string `limit` `"7"`. Eleven global caches `n00` through `n10` with chunk text `needle` return names `n00` through `n06`. Name `n07` is absent. `searchedFiles` is `11`. Each match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway treats string `limit` `"7.9"` as the default of 5. Eleven global caches `n00` through `n10` with chunk text `needle` return names `n00` through `n04`. Name `n05` is absent. `searchedFiles` is `11`. Each match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway truncates numeric `limit` `3.9` toward zero. Eleven global caches `n00` through `n10` with chunk text `needle` return names `n00` through `n02`. Name `n03` is absent. `searchedFiles` is `11`. Each match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway treats boolean `limit` `true` and boolean `limit` `false` as one match. Eleven global caches `n00` through `n10` with chunk text `needle` return only name `n00` for each. Name `n01` is absent. `searchedFiles` is `11`. The match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway trims string `limit` `"  7  "` to 7. Eleven global caches `n00` through `n10` with chunk text `needle` return names `n00` through `n06`. Name `n07` is absent. `searchedFiles` is `11`. Each match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /mcp` `search_files` on the same prebuilt native gateway floors numeric `limit` `-9` at one match. Eleven global caches `n00` through `n10` with chunk text `needle` return only name `n00`. Name `n01` is absent. `searchedFiles` is `11`. The match has an empty `projectId`, kind `text`, chunk index `1`, snippet `needle`, and retrieval source `json_hybrid`. The seeded bytes stay unchanged. `.local-rag` stays absent. A missing token returns 401. Python `create_mcp_router`, `handle_mcp_message`, and `search_files` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收. The gateway is not shown to be a writer.
- [x] `POST /api/memory` on the same prebuilt native gateway enforces memory-store ownership. Unset mode: a missing token is 401, and bearer `add` of `likes dark mode` / `preference` is 409 `NATIVE_MEMORY_WRITE_NOT_OWNED` with `.memory` and `.workspace-generation` absent. `python_disabled`: the same add returns that content, type `preference`, scope `global`, and a 20-character `memoryId`. `GET /api/memory` lists that id. `.memory/memories.json` contains the content and `.workspace-generation` is `4`. A later non-owner `delete` is 409 and both files keep their bytes. Python `create_memory_router`, `add_memory`, `save_memories`, and `upsert_memory` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/reminders` on the same prebuilt native gateway enforces reminders-store ownership. Unset mode: a missing token is 401, and bearer `create` of title `喝水`, content `多喝水`, due `2026-01-01T00:00:00Z` is 409 `NATIVE_REMINDERS_WRITE_NOT_OWNED` with `.reminders` and `.workspace-generation` absent. `python_disabled`: the same create returns that title and content, due `2026-01-01T00:00:00+00:00`, `notified` false, and a 16-character lowercase hex id. Bearer `list` returns that id. `.reminders/reminders.json` contains the title and the id, and `.workspace-generation` is `2`. A later non-owner `delete` is 409 and both files keep their bytes. Python `reminder_action`, `create_reminder`, and `create_reminder_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/skills` on the same prebuilt native gateway enforces skills-store ownership. A missing token is 401 with the flat body `{"error":"Auth required","code":"unauthorized"}`. Unset mode: bearer `create` of skill id `custom`, name `学习`, version `1.0`, and prompt `Explain the topic` is 409 `NATIVE_SKILLS_WRITE_NOT_OWNED` and `.skills` stays absent. `python_disabled`: the same create returns that id, name, version, prompt, and `securityReview.reviewStatus` `local-custom`. `.skills/custom/custom.json` contains the prompt. Bearer `get` returns the same id, name, version, and prompt. A later non-owner `delete` is 409 and every file under `.skills` keeps its bytes. Python `create_skills_router`, `create_custom_skill`, and `delete_skill` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/reminders/due` on the same prebuilt native gateway enforces reminders-store ownership. The seed is id `abc`, title `喝水`, due `2000-01-01T00:00:00+00:00`, `notified` false. Unset mode: a missing token is 401, and the bearer poll is 409 `NATIVE_REMINDERS_WRITE_NOT_OWNED` with the seeded bytes unchanged and `.workspace-generation` absent. `python_disabled`: the poll returns that reminder with `notified` true and an integer `notifiedAt`, and the file contains `"notified": true`. A later non-owner poll is 409 and those bytes stay unchanged. Python `due_reminders`, `reminder_action`, `create_reminder`, and `create_reminder_tool` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/automation/templates/daily_project_summary` on the same prebuilt native gateway enforces automation-store ownership. Unset mode: a missing token is 401, and bearer `{}` is 409 `NATIVE_AUTOMATION_WRITE_NOT_OWNED` with `.automation`, `.workspace-generation`, and `.projects` absent. `python_disabled`: the same body returns `ok` true, `automationId` equal to `id`, prefix `auto_`, a 16-character lowercase hex suffix, name `Daily Project Summary`, trigger `schedule`, action `project_summary`, and an empty `projectId`. `GET /api/automation/{id}` returns that object. `.automation/automations.json` contains the id. `.projects` stays absent. A later non-owner `POST` of `{}` is 409 and those bytes stay unchanged. Python `create_from_template`, `create_automation`, and `create_automation_router` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `DELETE /api/automation/auto_keep` on the same prebuilt native gateway enforces automation-store ownership. The seed is id `auto_keep`, name `Keep`, action `save_item`, `createdAt` `stored-created`. Unset mode: a missing token is 401, and the bearer delete is 409 `NATIVE_AUTOMATION_WRITE_NOT_OWNED` with the seeded bytes unchanged. `python_disabled`: the delete returns `deleted` 1, the stored `automations` array is empty, and GET is 404 `Automation not found`. A later non-owner delete is 409 and those bytes stay unchanged. Python `delete_automation` and `create_automation_router` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `PATCH /api/automation/auto_keep` on the same prebuilt native gateway enforces automation-store ownership. The seed is the same `auto_keep` record. Unset mode: a missing token is 401, and bearer `{"name":"Renamed"}` is 409 `NATIVE_AUTOMATION_WRITE_NOT_OWNED` with the seeded bytes unchanged. `python_disabled`: the patch returns name `Renamed`, `createdAt` `stored-created`, `createdAtMs` `1700000000000`, action `save_item`, and an empty `projectId`. GET returns that object. The file contains `auto_keep` and `Renamed`, and it does not contain `Keep`. A later non-owner patch is 409 and those bytes stay unchanged. Python `update_automation` and `create_automation_router` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [x] `POST /api/automation/auto_keep/run` on the same prebuilt native gateway enforces automation-run ownership. Seed project `proj_ok` and automation `auto_keep` with action `save_item` title `Kept note`. Unset mode: a missing token is 401, and the 12:34 schedule post is 409 `NATIVE_AUTOMATION_WRITE_NOT_OWNED` with the automation file and `project.json` unchanged and with history and saved items absent. `python_disabled`: that post returns status `success` and logs `["saveItem"]`. One saved item has title `Kept note`, content `hello from automation`, type `assistant_answer`, and `savedId` `save_` plus 16 lowercase hex characters. History contains the `auto_run_` id. The automation file keeps the seeded bytes. `project.json` `updatedAt` is an integer greater than 10. A later non-owner post is 409 and those bytes stay unchanged. Python `run_once`, `save_item_action`, and `create_automation_router` are patched and are not entered. `pdftoppm` is not called. Offline: 1 passed, listed by `-rA`, and the run exited 0. Ruff and one-file mypy passed in that log. Cargo was not started. No epoch was installed. This is not 完成验收.
- [ ] Prove a second `POST /api/automation/auto_keep/run` on the same prebuilt native gateway does not save another item when the schedule is not due. After the 12:34 `success`, bearer cron `0 0 * * *` at `2026-10-04T15:00:00+00:00` returns `skipped` / `schedule_not_due`. The saved-item file and `project.json` keep the bytes from the successful run. The automation file keeps the seeded bytes. Do not call `pdftoppm`. Do not install an epoch.
- [ ] Re-run the public driver and the daemon restart probe on a reviewed
  snapshot. Vault reopen is not proven until the restarted worker, with the
  server key removed, still presents `request-only-credential`.
- [ ] Full product cutover remains open. Status stays **NOT_READY / 未完成**.
<!-- END CURRENT_NATIVE_CLOUD_CHECKPOINT -->


<!-- native-planner-worker-20261011 -->
The current isolated candidate adds authenticated typed planner execution and historical node queries to the production Rust worker. It reuses the native request policy, reads the immutable request body by its bound Rust artifact reference, checks the stored signed grant and installed epoch immediately before one bounded HTTP exchange, and persists received-result evidence before publishing a Rust-owned public-event reference. The planner path reads budget data without schema/pragma writes. It never turns an unknown effect into a fresh dispatch.

Local source/codegen checks and cloud qualification are separate. The new controlled HTTP process probes cover a confirmed response and an interrupted response, restart, exact HTTP action/epoch/node headers, immutable projection and one request across replay. Python only orchestrates isolated fixtures and seeds oracle inputs; it is not the application runtime. Live-provider qualification, Go proof consumption and phase/DAG settlement, all task roles/tools, the public Agent flow, full retained feature workloads, desktop/Android/worker distribution and zero-Python runtime evidence remain unfinished. Readiness remains NOT_READY; no production data or main-branch promotion is authorized.
<!-- /native-planner-worker-20261011 -->

## Rust node effect journal — 2026-10-11

- Prepared: signed-scope Rust-owned node effects, one fresh permit, immutable
  bounded canonical output, atomic v5 migration and fail-closed restart recovery.
- [ ] Qualify the fifteen new Rust tests and original complete coverage floors in cloud.
- [ ] Expose and exercise the actual worker execution RPC and provider/tool path.
- [ ] Sign/verify outcomes, settle Go phases and admit proven DAG dependencies.
- [ ] Complete public Agent workflows, platforms and all remaining native ownership.

The Android staging repair `a9799fa7` is qualified (37/37 CI); the latest Agent
control correction `fa274e77` awaits exact-head cloud gates. Neither reservations
nor this prepared journal prove a provider effect or full migration completion.

## Agent node dispatch continuation — 2026-10-11

- The exact `ca84511a` full CI is qualified (37/37 jobs); the Android staging
  follow-up `a9799fa7` is in cloud validation. Retain both revision-bound reports.
- Prepared: transactional single-fresh node reservations, authenticated typed
  begin/read RPCs, immutable-node/generic-completion guards, and Rust live-scope
  validation. Native execution tests and unchanged coverage floors await cloud CI.
- [ ] Wire the Rust-owned node effect journal and real provider/tool execution.
- [ ] Verify outcomes independently, admit dependencies, and settle Go phases.
- [ ] Complete public durable Agent workflows, all presets and restart/takeover.
- [ ] Finish retained native ownership, desktop/Android and zero-Python workloads.

No control reservation, imported event, static check or green legacy CI is full
Agent provider qualification or whole-product migration completion.

# 4.8.1 Todo — Native Runtime Contract Freeze

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

## 2026-10-11 exact cloud results and source-contract/package repair

The authorized twelve-file tree is uploaded as `e416cf59` / tree `1f200669`.
Rust run `38105173434` passes both required jobs: 1416 tests, one original Redis
ignore, all 15 crates, and 76311/93271 = 81.816427% line coverage at the unchanged
80% floor. The two semantic-hash regressions pass. The actual Go/Rust signing
process cases and the five-delegate layered sidecar semantic benchmark pass;
latency measurements remain informational under the existing workflow settings.

Full CI run `38105173957` finishes with 34 successful jobs and three failed
Python test jobs. All fourteen Go/Docker gates pass; default coverage is
10277/10816 = 95.016642% and Android coverage is 10103/10626 = 95.078110%.
Each Python version fails the same four source-contract/inventory cases while
reported coverage remains 95.51%-95.58%, above the original 95% floor. Existing
Evidence/RC jobs passing for this 4.8.0 workflow do not prove native 5.0 readiness.
Exact outcomes, report hashes and retained failures are in
`artifacts/native-20261011-full-ci-repair-preview/full-ci-evidence.json`.

The follow-up candidate derives Android test versions from VERSION, verifies
both platform SQLite connectors and the complete offline frontend/native build
chain, and regenerates the inventory's two missing Python-owned domains without
changing authority. Repeated Windows asset-publication refusals also require a
bounded same-move retry: persistent denial restores prior resources, and a
failed restore retains them for explicit recovery. The first Windows failures
and diagnostic results remain preserved; no permission or registry change occurs.
The five affected suites pass all 36 offline/source tests and mypy 2.4.0 passes
all 959 source files in Linux mode. This new source remains unqualified by cloud
CI until its reviewed snapshot receives specific upload authorization.

Rust, Go and Docker builds/caches stay in GitHub Actions, and only reports are
downloaded. The earlier authorized cache cleanup is complete with documented
bounded remainders; no further Docker start, broad prune or production-data
operation is part of this repair. Actual Agent node/provider/tool execution,
durable effect settlement/reconciliation, and complete platform/zero-Python
qualification remain required. Full migration and readiness stay **NOT_READY**.

## 2026-10-11 Go/Docker cloud execution and cache cleanup

The approved test-only expiry repair is uploaded as `9dd2ea89` / tree `a5dbe4d3`.
Exact run `38101509722` passes all fourteen Go/Docker jobs, including both
default/Android tests and races, Android-compatible SQLite and the actual
native worker process boundary. Default coverage is 10277/10816 = 95.016642%;
Android coverage is 10103/10626 = 95.078110%, at the original 95% floor.
Only 1,259,118 report bytes are downloaded. The exact source match and result
are recorded in `artifacts/native-20261011-go-docker-cloud-retry/cloud-evidence.json`.
Run `38098810886` and its failure logs remain historical evidence; six other
jobs in the new full CI run still fail and full release readiness is NOT_READY.

The authorized obsolete task-cache cleanup is executed. All 25 Docker volume
objects, 23 retained volume contents, 3574 source files and the real Git state
are preserved and verified. Offline supported VHDX compaction returned space:
observed C: +1.7 GiB and D: +73.6 GiB. Docker is stopped again. The audit receipt
is `artifacts/native-20261011-go-docker-cloud/cleanup-summary.json`.
34 approved task archives (18.3 MiB) remain after automatic review rejected the
bounded retry; 21 referenced build records remain while unrelated records are
preserved. No broader prune is authorized by this cleanup result.

Rust, Go and Docker compilation/tests stay in GitHub Actions; no compiler cache
or Docker image archive is downloaded. Full CI still requires Linux typing,
SDK/Hono audit, docs navigation and benchmark hash-order repairs. Agent node
execution, provider/tool effects, recovery and platform qualification remain
required; release readiness is **NOT_READY**.

## Production Go Agent authorization — 2026-10-10

- [x] Add the versioned Go authorization RPC using durable Go admission and the
  production TLS Rust custody client; independently recheck live scope before
  returning the signed grant. Focused positive/refusal/replay tests pass.
- [x] Update the real-process probe to require registered Go authorization,
  immutable replay before/after Rust restart, and terminal denial by both runtimes.
- [x] Add the required method to both Rust test service implementations, keeping
  explicit execution refusal; pinned Rustfmt passes without local compilation.
  Script lint, 959-file source typecheck and 15 boundary tests also pass.
- [x] Compile the updated process probe and pass all three actual Go producer
  preflight cases without a local Rust build.
- [x] Complete all ten default/native_android Go gates at the original 95%
  coverage floor (95.007396% / 95.068699%). Independently rehash the immutable
  snapshot and successful logs after interrupted aggregate finalization.
- [x] Freeze/review and upload exactly the sixteen explicitly authorized files as
  `92833d06` / tree `8b8a1318`; exact cloud run `38097512432` passes the production
  Go authorization RPC and all three real UTC Rust custody/TLS/restart/terminal
  denial cases. Complete Rust checks pass: 1414 tests / one original Redis ignore,
  all 15 crates and 76260/93234 = 81.794195% line coverage at the unchanged 80% floor.
  Verify all 806 native inputs and local HEAD/index; download only 4,562,578 report
  bytes. Compiler/dependency caches stay in the cloud; signing is not node-effect proof.
- [ ] Continue actual durable Agent phase/node/provider/tool execution and
  recovery. The new authorization primitive alone does not qualify those effects.

## Active work — 2026-10-10

- [x] Clear the seven user-authorized Rust compilation/dependency cache roots,
  record actual free-space growth and verify source/index/report preservation.
  Continue Rust compilation in the cloud; retain toolchains/SDK/runtime data.
- [x] Prepare a repository Go producer/process probe against actual real-UTC
  ControlRPC and lease stores; planning/tasks preflight passes with full process
  case code compiled. Private environment/public binding/TLS fixture tests pass.
- [x] Upload the authorized nine-file follow-up `7ca579c`; actual cloud plan/tasks
  signing passes with TLS, rebinding denial, forced restart and journal proof.
  Retain run `38044427626` as failed: its writer-expiry test used the old document
  deadline instead of the renewed Go writer deadline.
- [x] Correct only the two reviewed probe files, retain the exact terminal denial
  assertion and add a real UTC writer-renewal/expiry regression. All three Go
  producer cases pass. Explicitly authorized correction `4ffc3f2` is uploaded.
- [x] Qualify all three corrected production Rust custody/TLS/restart/writer-expiry
  cases at exact commit `4ffc3f2`, cloud run `38046474182`. Full Rust checks pass:
  1414 tests/one original Redis ignore and **76259/93231 = 81.795755%** line
  coverage at the unchanged 80% floor. Source context, receipt integrity and
  all 804 runtime files are verified. Download only 4,558,173 report bytes.
  This proves signing; Agent node/provider effects remain unfinished.
- [x] Complete Go `agent-grant-current-v2`: all 10 gates pass, including default
  and Android race. Exact coverage is 10205/10742 = 95.000931% and
  10031/10552 = 95.062547%; the 95% floor and frozen/current Go hashes are
  unchanged. Keep the final receipt; the owned container and tmpfs are released.
- [x] Add typed Rust Agent execution signing with an independently renewed Go
  claim, exact local epoch admission, deadline bounds and immutable retry bytes.
  Go verifies the distinct signature and immutable scope; 145 Worker tests,
  strict Worker Clippy and current signing regressions pass. Lease RPC fixtures
  do not qualify the actual Go producer or provider effects.
- [x] Qualify the current signing/disk-guard Rust inputs in the user-authorized
  cloud snapshot `291092e9210570416036d4a7292f53498a8572d8`: strict Clippy,
  1414 passing tests/one existing Redis ignore, all 15 crates, 1415 inventory
  entries and **81.797900%** line coverage at the unchanged 80% floor.
  Run `38034954077` passes; `rust-cloud-preview/cloud-evidence.json` records
  source bindings and small reports without downloading compiler caches.
- [x] Complete all three real-time production Go/Rust signer cases over TLS,
  including actual writer expiry while the action remains live. Current
  default/Android Go 95% gates already pass. Preserve each original input binding.
- [x] Exercise actual Go Agent claim/renewal with default real UTC time and
  authenticated loopback RPC in an isolated fresh store. Planning/tasks,
  replay, rebinding, cancellation and expiry of the writer while its action
  remains live pass (`agent-real-utc-probe-green-v4`). This is producer proof;
  Rust signing, TLS, artifact custody and provider effects remain unqualified.
- [x] Admit native Agent execution with a separate action fence, both live Go
  domain authorities, immutable request/plan binding and mandatory run reservation.
  Register typed ClaimExecution/RenewExecution on the actual authenticated service.
- [x] Reject lossy UTF-8 identities before creating a replay digest. Preserve raw
  caller input while defaulting a bounded lease; verify normal planning admission,
  cancellation/plan-change renewal denial, reconciliation takeover, transaction
  rollback, SQLite write failures and journal-scope/epoch replay refusal.
  Current focused Store/API regressions: `agent-execution-go-replay-scope-v17` PASS.
- [x] Finish the current complete default/Android Go gates. `full-v1` failed
  94.665665% coverage and `full-v2` failed 94.973222%; rounding to 95.0% is not a
  pass. `agent-execution-go-full-v3` is the new frozen-input batch. Keep the
  original 95% floor and qualify both race suites; preserve all failed receipts.
  All ten checks now pass, including both race suites: default **95.020201%**,
  Android **95.073185%**. All 374 Go/Proto inputs match; session 81813 is terminal
  and its owned container is removed without a cache archive (`full-v3.json`).
- [x] Qualify the current full Rust workspace after body/read and additive Proto
  changes: strict Clippy, 1409 passing tests/one original ignore, all 15 crates,
  1410 inventory entries and complete LCOV at **81.773383%**, original 80% floor.
  All 3673 input bytes remain unchanged; `agent-execution-rust-full-v3` passes.
  Preserve the two offline-oracle image failures; their repair changes only the
  verification environment. The final RAM container is removed, no cache archive.
- [x] Run the actual Go/Rust lease/public-read product probe only after
  the complete Go receipt passes and its Go/Proto hashes match the current inputs.
  Label v1 passes actual admission/replay/renewal/terminal refusal, body reopen,
  public detail/cursor/NDJSON and authentication. Its fresh-empty fixture uses
  clock 1000; provider effects and nonempty handoff remain unqualified. Both
  product containers are removed without an archive; session 11640 is terminal.
- [ ] Implement signed Rust effect authority and actual Agent node/provider/tool
  execution, durable dispatch/outcomes, heartbeat and uncertainty reconciliation.
  Execution claims and body/event reads alone do not qualify these paths.

## Previous Agent metadata qualification — 2026-10-09

- [x] Implement the actual Go Agent metadata event writer: transactional cursor,
  projected DAG, immutable history, replay receipt, and denial of generic bypass.
- [x] Register the authenticated typed Agent metadata gRPC service; validate
  metadata fence equality against the live domain epoch inside the write transaction.
- [x] Verify SQLite persistence/reopen, rollback, pagination, request replay,
  actual RPC authentication, shadow/writer/fence denial and malformed requests.
- [x] Match 62 original-reference histories at all 1000 checkpoints; retain
  long tasks and remove untouched nodes after plan edits.
- [x] Complete all ten default/Android Go gates for the preceding metadata input, including both race
  suites and original 95% coverage floors, on unchanged Go input `6830e490faeb`.
  Verify source receipts and owned-container/RAM cleanup (`agent-events-go-full-v2`).
- [x] Finish current full Rust qualification after the additive Agent Proto change
  and new body storage (`agent-execution-rust-full-v3`, 81.773383%, 1409/one ignore).
- [ ] Complete Rust event/request artifact custody, public Agent routes and
  real fenced task effects/reconciliation, nonempty legacy Agent migration and
  complete restart/resume/rerun/cancellation workflows. Artifact references and
  metadata-only successes do not qualify these workloads. **NOT_READY**.

Current Rust prefix parity correction preserves the first four distinct recognized
planner roles even after long invalid/duplicate prefixes. A real public-HTTP
regression first returns four fallback roles instead of the reference two; after
the production fix all ten real Go/Rust controlled-provider scenarios pass.
Current fmt and strict all-target/all-feature Clippy pass. Complete all-feature
tests pass **1394**, with one original ignored test. The first snapshot omitted
the gitignored real Vite bundle and failed `production_contract`; its receipt
remains. A genuine build from unchanged frontend inputs adds 110 files (3,336,573
bytes), and the original unmodified test then passes. Rust source bytes are
unchanged by this topology repair. Default intermediates occupy 3,669,757,952
RAM bytes. After sharing the cfg(test)-only queue lock between Hub and HTTP
router tests, all 198 Gateway unit tests and current strict Clippy pass. The
original complete 80% coverage gate passes **74329/91151 (81.544909%)**,
with all 15 crates, 1395 inventory entries, 1394 passing tests and one original
ignored test, and complete LCOV. Input `7f89c13480e1` changes only test
synchronization from the preceding snapshot; every frozen fixture and test
assertion is unchanged. The 12 GiB RAM disk/14 GiB memory/zero-swap verification
container is removed, with its 9,221,550,080 bytes of intermediates/source copy
released and no cache archive. The initial 10 GiB capacity refusal and subsequent
A2A concurrency failure remain recorded. Proof:
`artifacts/native-20261009-agent-prefix-qualify-v3.json` and its coverage/cleanup
receipts.
Proof: `artifacts/native-20261009-agent-prefix-full-test-ui.json` and
`native-20261009-agent-prefix-freeze-v3.json`. No remote provider, durable Agent
product, zero-Python production, device or exact-head release proof is claimed.


- [x] Retain Agent metadata approval/orphan/resume/rerun states and snapshot
  updates with durable retry/reopen tests and no execution-epoch escalation.
  Two Go regressions and the complete Windows Store suite pass. Connect the
  real Python Agent writer and enabled auto-resume to existing ownership denial
  before filesystem/worker mutation; 54 writer/execution-entry/reference tests,
  Ruff and local Mypy pass. Reads and reference modes retain their behavior.
  Review: `artifacts/native-20261009-agent-durable-control-review.md`.
- [x] Finish the current 349-file Go complete default/Android test/race/95%
  qualification using executable RAM. Format/modules/default vet/test/race and
  coverage **9587/10076 (95.146884%)** pass; Android vet/test/race and coverage
  **9413/9886 (95.215456%)** pass. The verification container is removed and its
  991,043,584-byte RAM cache released. Preserve the initial logging-argument
  failure. Android-tag validation does not qualify an APK or device.
- [x] Finish per-build compiler/linker scratch, registry/download and Cargo
  metadata capacity protection. Disable implicit toolchain installation in child
  environments; explicitly opted-in installation is monitored throughout metadata
  and compilation. All 64 related regressions pass without skips, plus Ruff/Mypy.
  Full workspace fmt/check/strict Clippy and two all-feature test runs pass:
  1394 passed/one original ignored in each run; repeat reuses all 599 artifacts.
  RAM cache stays at 7951 files/4,176,035,014 logical bytes, with no scratch
  directories left. Approved exact-list cleanup removes 1964 dependency files,
  recovers 631,775,232 bytes of D: free space and preserves all 55 sibling hashes.
  Evidence: `artifacts/native-20261009-rust-final-policy-evidence.json`. Preserve
  initial test-fixture failures; production inputs are unchanged by their repair.
- [ ] Complete retained interactive multi-agent chat and the actual browser's
  durable `/api/agent-runs` flow. The new authenticated Go gRPC scheduler has
  real-socket scheduling/auth tests. The 103-case pure reference fixture passes;
  eight actual Go-daemon/Rust-HTTP controlled-provider scenarios pass: normal,
  revision, retry, role denial, shadow refusal, bearer refusal, positive budget
  exhaustion and zero/unlimited budget. Pure scheduling does not grant durable
  mutation authority. Require remaining reference
  compatibility, real Rust/Go/provider execution, role denial, retry/cancellation,
  durable unique-writer state, restart/recovery and browser acceptance before
  qualifying this domain. Current Go full/race/95% coverage, current Rust 80%
  coverage, durable browser flow and whole-product acceptance remain open.

- [x] Wire native cascade draft/gate/judge/refine execution and final-result
  stream replay, including vision bypass, environment overrides, actual Ollama
  responses, failure masking, disconnect and zero-tool judge finalization.
  Current Rust input `44c20f629c64` passes 1376 default tests, workspace fmt and
  strict all-target/all-feature Clippy; all 30 Windows router tests pass.
- [x] Collect the original complete all-feature coverage, LCOV and inventory
  for current input `8548646e6de8`: **73837/90043 lines (82.001932%)**, 1393
  passed and one original ignored test, complete 1394-test inventory, no core
  exclusions, genuine dirty-source Git metadata and strict current Clippy.
  Production bytes match default-qualified `44c20f629c64`; the only delta fixes
  a clock-sensitive test assertion. Preserve its failed coverage run and the
  passing corrected Windows case. Source and original index are unchanged.
  Consolidated evidence: `artifacts/native-20261009-chat-cascade-evidence.json`.
- [x] Return ordinary `/api/chat` JSON for non-streaming requests and retain
  shared tool-round/search behavior. Full gateway 371-test pass; 16 router
  tests cover JSON shape, prefetch/tool rounds and request isolation.
- [x] Reduce native build disk growth: default symbol stripping, explicit
  diagnostic/coverage profiles, fixed caches, Android output/cache separation
  and capacity protection in all four native build entry points. Five exact
  task caches recover 5.85 GB without deleting or changing file contents.
  Two actual Windows builds reuse all 420 artifacts without cache growth.
  Forty-four build/coverage/contract tests pass with no exclusions; original
  coverage floor and complete test inventory remain enforced.
- [x] Qualify the final coverage-map input `3a38ddc2daaf` with full
  Rust fmt/strict Clippy/default tests and the original all-feature 80% gate.
  Earlier inputs `20364b1c6e9b` and `8b4f02e1fc9b` pass 1360 tests; their
  full-type/line-table coverage attempts hit the RAM reserve. Preserve both
  failures and the frozen-Git diagnosis. Final default gates pass; measurement
  passes 1377 all-feature tests with one ignored. The remaining formal export
  passes at 73452/89655 lines (81.927388%) with the complete 1378-test inventory,
  LCOV and true dirty-source metadata. Uses 10 GiB RAM, original capacity and
  coverage thresholds, and an independently verified RAM Git view. Original
  index and all 3640 frozen-source bytes remain unchanged. Consolidated local
  evidence: `artifacts/native-20261009-rust-disk-policy.json`.

## Previous qualified work — 2026-10-08

- [x] Complete original current-source Go/Rust full gates. Stable 343-file GUI
  Go v5 passes all ten gates, including both race suites; default coverage is
  95.083945% and Android 95.152198% at the original 95% floor. Frozen 415-file
  Rust search-prefetch input `f308ae998b47` passes workspace fmt, strict
  all-target/all-feature Clippy, 1355 default tests and original all-feature
  coverage 81.898729% at the unchanged 80% floor. Preserve earlier failed runs.
- [x] Implement and qualify forced-search prefetch at the router/provider/tool
  boundary: live prefetch progress, hardened result context, cache, seeded
  memo/budget/citations, failure context and disconnect before model dispatch.
  Thirteen focused tests pass, including 22 offline browser-projection oracle
  cases; the full current-source Rust gates above bind these same bytes.
- [x] Complete original Windows real-S3/recovery gates on frozen 415-file inputs.
  The race loader preflight is repaired with test-only synchronization-library
  linkage, without changing production CGO-disabled Go. Fresh five-MinIO gates
  pass: seven storage tests, ten worker tests and the actual Go race integration,
  including versioned replay and six lost-response/takeover cases. Receipt:
  `native-20261008-provider-current-windows.json`. Fixture data/logs are retained;
  actual remote-provider and whole-product recovery acceptance remain open.

- [x] Implement and locally qualify native CLI/mobile options, masked prompt,
  real control readiness, LAN URL guidance and actual headless/mobile HTTP
  workloads. Both full Go variants pass all ten original gates with the 95%
  floor; Windows checks and three source/symbol security scans pass. Rust fmt,
  strict Clippy/default workspace tests and original 80% coverage pass.
- [ ] Qualify actual default browser/Termux/device opening, retained mobile
  recovery and all platform packaging; existing receipts qualify the named slices.
- [ ] Complete the configuration GUI and ownership-controlled credential workflow.
  `--gui` now starts the private configuration page before the backend. The first
  frozen Windows candidate passes actual save/start/stop/reload/restart and owned
  HTTP message transport; the user screenshot proves the conversation entry.
  Frontend full check passes 613 tests. DPAPI/AES-GCM and exclusive settings
  locks are implemented. Corrupt ciphertext is retained until explicit recovery;
  actual Windows/Linux settings-owner hard-kill/reopen probes pass. Both HTTP
  and native-window stale-close regressions pass after atomic controller fencing.
  Focused launchgui coverage is 97.7%; GTK confirmation/callback boundary tests
  pass, with actual modal cancellation still open. The stable 343-file Go v4
  source builds three fresh Windows binaries on C: and passes three source-security
  variants. Matching pinned Rust Windows binaries build offline on C:, and the
  frozen v4 HTTP/DPAPI/native-process integration passes 34 real checks including
  nonempty model/file transport, restarts and explicit corrupt-settings recovery.
  The native window/launcher entry and browser message are not exercised by this
  harness. Complete v2 default coverage remains a genuine 94.979374% failure;
  v3/v4 full runs fail under D: disk I/O, and v4 additionally exposes `/tmp`
  `noexec`. Keep the original 95% floor and all failed receipts. Controlled
  legacy publication/migration, all-platform close/recovery, actual window/launcher
  workloads and fresh Android/package bindings remain open. Original full Go
  gates now pass at the original 95% floor on the unchanged 343-file inputs.
- [x] Fix and qualify async-runtime Rust browser-sidecar startup with an actual
  gRPC service regression and pinned fmt/strict Clippy/default workspace tests
  (1349 passes, 123 suites, one ignored) on the complete topology.
- [x] Complete current Rust 80% coverage after storage recovery. Fresh executable
  RAM target passes all-feature coverage at 73301/89502 (81.898729%) on current
  415-file inputs. Earlier disk/container I/O failures retain their null metrics.
- [ ] Recover the local validation environment without deleting runtime data.
  The explicitly authorised Docker restart recovered health before D: filled
  again. The authorised exact 1964-file task cache deletion was rejected by
  automatic approval even after authorisation; no deletion occurred. A reviewed
  human-only script and pending manual result are recorded in `continuation.md`.
  Docker now responds and full Go/Rust verification runs with caches/logs on C:
  and executable `/tmp`. D: headroom remains limited; no manual-removal receipt
  has been observed. Preserve all failed attempts and runtime volumes/data.

## Local integration — 2026-10-07

- [x] Apply the explicitly authorised local merge: HEAD `37efd8e9`, reviewed tree
  `678ce430`; preserve all 74 original task files. No push or production operation.
- [x] Repair stale Android asset staging; inspect corrected APK `32b35b1cc71b`;
  retain UID/token/all 21 store files across upgrade, and read the old business
  run/artifact. Three actual instrumentation tests and the full 26-check app
  workload pass for its dirty source `c593437df2d7`.
- [x] Qualify typed Android image/raster-PDF OCR APK `bde81447a61a` (dirty source
  `ab9d9f0b5616`): seven device tests, 26 recovery/UI checks, 17 file checks,
  ten EXIF/public-disconnect lifecycle checks, and actual browser upload/preview/
  byte-identical download. Blank pages and temporary-file recovery are proved.
- [x] Repair instrumented worker test profile leakage; complete Linux coverage
  at 81.454954% with unchanged inputs, 1361 passes and the original 80% floor.
- [x] Qualify clean local commit `71b05a62` APK `cf00d5ca167a`: preserve all
  44 upgrade files; rerun seven device tests, 26 recovery/UI checks, 17 file
  checks, ten lifecycle checks and actual browser text/render/source downloads.
- [x] Implement and locally qualify the actual Windows WebView2 host: retained
  token, nonempty native React upload/read/source workload and hard-kill cleanup
  of the default launcher's desktop, WebView2 and backend process tree.
- [x] Rebuild clean local commit `ff3fab80`; qualify Windows SDK workload, rapid
  natural-lease restart, owned-tree cleanup and zero-finding pinned security scan.
- [x] Qualify separate 299-file Linux guardian/GTK draft with all ten full Go
  gates and unchanged default/Android 95% coverage floors. Windows shared
  preparation build/vet/test, seven real SDK and twelve restart checks also
  pass, with stable source bytes.
- [x] Qualify the bundled Python/Node-free Linux consumer: default Chinese React
  rendering, actual native mouse/keyboard/system chooser upload, text preview,
  original view and byte-identical 129-byte download; retain token and REST/UI
  originals across same-container restart. Bind image/bundle/source/screenshots.
  Preserve earlier blank tools-image controls and correct HTTP counter labels.
- [x] Integrate the reviewed Linux slice with raw-byte backups, canonical Go
  equality and local guards, Windows build/vet/test, Linux boundary race and
  zero-finding pinned Linux security scan; record the local commit/content binding.
- [ ] Qualify permissions, all retained flows and platform packages. Investigate the earlier
  tools-image display difference without changing production acceleration.
- [ ] Complete the native desktop window and configuration/mobile launch flows
  on all retained platforms, with actual workload and close/failure recovery.
- [ ] Qualify exact-head CI/Evidence; retain shadow/control ownership, ARM64,
  formula/media and whole-product gates.

The resolved source retains the clean primary `707a0528` and the isolated task's
Android, browser authentication, Skills parity and store fencing work. Both Go
race/vet/95% coverage variants pass; the actual Rust HTTP comparisons pass
133 read, 49 mutation/recovery and 20 run cases. React checks and actual browser
create/edit/disable/reload pass. The new Android OCR APK passes its measured
local device/browser workloads. Clean commit `71b05a62` now has a freshly
qualified source-bound APK; exact-head CI remains open. Online Skill, Android
formula/media and ARM64 qualification remain open. Full proof scope, source hashes,
unchanged thresholds and retained pre-integration APK are in
[`continuation.md`](continuation.md#integration-preview--2026-10-07).

The preceding comparisons qualify the retained pre-merge review source. Local
integration is now applied in the isolated Git worktree; the clean `D:/deepseek`
checkout remains unchanged at `707a0528`. Whole-product source/package
qualification and exact-head CI remain required. Status remains **未完成 / NOT_READY**;
retain every whole-product gate. Current evidence is recorded at the top of
[`continuation.md`](continuation.md).

### Retained primary migration record


Status: all Phase 0-3 items are checked; Phase 4's two release gates (exact-head CI /
Evidence Assembly, qualification) remain open and no 4.8.1 release has been cut —
`VERSION` is `4.8.0`. Live slice work continues in
[`continuation.md`](continuation.md) under the 5.0 native milestone; the unified tracker
is [`migration-matrix.md`](migration-matrix.md). This file is the 4.8.1 contract-freeze
record; the active section below records current slices without rewriting that history.

## 5.0 active execution — 2026-10-06

- [x] Close the preceding CI repairs on exact HEAD `3406f7ad`: all 37 jobs pass,
  including Rust/Go coverage, race and Evidence Assembly. No native readiness flip.
- [x] Generate and retain an independent control signer inside Rust, encrypted
  with the frozen custody envelope; add a native initializer and typed scoped RPC.
- [x] Integrate default authoritative Go storage execution and renewable claims
  with Rust epoch/grant issuance after durable claim, with TLS, public binding,
  exact intent and post-signing ownership/lease checks.
- [x] Persist immutable Rust signature request/nonce receipts; deny changed replay,
  missing custody, wrong caller/scope, stale epochs and foreign/symlink journals.
- [x] Qualify native custody against three real MinIO providers, including a
  versioned provider, Unicode object bytes, force-kill/restart and stable receipts.
  The administrative promotion fixture and leased `VERIFYING` limit stay explicit.
- [x] Fix Go/Python/Rust canonical signing bytes for HTML/Unicode separators;
  retain all frozen fixtures and the unchanged full verification contracts.
- [x] Qualify custody checkpoint `005e5bb6` on exact-head CI: all 37 jobs,
  including Evidence Assembly, release-package and RC checks, pass.
- [x] Qualify six actual provider response-loss/lease-takeover scenarios under
  Go race. Retain original dispatch epoch 1 under successor epoch 2; a real 404
  stays unknown until a delayed original PUT is byte-verified. No repeated PUT;
  another worker death preserves receipt/object version. Proof settlement stays open.
- [ ] Complete production Fleet signer custody/rotation and leased proof settlement.
- [ ] Finish remaining product/platform/domain transfers and release qualification;
  the full migration remains **未完成 / NOT_READY**.

## Previous active execution — 2026-10-04

- [x] Fix public mirror body guards and Python text coercion: native 7 mirror +
  2 auth tests and Python HTTP oracle 12 tests pass; retain the red empty-body case.
- [x] Fix relative launcher binary/data/static paths; native child listener tests
  verify sibling cancellation after normal/failing exit. Launch tests pass at 96.5%.
- [x] Validate real control RPC health, missing policy state, duplicate credentials
  and the 1 MiB body bound. Restore 5515 pinned SDK files with exact SHA-256 matches.
- [x] Restore current whole Go coverage to 95.020796% without lowering 95%.
  Go format/vet and launch/API/lifecycle race pass (155 tests, no skips/races).
- [x] Repair whole Rust check/fmt/all-target/all-feature clippy and coverage crate
  inventory (actual fifteen from locked Cargo metadata, 19 evidence tests).
- [x] Reproduce and fix Rust/Python mirror writes during the restore lock window:
  hold the OS lock, recheck the fence and preserve successful recovery uploads.
- [x] Complete whole-Go Linux race: 1768 passes, no failures/races, one intentional
  fixture-generator skip; snapshot-qualified evidence, no Go changes since it.
- [x] Implement persistent mirror source fencing, byte inventory export and an
  independent Rust candidate importer. Verify real Age reads and force-kill
  recovery after a nonzero copy; imported stores remain denied pending admission.
- [x] Run the real Redis gate, reproduce error-reply poisoning and repair both
  clients; six parser/network tests and the real Lua/fence release test pass.
- [x] Refresh whole Rust tests/clippy and fifteen-crate coverage: 1334 passing
  executed tests, one explicit provider ignore, 80.732473% on snapshot `33acecda`.
  Exact-head release evidence is not established; investigate Git metadata.
- [x] Revoke a settled mirror candidate through the real native CLI; independently
  reattest and journal pre-admission handback, then prove original writer progress
  and persistent native/candidate denial with real Age data.
- [x] Serialize import/revocation/handback with the target workspace restore lock;
  refuse any invalid restore fence and deny Python writes to native candidates.
- [x] Move twelve production Redis Lua scripts into Rust, preserve the unchanged
  TypeScript oracle bytes and rerun the real Redis acceptance test.
- [x] Diagnose the ten full-Python failures without changing frozen fixtures;
  repair build inputs and current source-contract checks; 61 targeted tests pass.
- [x] Reproduce loss of Go control leaving the real candidate image healthy;
  use the Go supervisor, forward native trust configuration and handle SIGTERM.
- [x] Rebuild the pinned supervisor image; prove native persistent writes,
  sibling cancellation, live-writer restart denial, lease-expiry recovery and SIGTERM.
- [x] Fix actual private Compose channels and namespace-owner restart recovery;
  v1 RPC, credential/ownership refusal and native state preservation pass.
- [x] Refresh Go coverage to 95.030835%, vet, Ruff and 949-file mypy.
- [x] Complete current store race with its measured thirty-minute package bound;
  other whole-run packages passed, the default ten-minute store run timed out.
- [x] Rerun the complete Python 95% gate on the final frozen source; the resumed
  5629-pass/two-failure run at 95.574917% remains failed evidence; both failures
  are repaired and 32 targeted cases pass. The final `70b29cf9` input passes
  **5631 tests / 95.589801%**; session 27119 is collected. Git status failures reject evidence.
  Do not reuse older source PASS values.
- [x] Wire default worker S3 transport through dedicated production configuration;
  preserve raw supervisor values and isolate the worker state volume. Default
  Linux binary passes ten actual-provider tests including TLS, forced restart,
  exact completed receipt replay and a versioned bucket on three providers.
- [x] Complete Windows default-binary three-provider verification: ten real tests
  pass after isolating executable-location-dependent access denial; no ACL/security changes.
- [x] Rerun the combined Rust-byte/Go-promoted-control provider runner: seven
  storage cases, ten worker cases and three Go provider subcases pass on actual
  MinIO with the emitted default-binary hash and pinned Cargo/Go versions.
  Signing is still an isolated test fixture; production Rust custody is open.
- [x] Push checkpoint `7fe490d6`; run exact-head CI and preserve the six upstream
  failures. Repair oracle setup, versioned-read expectations, isolated MCP/native
  fixture and hybrid reference images, and default-worker TLS authority refusal.
  Local MCP/source/runtime guards and the Windows TLS process pass.
- [x] Collect repair checkpoint `181dbdf6` CI: 31 jobs pass, including native Go,
  MCP failover and hybrid. Repair missing Vite preparation in both Rust jobs and
  Go dependency downloads before S3 proxy isolation. The actual Linux production
  contract and ten workflow guards pass; coverage floors remain unchanged.
- [x] Obtain complete exact-head CI/Evidence for the prerequisite repairs on
  `3406f7ad`: all 37 jobs pass; earlier failed runs remain historical evidence.
- [x] Refresh complete Rust coverage after the `python_disabled` packaging and
  inventory assertion repairs: the unchanged 80% gate passes in that exact-head CI.
- [ ] Close mirror malformed-JSON/integer parity, browser upload, native handback,
  full restore consumer and Go action/epoch ownership and write admission.
- [ ] Finish all outstanding product/platform/provider/zero-Python and exact-head
  CI/Evidence gates. The whole migration is **未完成**, readiness `NOT_READY`.

- [x] Serve `GET /api/workspace/home` on the Rust production router as
  `workspace_home.workspace_home`. The body matches a live Python call on the
  same root, including the truncated `counts.automationRuns` window. `"0"` is
  8 because `int(0 or 8)` is 8. A non-integer is 500. Missing auth is the
  nested 401. `HEAD` is empty 200. Other methods are 405 with
  `Allow: GET, HEAD`. An empty root stays empty. Not a domain flip.
  Inventory python-only routes 106 → 105 (`workspace.py` 28).
  Evidence: `cargo test -p deepseek-gateway --offline --test data_routes -- --test-threads=1`
  (31 passed) and `pytest tests/test_production_runtime_inventory.py`.
- [x] Replace the `501` on `POST /api/workspace/projects/{project_id}/artifacts`
  with `register_artifact`, and serve `PATCH` and `DELETE` of
  `.../artifacts/{artifact_id}` together. A truthy `path` appends a version.
  A legal create, title update, version append, and delete match the Python
  store only when `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise 409 and
  the files stay unchanged. The 500-row window matches the oracle. Not a
  domain flip. Inventory python-only routes 108 → 106 (`workspace.py` 29).
  Evidence: `cargo test -p deepseek-gateway --test data_routes -- project_`
  (7 passed) and `pytest tests/test_production_runtime_inventory.py`.
- [x] Replace the `501` on `POST /api/workspace/projects/{project_id}/saved-items`
  with `create_saved_item`, and serve `PATCH` and `DELETE` of
  `.../saved-items/{saved_id}` together. A legal create, rename, and delete
  match the Python store only when `DEEPSEEK_RUNTIME_MODE=python_disabled`.
  Otherwise 409 and the files stay unchanged. The 1000-row window matches the
  oracle. Not a domain flip. Inventory python-only routes 110 → 108
  (`workspace.py` 31).
  Evidence: `cargo test -p deepseek-gateway --test data_routes -- project_`
  (6 passed) and `pytest tests/test_production_runtime_inventory.py`.
- [x] Serve `POST /api/automation/{automation_id}/run` on the Rust production
  router as `runner.run_once`. A legal `save_item` run persists history, the
  saved item, the project touch, and one memory summary only when
  `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise 409 and those files stay
  unchanged. Missing automations are 404 with no history file. Invalid `now`
  is 400 before lookup. Empty or missing `Content-Length` is `{}`. This is
  not a domain flip and not full action parity: `traceId` is empty, and
  actions other than `save_item` that pass policy record a failed run.
  Inventory python-only routes 111 → 110 (`automation.py` 2).
  Evidence: `cargo test -p deepseek-gateway --test automation_run_routes`
  (3 passed) with the definition and template tests, plus
  `pytest tests/test_production_runtime_inventory.py`.
- [x] Serve `PATCH` and `DELETE /api/automation/{automation_id}` on the Rust
  production router. A legal rename and delete match `update_automation` /
  `delete_automation` only when `DEEPSEEK_RUNTIME_MODE=python_disabled`.
  Otherwise 409 and the file is unchanged. Project touch follows a successful
  write. The 500-record window matches the oracle. Not a domain flip.
  Evidence: `cargo test -p deepseek-gateway --test automation_definition_routes`
  (2 passed).
- [x] Serve `POST /api/automation/templates/{template_id}` on the Rust
  production router. A legal create matches `create_from_template` only when
  `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise 409 and the file is
  unchanged. Unknown templates are 404 before that gate. Project touch follows
  a successful write. Duplicate and 500-visible cap refusals do not rewrite.
  Not a domain flip. Inventory python-only routes 112 → 111 (`automation.py` 3).
  Evidence: `cargo test -p deepseek-gateway --test automation_template_routes`
  (2 passed) and `pytest tests/test_production_runtime_inventory.py`.

## 5.0 active execution — 2026-10-03

- [x] Serve backup retirement create/get/list on the Rust production router.
  Success path writes `.backup-retirements/retirements.sqlite3` only when
  `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise 409 and no directory.
  GC/marker execution is not ported. Not a domain flip.
  Evidence: `cargo test -p deepseek-gateway --test backup_retirement_routes`.
- [x] Serve `GET /api/workspace/backup-runs` as a read-only projection of
  `.backup-scheduler/scheduler.db`. Missing file stays missing. Inventory
  python-only routes 129 → 125 (`backup_governance.py` 65).
  Evidence: `cargo test -p deepseek-gateway --test backup_run_routes`.
- [x] Serve `GET /api/workspace/disaster-recovery/replication` as a read-only
  listing of `.backup-replication/*.json` (limit 100). A missing directory
  stays missing.
  Evidence: `cargo test -p deepseek-gateway --test backup_replication_routes`.
- [x] Serve `GET /api/workspace/disaster-recovery/drills/{restore_id}` as a
  read of `drill-result.json` or `drill-running.json`. A missing session stays
  missing. `POST .../drills/run` is still proxied and still Python-only.
  Inventory python-only routes 125 → 123 (`backup_governance.py` 63).
  Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test backup_drill_routes`.
- [x] Leave `GET /api/workspace/resilience/journal` on Python.
  `list_actions` opens the journal through `_connect`, which creates
  `.resilience-journal`, sets WAL, and migrates the schema.
- [x] Serve `GET /api/automation/templates` from the in-memory builtin catalog.
  The response matches `registry.list_templates()`. `.automation` is not
  created. Inventory python-only routes 123 → 122 (`automation.py` 8).
  Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test automation_template_routes`.
- [x] Serve `GET /api/automation/{automation_id}/runs` as a read of
  `.automation/history.json`. Missing file stays missing. `limit=0` is 100;
  a negative limit returns every loaded run. Inventory python-only routes
  122 → 121 (`automation.py` 7). Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test automation_run_routes`.
- [x] Serve `GET /api/automation/{automation_id}` as a read of
  `.automation/automations.json`. Missing file is 404 and stays missing.
  PATCH and DELETE on that path are forwarded to the Go proxy, not
  implemented. The path-level scanner dropped them from the Python-only
  list anyway. Inventory output 121 → 118 (`automation.py` 4). Authority
  stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test automation_definition_routes`.
- [x] Serve `GET /api/media/{media_id}/segments` as a read of `.media`.
  Missing library is 404 and stays missing. Missing segments file is an
  empty list. Other methods on that path are 405. The collection and item
  media routes stay Python. Inventory output 118 → 117 (`media.py` 6).
  Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test media_segment_routes`.
- [x] Serve `GET /api/workspace/resilience/federation` from
  `build_federation_snapshot`. No store is created. A blank `fleetId` is
  `local`. Whitespace-only is 500. Inventory output 117 → 116
  (`backup_governance.py` 62). Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test resilience_federation_routes`.
- [x] Serve `GET /api/workspace/artifacts/{artifact_id}/preview` as a read of
  `.projects/<id>/artifacts.json` plus the artifact file. A missing store
  stays missing. An empty `projectId` scans existing `project.json` rows only.
  `GET .../download` stays on the Go proxy. Inventory output 116 → 115
  (`workspace.py` 33). Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test artifact_preview_routes`.
- [x] Serve `GET /api/scheduler` as a read of the fresh in-process snapshot
  and, when the file already exists, `.scheduler/scheduler.sqlite3`. A missing
  file stays missing. Inventory output 115 → 114 (`status.py` 3). Authority
  stays `python`. Taking a lease is not this slice.
  Evidence: `cargo test -p deepseek-gateway --test scheduler_routes`.
- [x] Serve `GET /api/edge/status` from `edge_inference_status()` with an empty
  payload. No store is created. Query strings are ignored. Inventory output
  114 → 113 (`status.py` 2). Authority stays `python`. Loading a model is not
  this slice.
  Evidence: `cargo test -p deepseek-gateway --test edge_status_routes`.
- [x] Serve `GET /api/rust/status` from `rust_status()`. A disabled gateway
  does not open a socket. An enabled gateway checks `GET /healthz` and is
  healthy only on HTTP 200. Inventory output 113 → 112 (`status.py` 1).
  Authority stays `python`.
  Evidence: `cargo test -p deepseek-gateway --test rust_status_routes`.
- [ ] Port the remaining 62 `backup_governance.py` routes and the other
  Python-only public routes, without 501 placeholders or a second writer.
  105 Python-only routes remain. `status.py` still has
  `GET /api/semantic-cache/status`, which creates `.semantic-cache`.
  `automation.py` still has only `GET` and `POST /api/automation` (one shared
  path; POST is the full action dispatcher: create, run, rerun, simulate,
  run_due). The six `/api/media` methods are still open. Artifact download is
  still open. Scheduler admission and DLQ writes are still open.
  `GET /api/workspace/home` is **集成通过** and is not a domain flip.
  `GET /api/workspace/projects/{project_id}/provenance` is the next public route.
- [ ] Complete every outstanding product/platform/provider/zero-Python and exact-head
  CI/Evidence gate. The product remains **未完成**.

## 5.0 active execution — 2026-10-02

- [x] Replace mirror recipients with the exact typed `control/v1` read RPC on the
  existing Go listener; preserve frozen descriptor JSON; pass pinned codegen
  drift/contract checks and 30 contract/mechanical-denial cases.
- [x] Pass four real Rust/Go process tests: policy reads/full CRUD, target health
  and actual encrypted mirror upload/replay/recovery. Wait for the recorded lease,
  advance the successor fence, refuse lost-RPC GET/PUT without filesystem effects.
- [x] Linux Rust check/build/clippy all targets/features, mypy 929 files, frontend
  typecheck/609 tests in 73 files/build/bundle.
- [x] Reproduce corrupt recovery-fence publication, preserve the red regression,
  and fix the swallowed error to retain the 423 refusal.
- [ ] Re-run that regression and whole Rust 80%/Go 95% coverage/race gates after
  the local Docker I/O failure. Interrupted checks are not PASS.
- [ ] Observe current policy CRUD in a real browser; extend mirror body coercion
  parity and implement fenced transfer, handback, restore and write admission.
- [ ] Complete every outstanding product/platform/provider/zero-Python and exact-head
  CI/Evidence gate in the full matrix. The product remains **未完成**.

## Historical execution — 2026-10-01 (policy CRUD over the process boundary)

The 4.8.1 list below remains a historical contract-freeze record. Current
work is tracked against the full product matrix, with 5.0 still **未完成**.

- [x] Add the **terminal tombstone state** so a policy delete is possible without
  removing a row whose immutable events would orphan it. `TombstoneState` (`DELETED`,
  `go/internal/store/schema.go`) is honoured as absent by `Control.Get` and
  `ListAuthoritativeRecords`, is terminal (no transition leaves it) and keeps the
  document for audit while advancing the store-owned revision. `DELETE
  /api/workspace/backup-policies/{id}` therefore answers the oracle's own
  `{"deleted": true, "policyId": …}`, and a second delete or a patch after the delete is
  the oracle's `404 Backup policy not found`. The `501 GO_POLICY_DELETE_UNSUPPORTED`
  placeholder and its constant are gone; the frontend's delete flow is servable.
  Verified: `go/internal/store/operator_mutation_test.go`
  (`TestTombstoneIsTerminalAndHiddenFromTheInventory`), `go/internal/store/authoritative_list.go`,
  `go/internal/api/backup_policy_writes_test.go` (`TestPolicyDeleteTombstonesTheRecord`) —
  `go test ./internal/store -run 'Tombstone|OperatorMutation|AuthoritativeList'` and
  `go test ./internal/api -run 'Policy|Backup'` pass.
- [x] Drive the whole policy CRUD over the **real process boundary**:
  `TestBackupPolicyRustGatewayToGoPolicyCrud`
  (`go/internal/api/backup_policy_process_integration_test.go`, `native_integration`) starts a
  real `deepseekd` and a real Rust gateway over one isolated Go-authoritative store, and
  asserts create → projected read → merge-and-advance update → tombstoning delete → terminal
  404 for both write verbs → a create refused by name for a foreign revision → Host/token
  refusals on the write verbs. The shared harness (`startNativeProcesses`) now serves the
  read, CRUD and target-health tests, reports both child logs on a transport failure, and
  takes the deployment write opt-in explicitly (`DEEPSEEKD_CONTROL_AUTHORITY` +
  `DEEPSEEKD_INTERNAL_BEARER`) because an operator write needs that opt-in *and* a durably
  Go-authoritative domain.
- [x] Two defects the slice exposed were fixed rather than worked around. (1) The harness
  cancelled its own child processes: `defer cancel()` inside the old single-function helper
  fired when the new split `startNativeProcesses` returned. (2) The Rust edge checked the
  original `Host` only on the **collection** paths, so `PATCH`/`DELETE
  /api/workspace/backup-policies/{id}` skipped it — and the Go-side check cannot catch that,
  because the proxy rewrites `Host` to the Go listener. `backup_inventory_route` now covers
  the item paths too, which is what the oracle's `require_api_auth` does.
- [ ] The Rust half above is **written and formatted, not locally linked**: `cargo test -p
  deepseek-gateway` fails at the link stage in this environment
  (`collect2.exe: error: ld returned 5`, with `multiple definition of __imp_atan` between the
  local mingw's `libntdll.a` and `libmsvcrt.a`), and `cargo build` cannot re-run the
  `deepseek-protocol` build script (`OS Error 5` opening `proto/action/v1/action.proto`).
  Executables whose image lives under `D:\deepseek` are restricted on this machine — one Go
  binary that binds `127.0.0.1` from `D:\deepseek\artifacts` fails with `winapi error
  #10106` while the same bytes run from `%TEMP%` or from `D:\wb-probe-tmp` — so the build
  scripts and the fresh test binary are the blocked part, not the source. Rebuild the gateway
  and re-run the fast path in an unrestricted shell before claiming the Rust half.
- [ ] Observe create/update/delete in a real browser against built Rust/Go processes. The
  harness supports it (`DEEPSEEK_NATIVE_BROWSER_HOLD_SECONDS` holds both processes, disables
  token auth and prints `BROWSER_URL`), the frontend already binds all four verbs
  (`frontend/src/api/workspaceBackupApi.ts`) and `AutomaticBackupsTab.tsx` now reads the three
  inventories with `Promise.allSettled`; the observation itself is outstanding.
- [ ] The mirror slice's remaining gaps: source fence, export/handback document, browser
  observation.

## 5.0 active execution — 2026-10-01 (policy write channel)

The 4.8.1 list below remains a historical contract-freeze record. Current
work is tracked against the full product matrix, with 5.0 still **未完成**.

- [x] Add Go schema v15 and the append-only `control_operator_mutations` journal, as a
  **separate** table rather than a widened `control_operations.result_status` (which is
  `CHECK(... IN ('PROPOSED','APPLIED'))` and means "signed"). Update every historical
  fixture so the fail-closed object catalog still refuses a forged older version.
- [x] Implement `Control.ApplyOperatorMutation`: capability + durably Go-authoritative
  domain + live cutover read inside the write transaction; record, control event and
  journal row in one transaction, stamped with actor, server-generated `actionId`, the
  live `executionEpoch`, the cutover revision/fencing token and the writer fence. Replay
  is `ALREADY_APPLIED`; a reused key with a different body is a replay conflict.
- [x] Serve `POST` and `PATCH /api/workspace/backup-policies` through that channel, with
  the oracle's merge list, the read route's admission, lazy authoritative target-binding
  resolution and the oracle's own refusal messages.
- [x] Hold the unchanged Go 95.0% floor at **95.037888% (7776/8182)**.
- [x] Add the **terminal tombstone state** for policy so `DELETE` can remove a policy:
  removing the row leaves its immutable events orphaned, which the read path reports as
  `CORRUPT_RECORD`, so delete was 501 `GO_POLICY_DELETE_UNSUPPORTED` until a state existed
  that `Get` and `ListAuthoritativeRecords` both honour as absent. **Landed** — see the
  current section at the top of this file; the 501 constant is gone.
- [ ] Observe create/update/delete in a real browser against built Rust/Go processes.
- [ ] The mirror slice's remaining gaps: source fence, export/handback document, browser
  observation.

## 5.0 active execution — 2026-10-01 (policy write semantics)

The 4.8.1 list below remains a historical contract-freeze record. Current
work is tracked against the full product matrix, with 5.0 still **未完成**.

- [x] Port the backup-policy write semantics (`normalize_policy`) to
  `go/internal/policy`, pinned by an 81-case oracle fixture
  (`scripts/generate_policy_normalization_fixture.py` →
  `go/internal/policy/testdata/policy_normalization_v1.json`, `--check` stable).
  Acceptance: every accepted payload normalises to the same document, every refusal
  carries the same message/code/status, the four uncaught `ValueError`s stay 500s, and
  the validation order is pinned boundary by boundary. Two real divergences were found
  and fixed (Python's `a or b` last-operand rule; `parse_cron` wrapping a `ValueError`).
- [x] Hold the unchanged Go 95.0% statement floor after the new package:
  **95.399049% (7423/7781)**, `artifacts/go-coverage-policy.out`;
  `internal/policy` itself is at 98.7%.
- [ ] Settle and implement the **operator mutation channel** — the blocker for every
  policy write. `control_operations.result_status` is constrained to
  `('PROPOSED','APPLIED')`, so a distinct journal needs a schema migration; preferred
  shape is a separate append-only `control_operator_mutations` table so the signed
  journal cannot become ambiguous, with an authenticated public route gated on
  `authorizeCutover` + a durably Go-authoritative domain, the record revision CAS,
  `prepareControlRecordWrite` admission, and a server-generated `actionId` plus the live
  cutover `executionEpoch` in the same transaction.
- [ ] Only after that: `POST`/`PATCH`/`DELETE /api/workspace/backup-policies` on top of
  `internal/policy`, with the browser flow observed end to end.

## 5.0 active execution — 2026-10-01

The 4.8.1 list below remains a historical contract-freeze record. Current
work is tracked against the full product matrix, with 5.0 still **未完成**.

- [x] Declare `frontend_mirror_store` (`data`, python -> rust, 4.9.4, `rust_data`) and
  port the sealed frontend mirror to `deepseek_policy::backup_mirror`: immutable
  generations, `HEAD.json` CAS, epoch indexes, idempotent replay, stale-epoch/sequence
  and head-conflict refusals, variant selection, corruption detection and the legacy
  4.4.4 read path. Sealing links `backup_crypto` (now lib+bin) rather than copying it.
  Acceptance: two-way byte-level oracle parity (8/8), a legal upload serving a real
  generation through the public routes, and the oracle's refusals on illegal ones.
- [x] Gate the Python mirror writer mechanically at its single write choke point and
  keep reads available, then prove both directions with real callers: the denied case
  leaves no directory behind, the permitted case publishes a verified generation.
- [x] Serve `GET /api/workspace/backup-mirrors`, `GET .../{profile}` and
  `PUT .../{profile}/frontend` from the Rust edge **only** after the store changes hands,
  so the Go proxy is never shadowed and Python keeps owning the path until then.
- [x] Keep the recipient sets in Go: the authenticated loopback route
  `GET /internal/control/backup-policy-recipients` derives the union over all policies
  (with Python's `protection or encryption` fallback) and one group per enabled policy
  (no `encryption` fallback, empty group preserved). An unavailable source refuses
  instead of returning an empty set that would skip the recipient check.
- [x] Hold the unchanged Go 95.0% coverage floor after the new route at
  **95.015755% (6634/6982)** — note the thin margin: two statements in the route are
  unreachable through any legal write path.
- [ ] Add the mirror **source fence** and a canonical export/handback document. Rust
  currently reads the directory Python wrote, so a cutover means "stop the Python
  service first"; there is no fence row, attestation or handback receipt yet.
- [ ] Observe the native mirror routes in a real browser against built Rust/Go
  processes, and prove `backup_scheduled`'s `mirror_files` consumer reads a
  Rust-written generation.
- [ ] Settle and implement the policy **write** channel: the signed v2 apply channel
  needs an external signer, so an authenticated browser CRUD request has no path today.
  The candidate design is an operator mutation channel gated by the durable cutover and
  journalled with `actionId + executionEpoch`, leaving the signed channel unchanged.
- [ ] Complete the repaired full Python gate, pinned Linux Go race and Rust
  coverage checks. Original full Python run: 5533 passed, nine failures,
  95.56% coverage. The current full rerun has no completed result yet.
- [x] Local read/transfer slice: preserve the target-health history in Python's separate
  scheduler SQLite, fence that source table, bind it to an exported target
  inventory and its signed promotion, and import/read it from Go without a
  Python runtime. Preserve the v1 inventory format; use a new v2 manifest for
  the additional scheduler source. Native target GET must refuse missing
  health-transfer evidence rather than fabricate an empty history.
  Acceptance: nonempty and attested-empty history, real old Python writer
  denial, exact retry, tamper/missing fence refusal, Go import atomicity,
  first-promotion source reattestation, restart and reversible handback on
  isolated copies, authenticated Rust→Go HTTP success and browser observation.
  Scheduler mutations, mirror generations and provider effects remain later
  slices; they are not qualified by a read-only health transfer.
- [x] Restore the full Go 95.0% coverage gate after the existing signed-binding
  changes and this slice: **95.009403% (6568/6913)**, profile
  `artifacts/go-target-health-coverage-v14.out`; no threshold or business-code
  exclusions changed. The fresh CGO-free Go daemon passed both policy and
  target Rust→Go process tests. A complete local LLVM MinGW toolchain fixes
  the prior Windows race startup failure; 17 packages passed. The store's 398
  top-level cases are all accounted for: 396 passed and two explicit skips
  (Windows symlink privilege, opt-in frozen-vector generator). Initial package
  timeouts remain recorded; this is partitioned local evidence. A pinned Linux
  run is in progress; exact-head CI race evidence remains open.
- [x] Repair host-capacity-dependent offline placement fixtures after the full
  Python gate exposed nine failures at 91.2% disk usage. Only eight explicit
  filesystem unit scenarios use the isolated capacity fixture; provider probes
  are untouched. The original failover unit now also proves refusal at 95%
  usage. All 109 affected placement/capacity/recovery checks pass; the unchanged
  90% production watermark and 95% coverage floor remain enforced.
- [ ] Complete the repaired full Python gate, pinned Linux Go race and Rust
  coverage checks. Original full Python run: 5533 passed, nine failures,
  95.56% coverage. The current full rerun has no completed result yet.

- [x] Exercise the public backup-policy read in an actual browser against built
  Rust gateway and Go daemon processes on isolated loopback fixtures. The
  signed nonempty policy and next run appear. A frontend `Promise.all` had
  hidden the policy when target and mirror endpoints returned 501; independent
  reads now show the policy and explicitly report those unavailable states.
  This qualifies only the read slice, not full automatic backup behavior.
- [x] Reject an authoritative Go cutover when its persisted signed promotion
  artifact or authorization row is missing or its artifact digest differs.
  Ordinary cutover, authority and public inventory reads fail closed in the
  regression case. Full local Go coverage now passes; exact-head CI remains open.
- [ ] Transfer backup-mirror generations/HEAD and ciphertext with validation,
  unique-writer fencing, crash recovery and rollback; implement native mirror
  reads and mutations. Target-health transfer and target GET are locally
  verified above, but native provider probing/refresh, target CRUD and scheduler
  writes remain open. No fabricated empty health or mirror view qualifies them.

- [x] Reconcile the current `c99d3de6` checkout and preserve the existing
  uncommitted control-plane work; update the matrix's Go control status.
- [ ] Expand the remaining aggregate matrix rows into per-capability entries
  with original entry, observable behavior, owner, dependency, implementation,
  compat case, platform, command, evidence and gap before any final acceptance.
- [x] Make `control-authority-v1` claim and live-head read reachable only through
  the authenticated loopback internal API. Locally verify claim → cutover →
  signed v2 apply → persisted result, exact replay and refusal paths.
- [x] Refuse cross-domain reuse of a control mutation operation ID; regression
  tests first reproduced the false `ALREADY_APPLIED` response in both v1 and v2.
- [x] Re-run the exact Go coverage gate after the new routes and tests without
  changing its 95.0% statement floor: local result 95.127394% (5115/5377),
  with fmt/vet/test passing. Windows `go test -race ./...` exits `0xc0000139`
  before tests execute; Linux CI race evidence remains open.
- [ ] Add an externally signed per-domain promotion artifact and verify
  export/import, unique writer fencing, rollback and restart on isolated data.
  The signed artifact is now integrated locally with a deployment-pinned
  Ed25519 public key, an append-only v10 journal and started-process HTTP
  success/refusal and exact-replay tests.
  This item remains open until independent export/import, fencing, rollback and
  restart evidence qualifies the transfer.
- [x] On isolated Python control stores, export nonempty policy/target records
  against the live authority checkpoint and atomically fence those source
  tables. Prove real Python write denial, concurrent serialization, exact retry,
  unknown-effect refusal and failed output recovery.
- [x] Require the same fenced-source attestation for **empty** policy/target
  inventory before signed Go promotion. Use Python-generated empty-source
  fixtures, bind manifest/source digests and transfer ID, and prove a legal Go
  write followed by later signed authoritative-state transitions.
- [x] Add the Go-owned backup-policy **read** slice behind the native `/api/*`
  proxy: gate the list on an authoritative policy cutover in the same read
  transaction, validate records and histories, authenticate the direct Go
  route, match Python's next-run/DST/jitter oracle, and prove nonempty imported
  data plus an attested empty import followed by a signed Go policy write.
  Local Go HTTP integration and the unchanged 95% statement gate passed; this
  is not the full policy migration or a default production cutover.
- [x] Run built Rust gateway and `deepseekd` executables together. The
  `native_integration` case applies a signed policy mutation to an isolated Go
  SQLite store, closes the seeding process, reopens it in `deepseekd`, reads the
  nonempty result through Rust over TCP, and proves foreign Host and missing
  token refusals. Installer, browser/device, CRUD/run/continuity, scheduler and
  production cutover evidence remain open.
- [x] Bind and recheck legacy `.backup-policies/*.json` projections before
  policy export. The offline exporter refuses unadopted or malformed
  policy/target JSON, then binds the projection directory's exact state
  (`legacyProjection`: file count plus a canonical digest over each file's
  name, size and SHA-256) into the export manifest. The Go attester re-derives
  that digest from the directory — inferred from the standard `.backup-control`
  layout, or named explicitly through `--projection-dir` — and fails closed when
  the directory appeared, vanished or changed. The directory is **bound and
  rechecked, not frozen**, so the Python service must stay stopped. Go's scan
  includes hidden JSON, rejects JSON-named directories, and independently
  validates every projection's JSON and ID against the fenced rows even if
  a caller reseals the manifest digest. Local Go tests and the unchanged full
  95% statement gate pass at 95.008473% (6167/6491); checked-in fixtures prove
  both runtimes agree on the digest byte for byte. Then port public policy
  create/update/delete/run/continuity,
  scheduler claims and recovery with unique Go ownership, and verify the
  public success path through real Rust→Go processes and a browser.
- [x] Reattest the Python source and legacy policy/target JSON projection at the
  first signed promotion. Schema v13 durably binds the exact manifest bytes and
  source/projection locations to each attested import; policy/target drift is
  refused after restart. Upgraded unpromoted v11/v12 imports lack that binding,
  cannot promote, and can be handed back for a fresh export; an older already
  promoted import refuses upgrade. Keep the source service
  stopped: the external projection directory is not mechanically frozen, and
  cross-store atomicity and production transfer remain open.
- [x] Require an explicit absolute projection directory for nonstandard source
  layouts in both the Python exporter and Go attester, including their library
  entry points. The former `digest: null` bypass is refused before a source
  fence or Go import; tests cover refusal, a real explicit empty-directory
  export, and a resealed null-binding manifest. The first promotion still
  needs fresh source/projection proof as tracked above.
- [ ] Import that fenced inventory into the Go-owned store without losing
  revisions or user behavior; verify source and target digests, linked effects,
  unique writer ownership, restart recovery and a reversible handback. The
  explicit offline Go importer now accepts a nonempty Python-exported policy or
  target inventory into a **fresh** dual-evaluate domain, verifies the installed
  checkpoint and hashes, reopens the explicit Python source SQLite read-only
  to verify the exact fence, live rows and pending effects, writes its Go side
  atomically, and survives Go restart on an isolated store. Schema v11 now
  journals import provenance with the rows/events, preserves the Python CAS
  revision in the Go record and first event, and binds the attested source
  digests and source boot epoch to signed promotion, including empty domains.
  This remains open: existing shadow history,
  linked state, cross-store atomicity, complete business/API parity and
  handback are not implemented. Nonzero policy
  `topology_generation` is refused because the checkpoint does not carry it;
  promotion/drain/placement generations remain in the checkpoint but are not
  consumed by native business logic yet.
- [x] Prove the **reverse** transfer on isolated stores. `go
  cmd/control-inventory-handback` refuses a promoted, de-promoted, Go-mutated,
  wrong-transfer or unjournaled domain, removes the imported records, their
  events and the import provenance row in one transaction, appends the
  append-only v12 `control_inventory_handbacks` row, and emits the canonical
  `control-inventory-handback-v1` document.
  `scripts/native_control_handoff.py --rollback` verifies those exact Go bytes
  against the fenced export, refuses a changed source, journals an append-only
  revocation, removes the fence row and its guard triggers atomically, and
  publishes `python-control-inventory-handback-receipt-v1`. A **separate Python
  process** is denied before the handback and writes for real after it; the
  handed-back source can be fenced and exported again for a new transfer.
  The checked-in Go-produced fixture caught a real defect: Go hashed the
  document with an empty `handbackDigest` field present while Python excludes
  it, so the two sides disagreed about the bytes.
  Cross-store atomicity is still not claimed: the two SQLite databases are not
  one transaction, existing Go shadow history and the Python control state
  outside the fenced set are still not transferable, and no production handback
  has been performed.
- [x] Freeze the **linked** control state the transfer binds, not only the
  exported rows. While a fence is held the Python writer is mechanically denied
  on `control_authority_head`, `control_authority_outbox`,
  `control_authority_mutations` and `control_boot_state` (global: any fence
  freezes them) plus the `lifecycle_intents` and `target_receipt_mutations` rows
  that name a fenced domain — 18 `native_control_fence_*` triggers, which the Go
  source attester now requires byte for byte before it reads a row. A Python
  service restarted after the export fails closed with
  `PythonWriterMechanicallyDeniedError` instead of resuming ownership. The
  linked objects survive a partial revocation and are released only when the
  last fence is lifted; the append-only revocation journal remains as the audit
  record. Proven by Python tests through the production control connection, by
  the Go linked-fence denial test, by refreshed cross-language fixtures and by a
  two-step revocation that lifts the whole fence at the end.
- [ ] Prove worker effect reconciliation through real MinIO provider state,
  process kill, lease loss and takeover; retain `EFFECT_UNKNOWN` until proof.
- [ ] Complete remaining native control/data APIs, Rust public edge, desktop,
  Android and server-side TypeScript replacement; measure a successful zero-Python
  workload on each production platform.
- [ ] Obtain exact-head CI, provider and platform artifacts, Evidence Assembly,
  and the final readiness qualification before changing ownership status.

## Phase 0 — Decision and ownership

- [x] Review/accept 5.0 native runtime specification and ADR-0049.
- [x] Resolve fresh-context adversarial findings.
- [x] Add machine-readable ownership contract and negative tests.
- [x] Confirm existing `tasks/plan.md` and `tasks/todo.md` remain unchanged.

## Phase 1 — Protocol foundation

- [x] Pin Go 1.27.1, protoc 36.1, generators/runtimes, and Windows/Linux checksums.
- [x] Add deterministic tool bootstrap/check path.
- [x] Define `common/v1` and `action/v1`.
- [x] Define `storage/v1` and `federation/v1`.
- [x] Define `control/v1`, `evidence/v1`, and `agent/v1`.
- [x] Generate hashed Go bindings, Rust Prost/Tonic types, and a binary descriptor from the proto contract.
- [x] Add descriptor compatibility and generated-code drift gates.
- [x] Bind Go admission/dispatch helpers to generated types and require Rust's descriptor to equal protoc bytes.
- [x] Freeze an immutable v1 semantic baseline and compare it against the complete binary descriptor, including oneof/presence/options/RPC streaming metadata.
- [x] Keep generated Go/Rust code out of business-logic coverage without lowering thresholds.

## Phase 2 — Non-authoritative native processes

- [x] Initialize Go module and `deepseekd` lifecycle/health.
- [x] Prove 4.8.1 Go mutation paths are mechanically absent/denied.
- [x] Add isolated deterministic Go shadow envelopes.
- [x] Add Rust protocol crate.
- [x] Add Rust worker admission/result foundation.
- [x] Prove empty/zero/stale/missing/future action fences are rejected before effects, with epoch advance isolated to the authority path.
- [x] Prove unknown effects cannot be treated as not-applied.
- [x] Add a loopback-only Rust Tonic worker process and a typed Go client that never forwards caller-controlled `live_epoch`.
- [x] Prove with a real Go-to-Rust process test that an authority-uninitialized worker returns exact `FENCE_MISMATCH`.
- [x] Make Go-to-Rust effect queries require an exact returned fence and keep missing or unproven effects fail-closed.

## Phase 3 — Canonical corpus

- [x] Add corpus manifest, schema, sensitivity policy, and provenance.
- [x] Freeze REST inventory (SSE remains in existing gateway fixtures).
- [x] Freeze MCP corpus via existing protocol-preparation fixture.
- [x] Freeze storage wire field inventories.
- [x] Freeze federation and evidence inventories.
- [x] Freeze legal durable state transition labels and fail-closed rules.
- [x] Replay eventual Rust-owned MCP cases through the Python oracle; Rust admits fences locally.
- [x] Replay eventual Go-owned shadow digests and mutation denial in Go tests.
- [x] Produce immutable corpus SHA-256 digests.

## Phase 4 — CI, operations, release

- [x] Add Go fmt/vet/test/race gates.
- [x] Run the real Go-to-Rust admission and effect-query boundary in the `native-go` CI gate with exact worker cleanup.
- [x] Extend Rust workspace to protocol/worker crates.
- [x] Add protocol generation and native contract gates.
- [x] Add native migration/rollback/unknown-effect runbook.
- [x] Run existing frontend/Python/Rust/eval/security/release gates.
- [ ] Run exact-head CI and Evidence Assembly.
- [x] Verify no production owner or frozen contract changed.
- [ ] Qualify 4.8.1 without skips, mocks, or synthetic Evidence.

## 4.8.1 release blockers

- [ ] Any unexplained canonical parity divergence.
- [ ] Any Go production mutation capability.
- [ ] Any unfenced/stale Rust worker effect.
- [ ] Any shared Python/Go/Rust writable durable state.
- [ ] Any change to a frozen contract.
- [ ] Any non-reproducible generated binding.
- [ ] Missing exact-head provider/native Evidence.


### 2026-10-12 durable synthesis execution checkpoint


- The authorized cloud branch `codex/rust-cloud/20261010` contains reviewed commit `3cce762ecaa77ea33e280879657b7e3cc5c086d6`, frozen tree `ebe711efa66f4a175c39bc1f0573b62c4ceb0ae5`. The local source checkout, main Git HEAD/index, runtime data and native caches remain separate.


- Go synthesis admission verifies the original signed task settlements, immutable task completion checkpoint, and terminal lease/resource journals. Go selects the synthesis intent and predecessor IDs. Rust executes from original request/confirmed output custody with its own phase grant, live lease, fenced provider exchange, immutable journal and signed content/counters. Transactional worker schema v6 preserves prior journal bytes and all original fencing/immutability guards.


- All 11 local source/format/codegen/boundary checks passed; 863 native and 3631 complete snapshot inputs match the reviewed tree. Rust/Go builds, races, original coverage floors and Docker verification remain cloud-only. Exact current runs: Rust `38243369081`, complete CI `38243510658`; these results are pending and cannot borrow an older commit's qualification.


- Parent `1876a8dc86b888846f1a28fe2677d760810e7bdd` has collected bounded Rust PASS: 1459 tests passed, 0 failed, 1 original Redis ignore; 79668/97236 covered lines (81.932618%, original floor 80%). Its complete CI report has not yet been assembled at this checkpoint.


- Remaining: independently signed final Go synthesis settlement/public done, durable tool continuation, resume/cancel/rerun/final_reset, complete public Agent DAG and live provider proof, all retained capability ownership cutovers, desktop/Android/zero-Python workloads and final Evidence Assembly. Run completion and full product status remain **NOT_READY**.


- Next: collect exact cloud results and repair the first upstream failure, then implement final Go proof settlement and the real synthesis process probe. Do not use local native builds as a fallback.


Evidence: `artifacts/native-20261012-agent-synthesis-execution/{review.md,manifest.json,whole-snapshot-integrity-result.json,ci-dispatch.json}`; parent Rust evidence: `artifacts/native-20261012-agent-claim-writer-replay-fix/cloud-evidence.json`.


### 2026-10-12 synthesis process and cloud repair checkpoint


- Latest reviewed/uploaded source is `f6e531a17ff020cb3c76ea162d06d6e622ebe23c`, frozen tree `0040966ed1a2259683eab063f5c559ab3518a9dc`, on authorized branch `codex/rust-cloud/20261010`. All 11 local source checks passed, including 39 offline probe-boundary tests. 867 native / 3635 complete frozen inputs match; original HEAD/index and source before-images remain intact.


- The production Go synthesis client and independent real process scenario are implemented. The scenario requires completed task phase proof, a separate signed synthesis admission, actual fenced HTTP with both original task summaries, exact signed content/counters and body custody, and unchanged result after Rust/Go restarts without duplicate HTTP. Its actual qualification is still pending: Rust run `38250708129`, full CI `38250805776`.


- Exact parent `13bbf77bc135b95fc5bc1e8c6274f0ea39f68542` passed Go vet/unit tests but coverage was 11558/12169 (94.979045%) below the unchanged 95% floor. The added terminal RPC denial/cancellation tests require an unchanged durable store. Its Rust run failed another old v3 latest-schema assertion (6 actual, 5 expected); the follow-up preserves the identity rollback, epoch bytes and absence of invented grants. Maven protoc-gen-grpc-java download returned HTTP 429; bounded transient GET retries preserve the pinned URL and checksum.


- Previously completed commit `1876a8dc86b888846f1a28fe2677d760810e7bdd` now has collected complete CI PASS, 37 jobs and all 14 Go/Docker gates, default Go coverage 11506/12111 (95.004541%) and Android Go coverage 11332/11921 (95.059139%). Bounded Rust had 1459 PASS / 0 failed / 1 original Redis ignore and 81.932618% coverage. This older proof does not qualify the later synthesis commits.


- Existing disk cleanup remains applied. Current measured free space: C approximately 32.35 GiB, D approximately 85.56 GiB. All native compilation, race, Docker images and caches remain cloud-only; only reports are downloaded.


- Final signed Go synthesis settlement/public done, tools continuation and every retained full-project/native ownership/platform/provider/recovery requirement remain open. No run completion, production cutover or release is claimed. **NOT_READY**.


Evidence: `artifacts/native-20261012-agent-synthesis-process/{review.md,manifest.json,whole-snapshot-integrity-result.json,ci-dispatch.json}` and older exact-revision reports under `artifacts/native-20261012-agent-claim-writer-replay-fix/`.


### 2026-10-13 final synthesis settlement cloud checkpoint


- Reviewed source commit `47fc7f5441e84c8bda67ffb191a2c115b197f2e7`, tree `1f93d294124aeab93eb6a2104e7bd0083d2e05ff`, parent `4618362130ddc65ce9cc8da2bded343c66d57dde`: 25 files, additive typed synthesis projection/settlement, independent Rust signing domain and atomic Go content/done, terminal action and lease/resource release. Generic writers cannot install the private settlement marker. Historical source/task proof and both final immutable checkpoints are rechecked on replay/read. Production key custody remains Rust only.


- Source/format/codegen/contract/type/security checks and 43 Python runner-boundary tests passed. Native tests have not run locally. Exact-head Rust `38325283475` and full CI `38325355485` are running; all original scenarios, 15 crates, Rust80/Go95/Python95 floors and original Redis ignore remain required.


- Parent `4618362` has collected complete CI PASS: 37 jobs, all 14 Go/Docker jobs; Go default 11573/12180 (95.016420%), Android 11400/11990 (95.079233%). Its bounded Rust process gate failed at the low-level custody signer excluding synthesis phase three; actual producer log and failure are retained. Parent success does not qualify this new source.


- Current local free space: C approximately 32.36 GiB, D approximately 85.89 GiB. Compilation/caches/Docker remain cloud only, reports only downloaded. Original local HEAD/index and D:/deepseek source preserved.


- Full objective remains open: complete public durable Agent DAG integration, tool continuation, resume/cancel/rerun/final_reset, live provider/effect recovery, all retained capability ownership, desktop/Android/zero-Python workloads, original exact-head gates and final Evidence Assembly. Final synthesis is implemented but cloud qualification is pending; no full-product completion, production cutover, release or main-branch merge is claimed. **NOT_READY**.


Evidence: `artifacts/native-20261012-agent-synthesis-final-projection/{review.md,manifest.json,whole-snapshot-integrity-result.json,ci-dispatch.json}` and parent `artifacts/native-20261012-agent-synthesis-endpoint-refresh/{cloud-evidence.json,full-ci-evidence.json,go-docker/cloud-evidence.json}`.


### 2026-10-13 final transaction authority repair checkpoint


- Latest reviewed uploaded snapshot `13ca29dbf7ba1fb49572a52110dad9c72d333411`, tree `ab3ffce7ca88d8fe6274b335e84e89321768f5dc`, binds every live Agent phase to its original action deadline at the common final commit. Cloud regressions demonstrated expired writer commits on initial synthesis and exact replay; these failures and the root-cause repair are preserved. Claim responses now advertise the actually committed writer deadline. Delayed historic signed outcome retention keeps its established observation-only semantics.


- All 11 local source checks and 43 offline runner-boundary tests passed; no local Cargo/Go compilation or Docker verification ran. Exact Rust `38329050070` and full CI `38329098161` require original Rust80/Go95/Python95 floors, all 37 CI jobs and 14 Go/Docker jobs. Native/cloud qualification is pending, not inherited from a parent.


- Original checkout HEAD/index and dirty native source remain preserved; compilation and caches stay in cloud, only reports/logs are downloaded.


- Remaining: actual public durable Agent DAG wiring, tool continuation, resume/cancel/rerun/final_reset, real provider/effect recovery, all retained capability ownership, desktop/Android/zero-Python workloads and final Evidence Assembly. **NOT_READY**.


Evidence: `artifacts/native-20261013-agent-phase-final-authority/review.md`, `manifest.json`, `ci-dispatch.json`; demonstrated failure: `artifacts/native-20261013-synthesis-projection-lint-fix/failed-job-115070105139.raw.log`.


### 2026-10-13 confirmed-custody recovery checkpoint


- Current reviewed uploaded commit `04838cf9f597c1c053d78d6821f34d8867b0b6c2`, tree `d60ec5c85042d6422c51ad938fbea1f51c2a6d40`: authenticated original confirmed Rust custody is checked before requesting another Go dispatch. Exact scope, grant, writer and historical signer/realm remain bound; current signed grant and durable epoch checks remain in place. Confirmed results replay without a new effect permit, and unresolved effects remain `EFFECT_UNKNOWN`.


- Actual older synthesis process failed after three HTTP requests and terminal Go settlement at the post-restart Execute replay; its producer log is retained. Older Rust coverage passed 80102/97822 (81.885465%), but does not qualify the new source. Latest required gates: Rust `38331122792`, full CI `38331159871`, 1468 Rust tests with the original one Redis ignore, all 37 CI/14 GoDocker jobs and Rust80/Go95/Python95.


- Parent Go ordinary tests passed, but exact coverage remained 11864/12489 (94.995596%), despite a rounded 95.0% display. New exact-replay writer loss, ignored lease update rollback/retry and malformed digest regressions preserve the 95% floor. Eleven local source checks and 43 offline runner-boundary tests passed; native qualification remains pending.


- Original local source HEAD/index and dirty native files are preserved; no local native compilation, Docker verification or downloaded caches/images/volumes. Public durable Agent wiring, tool continuation, resume/cancel/rerun/final_reset, provider/effect recovery, retained capability ownership, desktop/Android/zero-Python and Evidence Assembly remain open. **NOT_READY**.


Evidence: `artifacts/native-20261013-confirmed-node-replay-fix/review.md`, `manifest.json`, `ci-dispatch.json`; actual failure: `artifacts/native-20261013-synthesis-projection-lint-fix/results/checks/agent-signing-process/synthesis-success-go.log`.


### 2026-10-13 durable node identity recovery checkpoint


- Latest reviewed uploaded snapshot `f2d0f3594ea62e50e9dfcf6bd3042c60c81e68de`, tree `e58165eae8096a742384aec536d3315e3e8f05f4`, parent `04838cf9f597c1c053d78d6821f34d8867b0b6c2`: two Rust files, +66/-4. The current-head process failure showed that the caller's Go operator command ID differs from the deterministic `agent-node-*` ID stored in Rust custody. Retained recovery now uses the existing unique action/epoch/node identity and revalidates the full original scope, exact grant, writer, intent and signer realm. Authentication, current signed grant and installed epoch admission remain required. No retained read returns another provider permit; uncertainty remains `EFFECT_UNKNOWN`.


- The restart regression now uses distinct actual command/custody IDs. Another regression proves changing the command cannot restart an uncertain effect and malformed command/actor inputs are denied. The real three-request process probe and original receipt/projection/terminal settlement assertions are unchanged. Eleven local source checks passed; no native compilation ran locally. Exact-head bounded Rust `38333342473` and complete CI `38333377558` are running. Required inventory is 1469 passed Rust tests plus the original Redis ignore, 15 crates, all 37 CI/14 Go-Docker jobs and original Rust80/Go95/Python95 floors.


- Parent `04838cf9` passed Rust coverage at 80289/98006 lines (81.922535%), but its actual synthesis Execute replay still failed after terminal settlement and Rust restart. Its full CI remains in Go race verification after the original default Go95 gate passed. These parent results do not qualify the new head. Failure log and source hashes are retained under `artifacts/native-20261013-confirmed-node-replay-fix/`.


- Compilation, compiler caches, Docker images and verification containers remain cloud only; only reports are downloaded. Original local HEAD/index, dirty native source and D:/deepseek remain preserved. Public durable Agent creation/DAG execution, tool continuation, resume/cancel/rerun/final_reset, provider/effect recovery, all retained capability ownership, desktop/Android/zero-Python workloads and final Evidence Assembly remain required. **NOT_READY**.


Evidence: `artifacts/native-20261013-node-custody-identity-fix/{review.md,manifest.json,ci-dispatch.json,prior-failure-evidence.json}`.


2026-10-13 exact `f2d0f359` bounded Rust gate collected **PASS**: 1469 passed tests, the original one Redis ignore, all 15 crates, Clippy for all targets/features, and 80343/98067 covered lines (81.926642%, original 80% floor). The real Go/Rust TLS producer, authoritative authorization RPC, controlled three-HTTP synthesis, final `done` at index 8, released resources, immutable outcome/projection and Go/Rust restart replays all passed. This controlled fixture seeds the request body through the offline oracle; live provider and zero-Python/full-DAG product readiness remain unqualified. Complete CI `38333377558` is still running, including Go race and its original coverage floors. Only 4,940,363 bytes of reports were downloaded; no build caches/images/volumes. Evidence: `artifacts/native-20261013-node-custody-identity-fix/{cloud-evidence.json,results/coverage/coverage-evidence.json,results/checks/agent-signing-process/synthesis-success-result.json}`. **NOT_READY**.


### 2026-10-13 cloud checkpoint: public native Agent driver


Reviewed dedicated-branch commit `717465f6fc933bdb738e8000599545615f4799d3`, tree `55927c42d164bd90673204800e99f4925c681d70`, adds actual Rust POST request custody and the default authoritative Go plan/task/synthesis DAG driver. Rust run `38345811943` and full CI run `38345847767` are pending qualification. Local Linux/Win32 type checks cover 961 sources; 82 offline evidence boundaries passed. Source/native builds, Go tests and Docker verification remain cloud only; only reports are downloaded. See `artifacts/native-20261013-agent-public-driver/review.md`. Root executable source has not been applied.


Prior renewal-witness commit `44aceca20ee9736e3a304210e8d5821f0c48f1f1` has exact-head bounded Rust PASS: 1469 passed, one original Redis ignore, 81.9271948608137% line coverage; real grant-expiry/refresh/dependent settlement and Go/Rust restart/replay passed. Its full CI is still pending. The prior process probe seeded input via an offline oracle; the new public probe must create the input through actual Rust HTTP and qualify four actual controlled upstream requests. Neither result qualifies a live provider, all presets/tools/public controls/recovery, default desktop/Android packaging, full zero-Python product ownership or Evidence Assembly. **NOT_READY**.


### 2026-10-13 public process failure and retained confirmation


Commit `717465f6fc933bdb738e8000599545615f4799d3` passed Rust lint/workspace tests and coverage but failed the actual public process gate: Rust POST returned `NATIVE_AGENT_CONTROL_UNAVAILABLE` because test-environment sanitization removed Go control origin/bearer and artifact configuration. Exact failed process evidence is retained in `artifacts/native-20261013-agent-public-driver/results/checks/agent-public-driver/public-driver-go.log`. This revision is not qualified.


Commit `23036c75b780ae4e584caa8b23ed55375ff90a18` adds retained auto/long-query plan confirmation and native public plan/cancel routes. Commit `741521a86f81e424502551aa2c6e3df5362c769b`, tree `a847b66d1c66964a57cd51d150b54bf39c3a9a5e`, fixes only the two test-tooling files by restoring explicit private-loopback Go binding and Rust body custody for the gateway. Private keys/provider credentials remain excluded. Both platform type checks cover 961 sources and 90 offline boundaries passed. New Rust run `38348690552` and full CI await exact-head qualification; required public proof includes actual HTTP creation, no task admission before confirmation, actual HTTP confirmation, four controlled provider calls, nine events and three completed/released Go phases.


Historical `f2d0f3594ea62e50e9dfcf6bd3042c60c81e68de` has collected bounded Rust PASS plus 37/37 full-CI and 14/14 Go/Docker proof; Go default coverage is 95.0196172631916%, Android 95.08090088625092%. It does not qualify later source. Latest root executable source/index remain preserved. Full preset parity, tools, runtime payload updates/rerun/resume, uncertain-effect recovery, real providers, default packages/device behavior, all retained capability ownership and full zero-Python/Evidence Assembly remain outstanding. **NOT_READY**.


<!-- native-20261013-agent-preset-parity checkpoint -->


2026-10-13 cloud checkpoint: `df8b5d8d1b6fd2f6c14862665c2f0fe0a782967b` passed both bounded Rust jobs (1476 passed, original Redis ignore, 15 crates; 81148/98886 = 82.06217260279513%). Actual public creation/confirmation/DAG settlement passed. All three original Python95 gates passed (5834 passed + one skip + 61 subtests each; 95.57/95.51/95.58%). Full 37-job CI remains failed solely at Go's original floor and dependent assembly/RC gates: exact Go coverage 12202/12852 = 94.94242141300965%, eight covered statements short. Go functionality passed; Android Go gate did not execute after the floor failure.


New reviewed cloud snapshot `a1e53a8793b47e73cf9a8d7c546c64a6e9f23a45`, tree `7a678a0a10808a1d7099b246f5f43a12af45b597`, contains fourteen files (+1070/-25). It wires original static full/code/research/reason/critic and automatic selection into actual admitted/attested Rust planning, accepts explicit leader and retained planner agents schema, and keeps Go normalization, confirmation, scheduling and settlement. Added 23 offline parity cases, 13 real public HTTP creation scenarios and actual signing-custody/closed-renewal/lost-reply-query regressions. Linux/Win32 whole source checks (961 Python inputs), 104 offline boundary cases, format, contracts, documentation and exact synthetic-secret reviews passed. Cloud qualification is pending; expected Rust count is1479 plus the original ignore. See `artifacts/native-20261013-agent-preset-parity/review.md` and per-run evidence. Dedicated branch only; root native source, local HEAD/index and D:/deepseek remain preserved. No local Rust/Go compilation/tests or Docker verification; reports only downloaded.


Keep NOT_READY. Required next work: verify latest exact-head Rust1479/native884/all3655, all37 CI/all14 Go-Docker and original Rust80/Go95/Python95 floors; then prove public cancellation, runtime payload updates, resume/rerun/tools, extended takeover/recovery, real providers, all retained capabilities, default server/desktop/Android/worker packages, actual app/device behavior and complete zero-Python product evidence. Controlled providers, offline Go probe and bootstrap HTML do not qualify default CLI, frontend/platform or full migration.


<!-- native-20261013-preset-verifier-fix checkpoint -->


2026-10-13 verifier follow-up: fourteen-file preset snapshot a1e53a8793b47e73cf9a8d7c546c64a6e9f23a45 failed cloud qualification: Go vet rejected two new test closures declared func() rather than the existing func() error callback type; the thirteen-preset process step lacked checks-mode binding and attempted to run without the pinned Go toolchain in coverage mode. Original signing process also could not build its Go producer because of the same test callback error. Preserve these actual failures; they do not qualify presets or Go95.


Two-file follow-up c9d6fc71d41a71a73c13fcfe777a4b79a1a607cb, tree24b5a367778d45d8e8e7bb31271c57e5e6bdff44, corrects both callback return types and binds all thirteen new preset cases to checks mode, which installs Go and builds Rust binaries. Coverage mode keeps full workspace tests and LLVM80. All source/type/offline checks and exact synthetic-secret review passed; source and local index remain preserved. Latest exact-head cloud qualification pending; Rust1479/native884/all3655 and all original 37/14 gates and Rust80/Go95/Python95 remain required. Real-provider, public cancellation/updates/resume/rerun/tools, extended recovery, default CLI/platform packages and complete migration remain open. NOT_READY.
