from __future__ import annotations

import json
import struct
import unittest
from pathlib import Path


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError(f"{path} is not a PNG file")
    return struct.unpack(">II", data[16:24])


class EncodingRegressionTests(unittest.TestCase):
    def test_python_sources_do_not_start_with_utf8_bom(self) -> None:
        python_files = [Path("app.py")] + list(Path("deepseek_infra").rglob("*.py")) + list(Path("tests").rglob("*.py"))
        offenders = [str(path) for path in python_files if path.read_bytes().startswith(b"\xef\xbb\xbf")]

        self.assertEqual(offenders, [])

    def test_runtime_files_do_not_contain_known_mojibake_fragments(self) -> None:
        bad_fragments = [
            "\u93b4\u621d\u539b",
            "\u6fb6\u6c33\u7586",
            "\u7ed7?",
            "\u6924?",
            "\u6fb6\u8fab\u89e6",
        ]
        runtime_files = list(Path("deepseek_infra").rglob("*.py")) + [
            path for path in Path("static").rglob("*.js") if "vendor" not in path.parts
        ]
        offenders: list[str] = []

        for path in runtime_files:
            text = path.read_text(encoding="utf-8")
            if any(fragment in text for fragment in bad_fragments) or any("\ue000" <= char <= "\uf8ff" for char in text):
                offenders.append(str(path))

        self.assertEqual(offenders, [])

    def test_frontend_stream_and_favicon_guards_are_present(self) -> None:
        text = Path("static/modules/chat.js").read_text(encoding="utf-8")
        stream = Path("static/modules/stream.js").read_text(encoding="utf-8")
        panels = Path("static/modules/panels.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")

        self.assertIn("function parseStreamEventLine(line", stream)
        self.assertIn("JSON.parse(trimmed)", stream)
        self.assertIn("\"Skipped invalid stream event line\"", stream)
        self.assertIn('event.type === "system_note"', text)
        self.assertIn("assistantMessage.systemNotes.push(text)", text)
        self.assertIn("function systemNotesForMessage(message)", text)
        self.assertIn("isHttpUrl(result.favicon)", text)
        self.assertIn("export function isHttpUrl(value)", panels)
        self.assertIn("image/*", html)
        self.assertIn(".png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff,.gif", html)
        self.assertIn("这张图片需要 OCR 才能识别文字", text)

    def test_seek_frontend_entrypoints_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        network = Path("static/modules/network.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")

        self.assertIn("const presetSeeks = Object.freeze", app)
        self.assertIn("const seekCore = window.DeepSeekSeekCore", app)
        self.assertIn("function buildSystemPrompt(seekContext = state.activeSeekId)", app)
        self.assertIn("[Seek: ${seek.name}]", app)
        self.assertIn("storageKeys.seeks", app)
        self.assertIn("storageKeys.tavilyKey", app)
        self.assertIn('id="tavilyKeyInput"', html)
        self.assertIn('id="rememberTavilyKeyInput"', html)
        self.assertIn('id="clearLocalDataButton"', html)
        self.assertIn('apiFetch("/api/auth/logout"', app)
        self.assertIn("sessionStorage.removeItem(storageKeys.authToken)", app)
        self.assertIn("sessionStorage.removeItem(storageKeys.authToken)", network)
        self.assertNotIn("sessionStorage.setItem(storageKeys.authToken)", network)
        self.assertNotIn('readCookie("auth_token")', network)
        self.assertNotIn("prefixToggleButton", app)
        self.assertNotIn("responsePrefix", app)
        self.assertNotIn('id="prefixToggleButton"', html)
        self.assertNotIn("前缀续写", html)
        self.assertIn('id="activeSeekRow"', html)
        self.assertIn('id="clearSeekButton"', html)
        self.assertIn('id="seekReferenceInput"', html)
        self.assertIn('id="seekReferenceList"', html)
        self.assertIn('<script src="/seek_core.js"></script>', html)
        self.assertIn('aria-label="Seek 助手"', html)
        self.assertIn('id="seekButton"', html)

    def test_formula_frontend_entrypoints_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        markdown = Path("static/modules/markdown.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        math = Path("static/math_core.js").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("const mathCore = window.DeepSeekMathCore", app)
        self.assertIn("const formulaPrompt =", app)
        self.assertIn("renderMathBlock(singleLineDollarMath[1])", markdown)
        self.assertIn("export function formatContent(value, options = {})", markdown)
        self.assertIn("export function renderMarkdown(value, { streaming = false } = {})", markdown)
        self.assertIn("flushPendingMathBlockAsText", markdown)
        self.assertIn("{ streaming: message.streaming }", app)
        self.assertIn("function renderMathInline(value)", markdown)
        self.assertIn('<link rel="stylesheet" href="/vendor/katex/katex.min.css" />', html)
        self.assertIn('<script src="/vendor/katex/katex.min.js"></script>', html)
        self.assertIn('<script src="/math_core.js"></script>', html)
        self.assertIn(".content .katex", css)
        self.assertIn(".math-pending", css)
        self.assertIn("global.DeepSeekMathCore = Object.freeze", math)
        self.assertIn("renderToString(source", math)
        self.assertIn("data-latex", math)
        self.assertIn("renderPendingMathIn", math)
        self.assertIn('"/vendor/katex/katex.min.js"', sw)
        self.assertIn('"/vendor/katex/fonts/KaTeX_Main-Regular.woff2"', sw)
        self.assertTrue(Path("static/vendor/katex/katex.min.js").is_file())
        self.assertTrue(Path("static/vendor/katex/katex.min.css").is_file())
        self.assertTrue(Path("static/vendor/katex/LICENSE").is_file())
        self.assertGreaterEqual(len(list(Path("static/vendor/katex/fonts").glob("*.woff2"))), 20)
        self.assertIn("node --check static/vendor/katex/katex.min.js", workflow)
        self.assertIn("node --check static/math_core.js", workflow)

    def test_generated_download_links_use_fetch_blob_download(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        markdown = Path("static/modules/markdown.js").read_text(encoding="utf-8")

        self.assertIn('document.addEventListener("click", onGeneratedDownloadDocumentClick, true)', app)
        self.assertIn('a.download-link, a[href*="/api/download?"]', app)
        self.assertIn("downloadGeneratedFile(link)", app)
        self.assertIn('apiFetch("/api/download-save"', app)
        self.assertIn("已保存到：", app)
        self.assertIn("function generatedDownloadName(value)", app)
        self.assertIn("generatedDownloadApiPath(id)", app)
        self.assertIn("generatedDownloadIdFromHref", app)
        self.assertIn("const blob = await response.blob()", app)
        self.assertIn("downloadBlob(blob, filename)", app)
        self.assertIn('/api\\/download\\?id=', markdown)
        self.assertIn("data-download-id", markdown)
        self.assertIn('class="download-link"', markdown)
        self.assertIn("renderGeneratedDownloadImage", markdown)
        self.assertIn('class="generated-image generated-mindmap"', markdown)

    def test_seek_prompt_binding_and_cache_cleanup_are_stable(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")

        self.assertNotIn("clearLegacyCaches", app)
        self.assertIn("function seekSnapshotFromMessage(message)", app)
        self.assertIn("seekName: normalizeSeekText(value.seekName, 32)", app)
        self.assertIn("seekReferenceAttachments: normalizeSeekReferenceAttachments(value.seekReferenceAttachments || [])", app)
        self.assertIn("combinedAttachmentsForMessage(message)", app)
        self.assertIn('message?.role === "user" ? message.seekReferenceAttachments : []', app)
        self.assertIn("referenceAttachments: normalizeSeekReferenceAttachments(state.seekEditorAttachments)", app)
        self.assertIn("systemPrompt: buildSystemPrompt(assistantMessage)", app)
        self.assertIn("buildCompressedRequestParts(apiKey, requestMessages, assistantMessage)", app)
        self.assertIn("localStorage.removeItem(storageKeys.activeSeek)", app)
        self.assertIn("uniqueSeekNamesForMessages(state.messages)", app)
        self.assertIn("setActiveSeek(\"\", { closePanel: true })", app)
        self.assertIn("setActiveSeek(seek.id, { newChat: true })", app)
        self.assertIn("seekCore.latestKnownSeekId(messages, allSeeks())", app)

    def test_v070_productivity_entrypoints_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        self.assertIn("deepseek-infra.draft", app)
        self.assertIn("function forkConversationFromMessage(messageId)", app)
        self.assertIn("data-branch-from-message", app)
        self.assertIn("function quoteMessageForReply(messageId)", app)
        self.assertIn('id="selectionPopover"', html)
        self.assertIn("data-selection-action=\"quote\"", html)
        self.assertNotIn("data-quote-message", app)
        self.assertIn("function scheduleReminder(reminder)", app)
        self.assertIn('apiFetch("/api/reminders"', app)
        self.assertIn('apiFetch("/api/conversations/search"', app)
        self.assertIn("function toggleConversationFavorite(id)", app)
        self.assertIn("function editConversationTags(id)", app)
        self.assertIn('id="draftRestore"', html)
        self.assertIn('id="quotePreview"', html)
        self.assertIn('id="historySearchInput"', html)
        self.assertIn(".history-menu-button", css)
        self.assertIn('data.type !== "show_reminder"', sw)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v071_project_library_and_citations_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        markdown = Path("static/modules/markdown.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        normalize = Path("static/modules/normalize.js").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        self.assertIn('id="projectButton"', html)
        self.assertIn('id="projectPanel"', html)
        self.assertIn('id="activeProjectRow"', html)
        self.assertIn('id="projectUploadInput"', html)
        self.assertIn(".pptx,.epub", html)
        self.assertIn("activeProjectSnapshot()", app)
        self.assertIn("projectAttachments: normalizeProjectAttachments(project.documents || [])", app)
        self.assertIn('apiFetch("/api/projects"', app)
        self.assertIn("/api/project-files?projectId=", app)
        self.assertIn('apiFetch("/api/file-chunk"', app)
        self.assertIn('apiFetch("/api/file-reader"', app)
        self.assertIn('apiFetch("/api/file-page-text"', app)
        self.assertIn("/api/file-page-image", app)
        self.assertIn("/api/file-page-layout", app)
        self.assertIn("/api/file-page-search", app)
        self.assertIn("/api/file-source", app)
        self.assertIn("新窗口打开", app)
        self.assertIn("翻译全文", app)
        self.assertIn("截图提问", app)
        self.assertIn("file-original-toolbar", app)
        self.assertIn("file-original-sidebar", app)
        self.assertIn("file-original-capture-layer", app)
        self.assertIn("file-original-region-toolbar", app)
        self.assertIn("file-original-text-layer", app)
        self.assertIn("文字层", app)
        self.assertIn("originalReaderFrameUrl", app)
        self.assertIn("renderOriginalPdfViewer", app)
        self.assertIn("renderOriginalReaderFooter", app)
        self.assertIn('dataset.readerRole = "originalViewerCard"', app)
        self.assertIn('dataset.readerRole = "originalViewerFooter"', app)
        self.assertIn("renderOriginalPdfPageStack", app)
        self.assertIn("filePageImageUrl", app)
        self.assertIn("filePageThumbnailUrl", app)
        self.assertIn("filePageLayoutUrl", app)
        self.assertIn("filePageSearchUrl", app)
        self.assertIn("renderOriginalPageTextOverlay", app)
        self.assertIn("renderOriginalSearchPanel", app)
        self.assertIn("renderOriginalMoreMenu", app)
        self.assertIn("toggleOriginalMoreMenu", app)
        self.assertIn("createOriginalToolbarLink", app)
        self.assertIn('left.dataset.readerRole = "pdfToolbarLeft"', app)
        self.assertIn('center.dataset.readerRole = "pdfToolbarCenter"', app)
        self.assertIn('right.dataset.readerRole = "pdfToolbarRight"', app)
        self.assertIn("file-original-pdf-command-button", app)
        self.assertIn("file-original-pdf-icon-link", app)
        self.assertIn('text: "翻译全文"', app)
        self.assertIn('text: "截图提问"', app)
        self.assertIn('createOriginalToolbarLink("新窗口打开"', app)
        self.assertIn('createOriginalToolbarLink("下载原文件"', app)
        self.assertIn('role: "fitPage"', app)
        self.assertIn("setOriginalReaderZoom(100)", app)
        self.assertIn("createOriginalToolbarSeparator", app)
        self.assertIn('originalToolbarIcon("more")', app)
        self.assertIn("handleOriginalPageInputSubmit", app)
        self.assertIn("performOriginalSearch", app)
        self.assertIn("jumpToOriginalSearchResult", app)
        self.assertIn("scrollOriginalCurrentSearchMatchIntoView", app)
        self.assertIn("onOriginalPdfStageScroll", app)
        self.assertIn("syncOriginalReaderPageFromScroll", app)
        self.assertIn("scrollOriginalPdfPageIntoView", app)
        self.assertIn("syncOriginalPdfPageWidths", app)
        self.assertIn("originalPageLayouts", app)
        self.assertIn("originalPageLayoutRequests", app)
        self.assertIn("prefetchOriginalPageLayouts", app)
        self.assertIn("originalCurrentPageImage", app)
        self.assertIn("syncOriginalCaptureLayerBounds", app)
        self.assertIn("originalCaptureTargetRect", app)
        self.assertIn('layer.style.inset = "auto"', app)
        self.assertIn("stage.scrollLeft + imageRect.left - stageRect.left", app)
        self.assertIn("imageRect.left + (region.left / 100) * imageRect.width", app)
        self.assertIn("originalWordMatchesCurrentSearchTarget", app)
        self.assertIn("showOriginalPageSelectionToolbar", app)
        self.assertIn("clearOriginalInlineSelection", app)
        self.assertIn("stepOriginalReaderPage", app)
        self.assertIn("setOriginalReaderPage", app)
        self.assertIn("modifier && key.toLowerCase() === \"f\"", app)
        self.assertIn("key === \"PageDown\"", app)
        self.assertIn("key === \"PageUp\"", app)
        self.assertIn("zoomOriginalReader(10)", app)
        self.assertIn("zoomOriginalReader(-10)", app)
        self.assertIn("clearOriginalCaptureRegion();", app)
        self.assertIn("runOriginalRegionAction", app)
        self.assertIn("runOriginalTextAction", app)
        self.assertIn("originalRegionImageAttachment", app)
        self.assertIn('button.classList.add("has-chevron")', app)
        self.assertIn('button.setAttribute("aria-haspopup", "menu")', app)
        self.assertIn("renderOriginalTranslateMenu", app)
        self.assertIn("renderOriginalDocumentTranslateControl", app)
        self.assertIn('menu.dataset.readerRole = "documentTranslateMenu"', app)
        self.assertIn('menu.dataset.readerRole = source === "region" ? "regionTranslateMenu" : "textTranslateMenu"', app)
        self.assertIn("originalTranslateOptions", app)
        self.assertIn("originalDocumentTranslatePrompt", app)
        self.assertIn("originalTextTranslatePrompt", app)
        self.assertIn("originalRegionTranslatePrompt", app)
        self.assertIn('["ask", "问问 DeepSeek"]', app)
        self.assertIn("syncFileReaderComposerTools", app)
        self.assertIn("file-reader-composer-tools", app + css)
        self.assertIn("renderFileReaderComposerMoreMenu", app)
        self.assertIn('menu.dataset.readerRole = "composerMoreMenu"', app)
        self.assertIn("renderFileReaderWorkspaceMoreMenu", app)
        self.assertIn('menu.dataset.readerRole = "workspaceMoreMenu"', app)
        self.assertIn('button.setAttribute("aria-expanded", "false")', app)
        self.assertIn('document.addEventListener("click", onDocumentClickCloseReaderMenus)', app)
        self.assertIn("function toggleFileReaderFloatingMenu", app)
        self.assertIn("function closeFileReaderFloatingMenus", app)
        self.assertIn("function closeOpenReaderMenus", app)
        self.assertIn("function menuKeyboardItems(menu)", app)
        self.assertIn("function handleMenuKeyboard(event, menu", app)
        self.assertIn("function focusFirstMenuItem(menu)", app)
        self.assertIn("focusFirstMenuItem(menu)", app)
        self.assertIn("handleMenuKeyboard(event, menu", app)
        self.assertIn('moreButton.setAttribute("aria-haspopup", "menu")', app)
        self.assertIn('more.setAttribute("aria-expanded", String(Boolean(reader.originalMoreOpen)))', app)
        self.assertIn("if (event.defaultPrevented) return;", app)
        self.assertIn("closeOpenReaderMenus()", app)
        self.assertIn("chevronDown:", app)
        self.assertIn("syncFileReaderComposerInputState", app)
        self.assertIn("file-reader-composer-has-input", app + css)
        self.assertIn("body.file-reader-workspace-open:not(.file-reader-composer-has-input) .send-button", css)
        self.assertIn(".file-reader-composer-more-menu", css)
        self.assertIn(".file-reader-workspace-more-menu", css)
        self.assertIn("fileReaderWorkspaceDisplayTitle", app)
        self.assertIn('replace(/[_\\s]+/g, "-")', app)
        self.assertIn("深入研究", app)
        self.assertNotIn("问问附件", app)
        self.assertIn("appendPromptToComposer", app)
        self.assertIn("quoteOriginalReaderSelection", app)
        self.assertIn("selectedOriginalPageText", app)
        self.assertIn("selectedOriginalInlineText", app)
        self.assertIn("originalCaptureRegion", app)
        self.assertIn("originalMoreOpen", app)
        self.assertIn("original-mode", app)
        self.assertNotIn('iframe.setAttribute("sandbox"', app)
        self.assertNotIn('fragment.set("toolbar", "0")', app)
        self.assertIn("fileReaderQuoteButton", app)
        self.assertIn("sourceAvailable", app)
        self.assertIn("pageCount", app)
        self.assertIn("pageCount", normalize)
        self.assertIn("data-project-document-read", app)
        self.assertIn("data-message-attachment", app)
        self.assertIn('id="fileReaderToolbar"', html)
        self.assertIn(".file-original-toolbar", css)
        self.assertIn(".file-original-sidebar", css)
        self.assertIn(".file-original-sidebar.pdf-thumbnails", css)
        self.assertIn(".file-original-page-thumb-preview", css)
        self.assertIn(".file-original-capture-layer", css)
        self.assertIn(".file-original-region-toolbar", css)
        self.assertIn(".file-original-action-button-chevron", css)
        self.assertIn(".file-original-translate-menu", css)
        self.assertIn(".file-original-pdf-command-wrap", css)
        self.assertIn(".file-original-text-layer", css)
        self.assertIn(".file-original-page-text-content", css)
        self.assertIn(".file-original-reader", css)
        self.assertIn(".file-original-viewer-card", css)
        self.assertIn(".file-original-preview", css)
        self.assertIn(".file-original-pdf-shell", css)
        self.assertIn(".file-original-page-stack", css)
        self.assertIn(".file-original-page-frame", css)
        self.assertIn(".file-original-more-menu", css)
        self.assertIn(".file-original-page-image", css)
        self.assertIn(".file-original-page-text-overlay", css)
        self.assertIn(".file-original-selection-toolbar", css)
        self.assertIn(".file-original-pdf-inner-toolbar", css)
        self.assertIn(".file-original-pdf-separator", css)
        self.assertIn(".file-original-pdf-command-button-chevron::after", css)
        self.assertIn(".file-original-pdf-command-button", css)
        self.assertIn(".file-original-pdf-command-button > span:not(.file-original-toolbar-icon)", css)
        self.assertIn(".file-original-pdf-icon-link", css)
        self.assertIn(".file-original-pdf-page-form", css)
        self.assertIn(".file-original-search-panel", css)
        self.assertIn("scrollbar-gutter: stable", css)
        self.assertIn(".file-original-pdf-stage::-webkit-scrollbar-thumb", css)
        self.assertIn("border: 1px solid #edf0f3", css)
        self.assertIn(".file-original-page-text-word.search-match", css)
        self.assertIn(".file-original-page-text-word.current-search-match", css)
        self.assertIn(".file-preview-panel.original-mode", css)
        self.assertIn("fullscreen-mode", css)
        self.assertIn(".file-reader-chunk", css)
        self.assertIn("openCitationForMessage", app)
        self.assertIn('data-citation="${escapeAttribute(id)}"', markdown)
        self.assertIn(".citation-pin", css)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v073_memory_suggestions_and_scopes_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        memory = Path("deepseek_infra/infra/data/memory.py").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        self.assertIn('event.type === "memory_suggestion"', app)
        self.assertIn("function memoryScopeForContext", app)
        self.assertIn('"memory_suggestion"', client)
        self.assertIn('"name": "suggest_memory"', tools)
        self.assertIn("def detect_memory_conflicts", memory)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v074_ui_ux_enhancements_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        markdown = Path("static/modules/markdown.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        self.assertIn('id="commandPalette"', html)
        self.assertIn("function onGlobalKeydown", app)
        escape_block_start = app.index('if (key === "Escape")')
        self.assertLess(
            app.index("if (isConfirmDialogOpen())", escape_block_start),
            app.index("if (closeOpenReaderMenus())", escape_block_start),
        )
        self.assertLess(
            app.index("if (isCommandPaletteOpen())", escape_block_start),
            app.index("if (closeOpenReaderMenus())", escape_block_start),
        )
        self.assertLess(
            app.index("if (closeOpenReaderMenus())", escape_block_start),
            app.index("if (isSelectionPopoverOpen())", escape_block_start),
        )
        self.assertIn("function openCommandPalette", app)
        self.assertIn("commandPaletteList.addEventListener(\"keydown\", onCommandPaletteListKeydown)", app)
        self.assertIn("function commandPaletteButtons()", app)
        self.assertIn("function focusCommandPaletteItem(index)", app)
        self.assertIn("function onCommandPaletteListKeydown(event)", app)
        self.assertIn('if (event.key === "ArrowDown")', app)
        self.assertIn("closeCommandPalette();", app)
        self.assertIn("function syncPanelTriggerStates()", app)
        self.assertIn("function setPanelTriggerState(control, panel", app)
        self.assertIn('control.setAttribute("aria-controls", panel.id || "")', app)
        self.assertIn('control.setAttribute("aria-expanded", String(Boolean(expanded)))', app)
        self.assertIn("setPanelTriggerState(activeSeekChip, seekPanel", app)
        self.assertIn("setPanelTriggerState(activeProjectChip, projectPanel", app)
        self.assertIn('button.setAttribute("aria-expanded", String(Boolean(isActivityPanelOpen()', app)
        self.assertIn('activeSearchMessageId: ""', app)
        self.assertIn('activeDiagnosticsMessageId: ""', app)
        self.assertIn('function openSearchPanel(search, { messageId = "" } = {})', app)
        self.assertIn('state.activeSearchMessageId = String(messageId || "")', app)
        self.assertIn('viewAll.setAttribute("aria-controls", "searchPanel")', app)
        self.assertIn('button[data-search-results]', app)
        self.assertIn('button[data-diagnostics-message]', app)
        self.assertIn("state.activeDiagnosticsMessageId = message?.id", app)
        self.assertIn("activateFocusTrap(seekPanel);\n  syncBackdrop();", app)
        self.assertIn("activateFocusTrap(projectPanel);\n  syncBackdrop();", app)
        self.assertIn("activateFocusTrap(settingsPanel);\n  syncBackdrop();", app)
        self.assertIn("activateFocusTrap(activityPanel);\n    syncBackdrop();", app)
        self.assertIn('id="offlineBanner"', html)
        self.assertIn("state.offlineMode = true", app)
        self.assertIn('id="themeStyleSelect"', html)
        self.assertIn('id="themeModeSelect"', html)
        self.assertIn("function applyAppearanceSettings", app)
        self.assertIn("data-code-action=\"toggle-collapse\"", markdown)
        self.assertIn("data-math-action=\"copy\"", markdown)
        self.assertIn("data-chart-action=\"bar\"", markdown)
        self.assertIn("function hydrateMermaidDiagrams", markdown)
        self.assertIn(".command-palette", css)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v080_voice_and_share_target_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        manifest = Path("static/manifest.webmanifest").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")

        self.assertIn('id="voiceInputButton"', html)
        self.assertIn("function startVoiceInput()", app)
        self.assertIn("function toggleSpeakMessage(messageId)", app)
        self.assertIn("data-speak-message", app)
        self.assertIn("function consumeShareTarget()", app)
        self.assertIn(".voice-button.listening", css)
        self.assertIn('"share_target"', manifest)
        self.assertIn('"/share-target"', server)
        self.assertIn('"/api/share-target"', server)

    def test_v081_share_target_and_speech_fixes_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        speech = Path("static/modules/speech_text.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        manifest = Path("static/manifest.webmanifest").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")

        self.assertIn("SHARE_TARGET_TTL_SECONDS = 30 * 60", server)
        self.assertIn("require_allowed_host(request)", server)
        self.assertIn("function confirmShareTarget(share)", app)
        self.assertIn("function speechChunks(text)", speech)
        self.assertIn("function preferredSpeechVoice(lang", speech)
        self.assertIn("公式略", speech)
        self.assertIn('id="voiceLanguageSelect"', html)
        self.assertIn("application/vnd.openxmlformats-officedocument.wordprocessingml.document", manifest)
        self.assertIn("application/epub+zip", manifest)

    def test_v082_selection_quote_entrypoints_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        format_module = Path("static/modules/format.js").read_text(encoding="utf-8")

        self.assertIn('id="quoteSelectionButton"', html)
        self.assertIn("function selectedAssistantQuoteCandidate()", app)
        self.assertIn("function selectedMathSources(range, bubble)", app)
        self.assertIn("function setFragmentQuote(messageId, fragment)", app)
        self.assertIn("data-quote-origin", app)
        self.assertIn("quoteDraft?.isFragment", format_module)
        self.assertIn(".selection-quote-button.active", css)

    def test_v083_pwa_icons_and_favicons_are_present(self) -> None:
        html = Path("static/index.html").read_text(encoding="utf-8")
        manifest = json.loads(Path("static/manifest.webmanifest").read_text(encoding="utf-8"))
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        app = Path("deepseek_infra/app.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")

        self.assertIn('<link rel="icon" href="/icons/favicon.svg" type="image/svg+xml" />', html)
        self.assertIn('<link rel="icon" href="/icons/favicon-32x32.png" sizes="32x32" type="image/png" />', html)
        self.assertIn('<link rel="icon" href="/favicon.ico" sizes="any" />', html)
        self.assertIn('<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png" />', html)

        icons = manifest.get("icons")
        self.assertIsInstance(icons, list)
        icon_by_src = {icon.get("src"): icon for icon in icons if isinstance(icon, dict)}
        for src in [
            "/icons/icon.svg",
            "/icons/pwa-192x192.png",
            "/icons/pwa-512x512.png",
            "/icons/maskable-192x192.png",
            "/icons/maskable-512x512.png",
        ]:
            with self.subTest(src=src):
                self.assertIn(src, icon_by_src)
        self.assertEqual(icon_by_src["/icons/maskable-512x512.png"]["purpose"], "maskable")
        self.assertEqual(icon_by_src["/icons/pwa-512x512.png"]["purpose"], "any")

        expected_sizes = {
            "static/icons/pwa-192x192.png": (192, 192),
            "static/icons/pwa-512x512.png": (512, 512),
            "static/icons/maskable-192x192.png": (192, 192),
            "static/icons/maskable-512x512.png": (512, 512),
            "static/icons/apple-touch-icon.png": (180, 180),
            "static/icons/badge-96x96.png": (96, 96),
            "static/icons/favicon-32x32.png": (32, 32),
            "static/icons/favicon-16x16.png": (16, 16),
        }
        for path, size in expected_sizes.items():
            with self.subTest(path=path):
                self.assertEqual(png_dimensions(Path(path)), size)
        self.assertTrue(Path("static/icons/icon.svg").is_file())
        self.assertTrue(Path("static/icons/favicon.svg").is_file())
        self.assertEqual(Path("static/favicon.ico").read_bytes()[:4], b"\x00\x00\x01\x00")

        for cached in [
            '"/favicon.ico"',
            '"/icons/apple-touch-icon.png"',
            '"/icons/favicon.svg"',
            '"/icons/maskable-512x512.png"',
            '"/icons/pwa-512x512.png"',
        ]:
            with self.subTest(cached=cached):
                self.assertIn(cached, sw)
        self.assertIn('icon: "/icons/pwa-192x192.png"', sw)
        self.assertIn('badge: "/icons/badge-96x96.png"', sw)
        self.assertIn('mimetypes.add_type("image/svg+xml", ".svg")', app)
        self.assertIn('".svg": "image/svg+xml"', server)

    def test_v084_motion_feedback_and_stream_throttle_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for token in [
            "--motion-fast",
            "--motion-base",
            "--motion-slow",
            "--ease-out",
            "--ease-standard",
            "prefers-reduced-motion: reduce",
            ".icon-button:not(:disabled):active",
            ".message[data-fresh=\"true\"]",
            "@keyframes msg-in",
            "@keyframes toast-in",
            ".backdrop.open",
            ".history-panel.open",
            ".segmented[data-active-mode=\"expert\"]::before",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, css)

        for token in [
            "const freshMessageIds = new Set()",
            "const pendingStreamingMessageIds = new Set()",
            "function markMessageFresh(message)",
            "function decorateFreshMessage(node, messageId)",
            "requestAnimationFrame(flushStreamingMessageUpdates)",
            "function renderStreamingMessage(message)",
            "function setBackdropVisible(visible)",
            "function removeWithMotion(node)",
            "modelTabs.dataset.activeMode",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v085_selection_quote_reasoning_and_composer_focus_fixes_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        for token in [
            "selectionQuoteLocked: null",
            "lastValidQuoteCandidate: null",
            "selectionQuoteActionHandledAt: 0",
            "function captureSelectionSnapshot(event)",
            "function scheduleSelectionRefresh()",
            'document.addEventListener("pointerup", scheduleSelectionRefresh, { passive: true })',
            "function chatBubbleForSelection(selection, range)",
            "function chatBubblesForTextRange(range)",
            "function rangeIntersectsTextNode(range, node)",
            "function handleSelectionPointerActivation(event, run)",
            "event.type !== \"touchstart\"",
            ".message[data-message-id] .bubble",
            'quoteSelectionButton.addEventListener("pointerup", quoteSelectionButtonPointerUp)',
            'quoteSelectionButton.addEventListener("touchstart", captureSelectionSnapshot, { passive: false })',
            "Boolean((state.selectionQuoteCandidate || state.lastValidQuoteCandidate)?.text)",
            "state.selectionQuoteLocked || state.selectionQuoteCandidate || selectedAssistantQuoteCandidate() || state.lastValidQuoteCandidate",
            "![\"assistant\", \"user\"].includes(message.role)",
            "function streamingSummaryLabel(message)",
            'if (phase === "answering") return "生成中";',
            "clearSelectionQuoteState({ render: false })",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        for token in [
            "textarea:focus-visible",
            ".composer:focus-within",
            "border-color: var(--accent)",
            "user-select: none",
            "-webkit-user-select: none",
            ".message .content",
            "user-select: text",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, css)
        self.assertNotIn("button:focus-visible,\ntextarea:focus-visible", css)

        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("## [0.8.5]", changelog)

    def test_v086_reasoning_timer_and_busy_interactions_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")

        for token in [
            "function markReasoningEnded(message)",
            "function markAnswerStarted(message)",
            "markAnswerStarted(assistantMessage)",
            "function reasoningElapsedSeconds(message)",
            "if (message.streaming && startedAt)",
            "Number(message.reasoningEndedAt) || Number(message.completedAt)",
            "reasoningEndedAt: Number(value.reasoningEndedAt) || undefined",
            "delete assistantMessage.reasoningEndedAt",
            "voiceInputButton.disabled = !supported || state.offlineMode",
            "const enabled = Boolean((state.selectionQuoteCandidate || state.lastValidQuoteCandidate)?.text) && !state.offlineMode",
            "sendButton.hidden = isBusy",
            "regenerateButton.disabled = state.busy",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        for removed in [
            "promptInput.disabled = isBusy",
            "fileInput.disabled = isBusy",
            "attachmentButton.setAttribute(\"aria-disabled\", String(isBusy))",
            "speakButton.disabled = state.busy",
            "quoteButton.disabled = state.busy",
        ]:
            with self.subTest(removed=removed):
                self.assertNotIn(removed, app)

        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [0.8.6]", changelog)
        self.assertIn("适用版本：v2.1.7。", api)

    def test_v170_streaming_phase_labels_and_timer_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for token in [
            'const streamPhases = new Set(["thinking", "tool", "searching", "agent", "answering"])',
            "function setAssistantStreamPhase(message, phase)",
            "function clearAssistantStreamPhase(message)",
            "function streamPhaseForSystemNote(text, message)",
            "function streamingSummaryLabel(message)",
            "function streamingActivityPlaceholder(message)",
            'if (phase === "tool") return "调用工具中";',
            'if (phase === "answering") return "生成中";',
            'if (phase === "tool") return "正在调用本地工具...";',
            'if (phase === "answering") return "正在输出正文...";',
            "startReasoningTick();",
            "if (message.streaming && startedAt)",
            "clearAssistantStreamPhase(message)",
            "delete stored.streamPhase",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("图片视觉理解", readme)
        self.assertIn("## [1.7.0]", changelog)

    def test_v090_sidebar_history_layout_is_present(self) -> None:
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        for token in [
            'id="historyButton" type="button" aria-label="打开侧边栏" title="侧边栏"',
            '<nav class="history-nav"',
            'id="projectButton"',
            'id="exportChatButton"',
            'id="seekButton"',
            'id="historyNewChatButton"',
            'class="history-row-button history-row-button--primary"',
            'class="brand-mark history-brand"',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, html)
        self.assertEqual(html.count('id="historyButton"'), 1)
        self.assertEqual(html.count('id="projectButton"'), 1)
        self.assertEqual(html.count('id="seekButton"'), 1)
        self.assertEqual(html.count('id="exportChatButton"'), 1)
        self.assertEqual(html.count('id="historyNewChatButton"'), 1)
        # v1.2.1+ 閲嶆瀯锛氬師 pill 鍐呯殑 newChatButton 宸插悎骞跺埌 historyNewChatButton锛?
        # 鍘?sidebar 鍐呯殑 closeHistoryButton 宸茬Щ闄わ紙鎶樺彔 sidebar 鏀圭敤澶栭儴 nav 鐨?historyButton锛?
        self.assertNotIn('id="newChatButton"', html)
        self.assertNotIn('id="closeHistoryButton"', html)

        for token in [
            ".history-nav",
            ".history-row-button",
            ".history-row-button--primary",
            ".history-panel > .history-nav",
            "overflow: hidden",
            ".settings-panel",
            "overflow-y: auto",
            ".history-list",
            "flex: 1",
            "min-height: 40px",
            ".history-meta",
            "display: none",
            ".history-footer",
            "flex-shrink: 0",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, css)
        self.assertNotIn(".history-footer {\n  position: sticky", css)
        self.assertNotIn("background: rgba(255, 255, 255, 0.96)", css)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("## [0.9.0]", changelog)

    def test_v091_tool_calling_improvements_are_present(self) -> None:
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")

        for token in [
            "REASONING_EFFORTS",
            "TOOL_PARALLEL_SYSTEM_HINT",
            'request_body["top_p"] = 1.0',
            'normalize_reasoning_effort(payload.get("reasoningEffort"))',
            'assistant_payload["reasoning_content"] = str(reasoning)',
            '{"content": round_content, "reasoning_content": round_reasoning}',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, client)

        for token in [
            "MAX_TOOL_ROUNDS = 3",
            '"strict": True',
            '"additionalProperties": False',
            "Evaluate a side-effect-free Python math expression.",
            "Fetch readable text from one public http(s) URL.",
            '"pattern": "^(global|project:[A-Za-z0-9_-]{1,64}|seek:[A-Za-z0-9_-]{1,64})$"',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, tools)

        for token in [
            'reasoningEffort: "deepseek-infra.reasoning-effort"',
            "const reasoningEffortSelect = document.querySelector(\"#reasoningEffortSelect\")",
            "function normalizeReasoningEffort(value)",
            "reasoningEffort: state.reasoningEffort",
            "reasoningEffort: assistantMessage.reasoningEffort",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        self.assertIn('id="reasoningEffortSelect"', html)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [0.9.1]", changelog)
        self.assertIn("适用版本：v2.1.7。", api)

    def test_v092_upload_limits_and_frontend_interactions_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        normalize = Path("static/modules/normalize.js").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")

        for token in [
            "upload_file_max_bytes: int = 200_000_000",
            "upload_max_bytes: int = 220_000_000",
            "MAX_UPLOAD_FILE_BYTES = settings.files.upload_file_max_bytes",
            "MAX_UPLOAD_BYTES = settings.files.upload_max_bytes",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, config)

        for token in [
            '"uploadLimits": {',
            '"fileMaxBytes": MAX_UPLOAD_FILE_BYTES',
            '"requestMaxBytes": MAX_UPLOAD_BYTES',
            "partsize_limit=MAX_UPLOAD_FILE_BYTES",
            "format_upload_limit(MAX_UPLOAD_FILE_BYTES)",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, server)

        for token in [
            "defaultUploadLimits",
            "function onPromptPaste(event)",
            "function uploadPendingAttachmentFiles",
            "function validatedUploadFiles",
            "function decorateUploadItemsWithImagePreviews",
            "function openImageLightbox",
            "function exportSingleAssistantMessage",
            "function confirmAction",
            "function activateFocusTrap",
            "function openShortcutPanel",
            "window.visualViewport",
            "distance <= 120",
            "Math.min(viewportHeight * 0.5, 360)",
            "data-feedback-message",
            "toast-action",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        for token in [
            'id="dropOverlay"',
            'id="statusLiveRegion"',
            'id="alertLiveRegion"',
            'id="shortcutPanel"',
            'id="confirmDialog"',
            'id="imageLightbox"',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, html)

        for token in [
            ".drop-overlay",
            ".shortcut-panel",
            ".confirm-dialog",
            ".image-lightbox",
            ".message-attachment.image",
            ".attachment-thumb",
            ".assistant-more-menu",
            ".toast-action",
            "var(--keyboard-inset, 0px)",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, css)

        self.assertIn("thumbnail: String(value.thumbnail || \"\")", normalize)
        self.assertIn("imagePreview: String(value.imagePreview || \"\")", normalize)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [0.9.4]", changelog)
        self.assertIn("uploadLimits", api)

    def test_v093_model_driven_search_and_selection_popover_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        search = Path("deepseek_infra/infra/tool_runtime/search.py").read_text(encoding="utf-8")
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")

        for token in [
            '"name": "web_search"',
            '"strict": True',
            '"intent"',
            "web_search_callback",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, tools)

        for token in [
            "def search_single_round(",
            "def compact_search_tool_result(",
            "def normalize_search_query_text(",
            "def search_queries_for(",
            "variants_by_intent",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, search)

        for token in [
            "WEB_SEARCH_SYSTEM_HINT",
            "def tools_for_payload(",
            "def web_search_callback_for_turn(",
            "forced_search_mode(payload)",
            "search_tool_enabled(payload)",
            "tools_enabled and search_tool_enabled(payload)",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, client)

        for token in [
            'id="selectionPopover"',
            'data-selection-action="quote"',
            'data-selection-action="copy"',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, html)

        for token in [
            "const selectionPopover = document.querySelector",
            "function positionSelectionPopover(candidate)",
            "function setupSelectionPopover()",
            "navigator.maxTouchPoints > 0",
            "data-quote-message",
            "由模型决定本轮是否联网",
            "search-round-count",
        ]:
            with self.subTest(token=token):
                if token == "data-quote-message":
                    self.assertNotIn(token, app)
                else:
                    self.assertIn(token, app)

        self.assertIn(".selection-popover", css)
        self.assertIn(".search-round-count", css)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)

    def test_v094_citations_titles_peek_and_timeline_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        markdown = Path("static/modules/markdown.js").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        search = Path("deepseek_infra/infra/tool_runtime/search.py").read_text(encoding="utf-8")
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        title = Path("deepseek_infra/infra/gateway/title_generator.py").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        for token in [
            "citation_offset",
            '"cite": f"[^',
            '"citation_id"',
            "[^Wn]",
            "format_search_context",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, search)

        for token in ["Cite web search results", "citation_counter", "citation_offset=citation_counter"]:
            with self.subTest(token=token):
                self.assertIn(token, client)

        self.assertIn("Each result includes a cite field", tools)
        self.assertIn('"/api/title"', server)
        self.assertIn("def generate_title_payload", title)
        self.assertIn("你是一个对话标题生成器", title)
        self.assertIn("用户首轮提问", title)
        self.assertIn("RATE_LIMITED", title)

        for token in [
            "function openCitationForMessage",
            "function webCitationResults",
            "function appendTimelineReasoning",
            "function mergeSearchIntoTimeline",
            "function renderInlineSearchRound",
            "function maybeAutoGenerateTitle",
            "function regenerateTitle",
            "maybeAutoGenerateTitle(conversation, conversation.messages || [])",
            "peekClickLockUntil",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)
        # v1.2.7锛歯ormalizeTimeline / search 闄愰暱鏍￠獙浠?chat.js 鎶藉埌浜?agent_timeline.js锛?
        # 杩欓噷鍚屾椂纭鍑芥暟宸茶縼鍑恒€佸苟涓?chat.js 浠嶇劧 import 鐫€瀹冿紝鎵嶇畻瀹屾暣杩佺Щ銆?
        agent_timeline = Path("static/modules/agent_timeline.js").read_text(encoding="utf-8")
        self.assertIn("export function normalizeTimeline", agent_timeline)
        self.assertIn("snippet.slice(0, 200)", agent_timeline)
        self.assertIn("normalizeTimeline,", app)
        self.assertIn('from "./agent_timeline.js"', app)

        self.assertIn("citation-web", markdown)
        self.assertIn(".reasoning-search-round", css)
        self.assertIn(".history-title.is-pending-title", css)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [0.9.4]", changelog)

    def test_v096_search_hotfix_and_tool_expansion_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        search = Path("deepseek_infra/infra/tool_runtime/search.py").read_text(encoding="utf-8")
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        security = Path("docs/SECURITY.md").read_text(encoding="utf-8")

        for token in [
            "function settleStuckSearchSteps",
            "function settleStuckSearchData",
            "settleStuckSearchSteps(assistantMessage)",
            'document.createElementNS(svgNS, "svg")',
            'svg.setAttribute("width", "16")',
            'svg.setAttribute("height", "16")',
            'svg.setAttribute("fill", "none")',
            'svg.setAttribute("stroke", "currentColor")',
            'const seen = new Set();',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        self.assertIn(".reasoning-search-icon svg", css)
        self.assertIn("display: block", css)
        self.assertNotIn(".reasoning-search-icon svg {\n  width: 16px", css)

        for token in [
            "def simplified_retry_query",
            "def should_retry_tavily_error",
            "def search_tavily_with_retry",
            '"retried": bool(round_data.get("retried"))',
            '"retryQuery": round_data.get("retryQuery") or ""',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, search)

        self.assertIn("预取搜索失败", client)
        self.assertIn('not in {"web_search", "compare_search_results"}', client)

        for token in [
            "SERIAL_TOOL_NAMES",
            "ThreadPoolExecutor",
            '"name": "create_reminder"',
            '"name": "list_reminders"',
            '"name": "recall_memory"',
            '"name": "forget_memory"',
            '"name": "list_project_files"',
            '"name": "read_file_chunk"',
            '"name": "data_transform"',
            '"name": "generate_chart"',
            '"name": "compare_search_results"',
            "def data_transform(",
            "def compare_search_results(",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, tools)

        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [0.9.6]", changelog)
        self.assertIn("适用版本：v2.1.7。", api)
        self.assertIn("适用版本：v2.1.7。", security)

    def test_v111_visual_theme_system_is_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        normalize = Path("static/modules/normalize.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        frontend_docs = Path("docs/FRONTEND_MODULES.md").read_text(encoding="utf-8")
        security = Path("docs/SECURITY.md").read_text(encoding="utf-8")

        for token in [
            'data-theme="chatgpt"',
            'data-mode="system"',
            'id="themeStyleSelect"',
            'id="themeModeSelect"',
            "deepseek-infra.theme-style",
            "deepseek-infra.theme-mode",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, html)

        for token in [
            "function normalizeThemeStyle(value)",
            "function normalizeThemeMode(value)",
            '"chatgpt", "linear", "notion", "arc"',
            '"system", "light", "dark"',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, normalize)

        for token in [
            "state.themeStyle",
            "state.themeMode",
            "function syncMetaThemeColor",
            "root.dataset.theme = state.themeStyle",
            "root.dataset.mode = state.themeMode",
            "localStorage.setItem(storageKeys.themeMode, state.themeMode)",
            "localStorage.getItem(storageKeys.theme)",
            'showToast("已复制代码", { tone: "success" })',
            'showToast("复制失败，请长按代码手动复制", { tone: "error" })',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)
        self.assertNotIn("state.theme =", app)
        self.assertNotIn("#themeSelect", app)

        for token in [
            "--bg-base",
            "--bg-elevated",
            "--surface-user",
            "--surface-assistant",
            "--text-primary",
            "--border-default",
            "--accent-soft",
            "--font-body",
            "--font-mono",
            "--bg: var(--bg-base)",
            "--surface: var(--bg-elevated)",
            "--brand: var(--accent)",
            ':root[data-theme="chatgpt"][data-mode="light"]',
            ':root[data-theme="linear"][data-mode="dark"]',
            ':root[data-theme="notion"][data-mode="system"]',
            ':root[data-theme="arc"][data-mode="dark"]',
            "@media (prefers-color-scheme: dark)",
            '--avatar-blue-bg',
            ':root[data-theme="arc"][data-mode="system"]',
            ".message.user .bubble",
            ".composer:focus-within",
            "color-mix(in srgb, var(--accent) 22%, transparent)",
            ".history-item.active::before",
            ":root[data-theme=\"arc\"] .history-menu",
            ".reasoning[open]",
            ".reasoning-search-round.status-error",
            ".reasoning-source-chip:hover",
            ".search-panel-result",
            ".diagnostics-row",
            ".seek-card.active .seek-avatar",
            ".code-action.copied",
            ".toast.is-error",
            ".toast.is-success",
            ".command-palette-item.is-active",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, css)
        self.assertNotIn(':root[data-theme="dark"]', css)

        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [1.2.2]", changelog)
        self.assertIn("normalizeThemeStyle", frontend_docs)
        self.assertIn("Google Fonts", security)

    def test_v111_search_round_recovery_is_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        search = Path("deepseek_infra/infra/tool_runtime/search.py").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        # v1.2.7锛歴earch round 鎭㈠閫昏緫鍒嗘暎鍦?chat.js锛坰ettleStuckSearchSteps锛夊拰
        # agent_timeline.js锛坣ormalizeTimeline 鐨?status fallback锛夛紝鍒嗗埆妫€鏌ャ€?
        self.assertIn('settleStuckSearchSteps(message, "搜索已中断")', app)
        self.assertIn("搜索未完成（页面已刷新或请求已中断）", app)
        agent_timeline = Path("static/modules/agent_timeline.js").read_text(encoding="utf-8")
        self.assertIn('rawStatus === "searching" ? "error" : rawStatus', agent_timeline)

        for token in [
            "variants_by_intent",
            "SEARCH_ROUND_LIMIT",
            "最新进展",
            "最多再补充 1 次 web_search",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, search)

        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [1.2.2]", changelog)

    def test_v115_agent_mode_and_search_limits_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        multi_agent = Path("deepseek_infra/infra/agent_runtime/multi_agent.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        markdown = Path("static/modules/markdown.js").read_text(encoding="utf-8")

        for token in [
            'agentMode: "deepseek-infra.agent-mode"',
            "state.agentMode",
            "function renderAgentModeButton()",
            "function toggleAgentTimelineStep",
            "agentDisplayMode: \"deepseek-infra.agent-display-mode\"",
            'if (event.type === "agent")',
            'if (event.type === "agent_reasoning")',
            'if (event.type === "agent_note")',
            "function renderInlineAgentStep",
            "reasoning-agent-thought",
            "reasoning-agent-toggle",
            "cloneJsonSafe(message.search)",
            "chatRequestTimeoutMs",
            "agentChatRequestTimeoutMs",
            "activityAutoDismissedMessageIds",
            "fallbackReasoningStepKey",
            "suppressAutoOpen",
            # v1.3.2锛歸orker Agent 杩愯涓嵆浣跨畝娲佹ā寮忎篃涓存椂鏄剧ず reasoning锛?
            # 淇 Leader 鈫?worker 鍒囨崲鍚庡彸渚ч潰鏉跨┖鐧斤紱鍏抽棴鎸夐挳淇濈暀 keepState 璁╂墜鍔ㄩ噸寮€绋冲畾銆?
            'const showLiveAgentInfo = status === "running"',
            'state.agentDisplayMode === "detailed" || showLiveAgentInfo',
            "closeActivityPanel({ keepState: true })",
            "function messageHasActivity(message)",
            "function activityTimelineSteps(message)",
            "function activityTimelineStepKey(step, index)",
            "function renderReasoningBlock(message)",
            "Boolean(message.agentMode && message.streaming)",
            "agentMode: Boolean(value.agentMode)",
            # v1.4.0锛氭€濊€冩爮妗岄潰渚ф爮鍜岀Щ鍔ㄧ details 鍏辩敤 syncReasoningBody锛?
            # 閬垮厤鐐瑰嚮渚ф爮鏃惰皟鐢ㄤ笉瀛樺湪鐨?buildReasoningBody锛屾垨绉诲姩绔覆鏌撶己灏?details 鏋勯€犲嚱鏁般€?
            "syncReasoningBody(body, message);",
            # v1.4.0锛欰ctivity 闈㈡澘涓嶆槸 chatLog 瀛愭爲锛汚gent 灞曞紑/閲嶈窇鍜屾悳绱㈡潵婧愬睍寮€
            # 蹇呴』鍦ㄩ潰鏉夸笂鍗曠嫭浜嬩欢濮旀墭锛屽惁鍒欐寜閽湅寰楀埌浣嗙偣涓嶅姩銆?
            'activityPanel.addEventListener("click", onActivityPanelClick)',
            "async function onActivityPanelClick(event)",
            "function searchPanelDataForMessage(message)",
            "function timelineSearchRoundsForPanel(message)",
            "openSearchPanelForMessage(searchButton.dataset.searchResults || state.activeActivityMessageId)",
            "toggleAgentTimelineStep(agentToggleButton.dataset.agentToggle || state.activeActivityMessageId",
            "await rerunAgentPhase(agentRerunButton.dataset.agentRerun || state.activeActivityMessageId",
            # v1.3.3锛歳unning 鍗＄墖鍦?text/reasoning/output/notes 鍏ㄧ┖鏃剁粰"姝ｅ湪鎬濊€冣€?鍗犱綅锛?
            # 閬垮厤 worker 鍒氳 emit 浣嗚繕娌?token 鐨勭灛闂达紝鐢ㄦ埛鐪嬪埌绋€鐤忓崱鐗囪鍒や负"鎵撲笉寮€"銆?
            '"reasoning-agent-note pending"',
            '"正在思考…"',
            "const requestTimeoutMs = (message.agentMode || state.agentMode) ? agentChatRequestTimeoutMs : chatRequestTimeoutMs",
            "agentMode: state.agentMode",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)
        # v1.2.7锛歛ppendTimelineAgent* 绯诲垪杩佸埌 agent_timeline.js锛?
        # 鍚屾椂妫€鏌ワ紙a锛夊嚱鏁板凡 export 鍒版柊妯″潡锛岋紙b锛塩hat.js 浠嶇劧 import 瀹冧滑銆?
        agent_timeline = Path("static/modules/agent_timeline.js").read_text(encoding="utf-8")
        for token in [
            "export function appendTimelineAgent",
            "export function appendTimelineAgentReasoning",
            "export function appendTimelineAgentNote",
            "export function appendTimelineAgentDelta",
            "export function agentExecutionReport",
            "AGENT_REPORT_PHASE_TITLES",
            "function normalizeDurationMs",
            "Number(null) is 0",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, agent_timeline)
        for token in [
            "appendTimelineAgent,",
            "appendTimelineAgentReasoning,",
            "appendTimelineAgentNote,",
            "appendTimelineAgentDelta,",
            "agentExecutionReport,",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        self.assertIn('id="agentModeButton"', html)
        self.assertIn('id="agentDisplayModeSelect"', html)
        self.assertIn(".reasoning-agent-step", css)
        self.assertIn(".reasoning-agent-toggle", css)
        self.assertIn(".reasoning-agent-note.pending", css)
        self.assertIn("复制 Agent 过程", app)
        self.assertIn("data-copy-agent-report", app)
        self.assertIn(".activity-panel-tools", css)
        self.assertNotIn("buildReasoningBody", app)
        self.assertIn("${summary.count} 个 Agent", app)
        self.assertIn(".agent-run-summary-item.status-error", css)
        self.assertIn("border-color: color-mix(in srgb, var(--danger) 36%, transparent)", css)
        self.assertIn("WEB_SEARCH_TURN_LIMIT = 15", client)
        self.assertIn("WEB_SEARCH_LIMIT_ERROR", client)
        self.assertIn("class SearchBudget", client)
        self.assertIn("class RequestCancelled", client)
        self.assertIn("MAX_TOOL_ROUNDS = 3", tools)
        self.assertIn("cancel_event", tools)
        self.assertIn("Run up to two related web_search queries", tools)
        self.assertIn("if len(cleaned_queries) >= 2", tools)
        self.assertIn("stream_multi_agent", server)
        self.assertIn("cancel_event = threading.Event()", server)
        self.assertIn("payload.get(\"agentMode\") is True", server)
        self.assertIn("MULTI_AGENT_TOTAL_SEARCH_LIMIT = 36", multi_agent)
        self.assertIn("MULTI_AGENT_PER_AGENT_SEARCH_LIMIT = 15", multi_agent)
        self.assertIn("MULTI_AGENT_TOOL_ROUNDS = 4", multi_agent)
        self.assertIn("MULTI_AGENT_TIMEOUT_SECONDS", multi_agent)
        self.assertIn("AGENT_TIMEOUT_SECONDS = MULTI_AGENT_TIMEOUT_SECONDS", multi_agent)
        self.assertIn("MIDDLE_PARALLEL_AGENT_IDS", multi_agent)
        self.assertIn("agent_durations_for_diagnostics", multi_agent)
        self.assertIn('"agentDurations": agent_durations_for_diagnostics(agent_outputs)', multi_agent)
        self.assertIn("agent_cache_for_diagnostics", multi_agent)
        self.assertIn('"agentCache": agent_cache_for_diagnostics(agent_outputs, synthesizer_usage or {})', multi_agent)
        self.assertIn("diagnostics.agentCache", app)
        self.assertIn("Agent 缓存命中 tokens", app)
        self.assertIn("Agent 缓存总 tokens", app)
        self.assertIn("各 Agent 缓存明细", app)
        self.assertIn('items.join("\\n")', app)
        self.assertIn('row.classList.add("is-multiline")', app)
        self.assertIn("white-space: pre-line", css)
        self.assertIn("formatAgentCacheTotal", app)
        self.assertIn("hasData", app)
        self.assertIn("无数据", app)
        self.assertIn("totalTokens", multi_agent)
        self.assertIn("formatAgentCacheByAgent", app)
        self.assertIn('"type": "agent_reasoning"', multi_agent)
        self.assertIn('"type": "agent_note"', multi_agent)
        self.assertIn("multi_agent_timeout_seconds: int = 3900", config)
        self.assertIn('MULTI_AGENT_TIMEOUT_SECONDS", 3900', config)
        # v1.2.4 璧?AGENT_ALLOWED_TOOLS 甯搁噺琚?agent_tools_for() 鍑芥暟鍙栦唬锛屾潈闄愭寜瑙掕壊鏀剁獎
        self.assertIn("def agent_tools_for", multi_agent)
        self.assertIn("flushPendingCodeAsText", markdown)

    def test_v072_local_tools_and_url_fetch_are_present(self) -> None:
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")

        self.assertIn("available_tool_definitions()", client)
        self.assertIn("execute_tool_calls(", client)
        self.assertIn('"name": "python_eval"', tools)
        self.assertIn('"name": "search_files"', tools)
        self.assertIn('"name": "fetch_url"', tools)
        self.assertIn('"name": "web_search"', tools)
        self.assertIn("web_search_callback", tools)
        self.assertIn('"/api/fetch-url"', server)

    def test_frontend_tavily_key_can_enable_search(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")

        self.assertIn("hasServerSearch: false", app)
        self.assertIn("function clientTavilyKey()", app)
        self.assertIn("function tavilyApiKeyForSearch(searchEnabled)", app)
        self.assertIn("function updateSearchAvailability({ render = true } = {})", app)
        self.assertIn("state.hasSearch = Boolean(state.hasServerSearch || clientTavilyKey())", app)
        self.assertIn("searchToggleButton.disabled = state.offlineMode", app)
        self.assertIn('searchToggleButton.classList.toggle("unavailable", !state.hasSearch)', app)
        self.assertIn('searchToggleButton.setAttribute("aria-disabled", String(!state.hasSearch))', app)
        self.assertIn("searchToggleButton.title = !state.hasSearch", app)
        self.assertIn("配置 Tavily API Key 后可启用联网搜索", app)
        self.assertIn("tavilyApiKey: tavilyApiKeyForSearch(shouldRequestSearch())", app)
        self.assertIn("请先设置 Tavily API Key 或启动前配置 TAVILY_API_KEY", app)
        self.assertNotIn("searchToggleButton.disabled = !state.hasSearch", app)
        self.assertIn(".tool-chip.unavailable:not(:disabled)", Path("static/styles.css").read_text(encoding="utf-8"))

    def test_seek_import_export_and_history_labels_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")

        self.assertIn('id="seekImportButton"', html)
        self.assertIn('id="seekExportButton"', html)
        self.assertIn('id="seekImportInput"', html)
        self.assertIn("function exportCustomSeeks()", app)
        self.assertIn("async function importSeeksFromFile(event)", app)
        self.assertIn("function forkSeek(id)", app)
        self.assertIn("data-seek-fork", app)
        self.assertIn("function seekNameForConversation(conversation)", app)
        self.assertIn("function onHistoryMenuKeydown(event)", app)
        self.assertIn("focusWithoutScroll(firstVisibleHistoryMenuItem(root))", app)
        self.assertIn("function focusWithoutScroll(element)", app)
        self.assertIn('closeHistoryMenu({ restoreFocus: true })', app)
        self.assertIn("function visibleHistoryMenuItems", app)
        self.assertIn("async function confirmAndDeleteConversation", app)
        self.assertIn("删除对话？", app)
        history_title_keydown = app.index("function onHistoryTitleKeydown(event)")
        self.assertLess(
            app.index("event.preventDefault();", history_title_keydown),
            app.index("state.editingConversationId = null;", history_title_keydown),
        )
        self.assertIn("history-seek", app)
        self.assertIn(".seek-panel-actions", css)
        self.assertIn(".history-seek", css)

    def test_frontend_app_entrypoint_is_split_into_modules(self) -> None:
        app = Path("static/app.js").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        self.assertLessEqual(len(app.splitlines()), 20)
        self.assertIn('import { bootstrap } from "./modules/chat.js"', app)
        for module in [
            "network",
            "charts",
            "format",
            "markdown",
            "normalize",
            "settings",
            "panels",
            "reminder_parse",
            "speech_text",
            "stream",
            "agent_timeline",
            "chat",
        ]:
            with self.subTest(module=module):
                self.assertTrue(Path(f"static/modules/{module}.js").is_file())
                self.assertIn(f'"/modules/{module}.js"', sw)

    def test_gitignore_excludes_runtime_private_state(self) -> None:
        ignore = Path(".gitignore").read_text(encoding="utf-8")

        for pattern in [
            ".auth-token",
            ".file-cache/",
            ".agent-runs/",
            ".memory/",
            ".projects/",
            ".reminders/",
            ".coverage",
            ".mypy_cache/",
            ".npm-cache/",
            ".ruff_cache/",
            "__pycache__/",
            ".idea/",
            "dist/",
            "server*.log",
            "pytest-cache-files-*/",
        ]:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignore)

    def test_v140_recoverable_agent_runs_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        agent_runs = Path("deepseek_infra/infra/agent_runtime/agent_runs.py").read_text(encoding="utf-8")
        multi_agent = Path("deepseek_infra/infra/agent_runtime/multi_agent.py").read_text(encoding="utf-8")
        release = Path("scripts/release.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for token in [
            'agentPreset: "deepseek-infra.agent-preset"',
            "async function startAgentRunForMessage",
            "async function attachAgentRunStream",
            "async function resumePendingAgentRuns",
            "function ensureAssistantHasVisibleContent(message)",
            "emptyAgentRunAnswerText",
            "function markAuthRequired",
            "state.authRequired",
            'event.type === "final_reset"',
            'event.type === "agent_reset"',
            "resetTimelineAgentPhase",
            "function renderAgentPlanWorkbench",
            "function normalizedEditableAgentPlan",
            "node.className = fresh.className",
            "单 Agent 重跑不会自动级联其它 Agent",
            "重新综合最终回答",
            "/api/agent-runs",
            "agentRunLastEventIndex",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        self.assertIn('id="agentPresetSelect"', html)
        self.assertIn(".agent-plan-workbench", css)
        self.assertIn("api_agent_runs_create", server)
        self.assertIn("agent_run_event_stream", server)
        self.assertIn("AgentRunRegistry", agent_runs)
        self.assertIn("events 是恢复 UI 的事实源", agent_runs)
        self.assertIn('"type": "final_reset"', agent_runs)
        self.assertIn('"type": "agent_reset"', agent_runs)
        self.assertIn("ORPHANABLE_STATUSES", agent_runs)
        self.assertIn("replace_with_retry", agent_runs)
        self.assertIn("RUN_WRITE_RETRY_DELAYS", agent_runs)
        self.assertIn("threading.get_ident()", agent_runs)
        self.assertIn("token_urlsafe(6)", agent_runs)
        self.assertIn("PermissionError", agent_runs)
        self.assertIn("stream_agent_plan", multi_agent)
        self.assertIn("stream_synthesis_for_outputs", multi_agent)
        self.assertIn("EMPTY_SYNTHESIS_FALLBACK", multi_agent)
        self.assertIn("content_seen", multi_agent)
        self.assertIn('".agent-runs"', release)
        self.assertIn("可恢复 Agent Run", readme)
        self.assertIn("## [1.4.0]", changelog)
        self.assertIn("## Agent Run API", api)
        self.assertIn("deepseek_infra/infra/agent_runtime/agent_runs.py", architecture)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v160_mobile_launcher_is_present(self) -> None:
        launch_py = Path("launch.py").read_text(encoding="utf-8")
        mobile_launcher = Path("deepseek_infra/launcher/mobile.py").read_text(encoding="utf-8")
        mobile_requirements = Path("requirements-mobile.txt").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        self.assertTrue(Path("launch_mobile.py").is_file())
        self.assertTrue(Path("launch_mobile.sh").is_file())
        self.assertTrue(Path("requirements-mobile.txt").is_file())
        self.assertIn("fastapi==0.115.14", mobile_requirements)
        self.assertIn("pydantic==1.10.24", mobile_requirements)
        self.assertIn("uvicorn>=0.30,<1", mobile_requirements)
        self.assertIn("is_mobile_environment()", launch_py)
        self.assertIn("DEFAULT_MOBILE_HOST = \"127.0.0.1\"", mobile_launcher)
        self.assertIn("termux-open-url", mobile_launcher)
        self.assertIn("prepare_and_start(host=host, port=port, serve=False)", mobile_launcher)
        self.assertIn("手机本机直接运行", readme)
        self.assertIn("## [1.6.0]", changelog)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v160_web_interaction_fixes_are_present(self) -> None:
        app = Path("static/modules/chat.js").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")

        for token in [
            "function hasClosablePanelOpen()",
            "if (hasClosablePanelOpen())",
            "closePanels();",
            "focusTrapStack: []",
            "state.focusTrapStack.push({ container, previous })",
            "const entryIndex = state.focusTrapStack",
            "Nested dialogs reuse the same tab trap machinery",
            'activityPanel.addEventListener("click", onActivityPanelClick)',
            'button[data-copy-agent-report]',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, app)

        self.assertNotIn('copyReport.addEventListener("click"', app)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [1.6.0]", changelog)
        self.assertIn("v1.6.0", architecture)

    def test_v161_web_search_tool_cache_stability_is_present(self) -> None:
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        search = Path("deepseek_infra/infra/tool_runtime/search.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for token in [
            "stable_tool_call_id",
            "canonical_tool_arguments",
            "Preserve the upstream tool_call ids",
            "use_cache=True",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, client)

        for token in [
            "use_cache: bool = False",
            "load_search_cache(cleaned)",
            "search_round_from_cache",
            "save_search_cache(cleaned",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, search)

        for token in [
            "stable_tool_output_for_model",
            "strip_volatile_tool_fields",
            'key not in {"cached"}',
            "sort_keys=True",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, tools)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [1.6.1]", changelog)
        self.assertIn("适用版本：v2.1.7。", api)
        self.assertIn("deepseek-infra-v186", sw)


    def test_v162_android_ocr_bridge_is_present(self) -> None:
        android_ocr = Path("android/app/src/main/java/com/deepseek/mobile/AndroidOcrBridge.java").read_text(encoding="utf-8")
        main_activity = Path("android/app/src/main/java/com/deepseek/mobile/MainActivity.java").read_text(encoding="utf-8")
        build_gradle = Path("android/app/build.gradle").read_text(encoding="utf-8")
        android_entry = Path("deepseek_infra/android_entry.py").read_text(encoding="utf-8")
        ocr = Path("deepseek_infra/infra/tool_runtime/ocr.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        apk_docs = Path("docs/APK.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for token in [
            "AndroidOcrBridge",
            "recognizeImage(byte[] imageBytes)",
            "recognizePdf(byte[] pdfBytes)",
            "ChineseTextRecognizerOptions",
            "PdfRenderer",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, android_ocr)

        self.assertIn("AndroidOcrBridge.initialize(getApplicationContext())", main_activity)
        self.assertIn("com.google.mlkit:text-recognition-chinese", build_gradle)
        self.assertIn('versionName "2.1.7"', build_gradle)
        self.assertIn('os.environ["DEEPSEEK_ANDROID_APP"] = "1"', android_entry)
        self.assertIn('os.environ.setdefault("OCR_ENABLED", "1")', android_entry)
        self.assertIn("PDF_RENDER_SCALE = 3", android_ocr)
        self.assertIn("MAX_PDF_BITMAP_PIXELS = 6_000_000", android_ocr)
        self.assertIn("class AndroidMlKitEngine", ocr)
        self.assertIn("class WindowsOcrEngine", ocr)
        self.assertIn("Windows.Media.Ocr.OcrEngine", ocr)
        self.assertIn("PowerShell is required for Windows OCR.", ocr)
        self.assertIn('jclass("com.deepseek.mobile.AndroidOcrBridge")', ocr)
        self.assertIn("DEEPSEEK_ANDROID_APP", ocr)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [1.6.2]", changelog)
        self.assertIn("ML Kit", apk_docs)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v163_desktop_local_app_shell_is_present(self) -> None:
        desktop_app = Path("deepseek_infra/desktop_app.py").read_text(encoding="utf-8")
        launch_py = Path("launch.py").read_text(encoding="utf-8")
        build_exe = Path("scripts/build_exe.py").read_text(encoding="utf-8")
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        frontend_docs = Path("docs/FRONTEND_MODULES.md").read_text(encoding="utf-8")
        security = Path("docs/SECURITY.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for token in [
            'prepare_and_start(host="127.0.0.1", serve=True)',
            "wait_for_server_ready(url)",
            "open_app_window(url)",
            "def webview_entry_url",
            "def wait_for_server_ready",
            "webview.create_window",
            "webview.start(debug=False, private_mode=False)",
            "shutdown_handle(handle)",
            "messagebox.showerror",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, desktop_app)

        for token in [
            "local desktop app window",
            "deepseek_infra.desktop_app",
            "--gui",
            "--server",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, launch_py)

        for token in [
            "--collect-all=webview",
            "--collect-all=pythonnet",
            "--collect-all=clr_loader",
            "local desktop",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, build_exe)

        self.assertIn("pywebview>=5,<6", requirements)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("内嵌 WebView", readme)
        self.assertIn("## [1.6.3]", changelog)
        self.assertIn("适用版本：v2.1.7。", api)
        self.assertIn("deepseek_infra/desktop_app.py", architecture)
        self.assertIn("pywebview", architecture)
        self.assertIn("pywebview", frontend_docs)
        self.assertIn("内嵌 WebView", security)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v165_multi_agent_budget_revision_and_dynamic_dag_are_present(self) -> None:
        multi_agent = Path("deepseek_infra/infra/agent_runtime/multi_agent.py").read_text(encoding="utf-8")
        chat = Path("static/modules/chat.js").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for token in [
            "MAX_REVISION_ROUNDS",
            "CRITIC_VERDICT_INSTRUCTION",
            "修订建议",
            "def run_critic_revision",
            "def parse_critic_verdict",
            "def plan_has_dependencies",
            "def layered_plan",
            "def _dependency_layers",
            "def _legacy_role_tiers",
            "depends_on",
        ]:
            with self.subTest(token=token):
                self.assertIn(token, multi_agent)

        self.assertIn("MULTI_AGENT_TOKEN_BUDGET", config)
        self.assertIn("multi_agent_token_budget", config)
        self.assertIn("class TokenBudget", client)
        self.assertIn("normalizeEditableAgentDependsOn", chat)
        self.assertIn('depends_on: ["researcher", "coder", "reasoner"]', chat)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [1.6.5]", changelog)
        self.assertIn("适用版本：v2.1.7。", api)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v166_gemini_skin_and_frontend_fixes_are_present(self) -> None:
        index_html = Path("static/index.html").read_text(encoding="utf-8")
        gemini_css = Path("static/gemini.css").read_text(encoding="utf-8")
        chat_js = Path("static/modules/chat.js").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        build_gradle = Path("android/app/build.gradle").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        # Gemini 鐨偆鎺ョ嚎锛歜ody 浣滅敤鍩?+ 鍙犲姞鏍峰紡琛?+ 鏂版杩庤
        self.assertIn('class="gemini-ui"', index_html)
        self.assertIn("/gemini.css", index_html)
        self.assertIn("你好，今天能帮你点什么？", index_html)
        self.assertIn("body.gemini-ui", gemini_css)
        self.assertIn("#0b57d0", gemini_css)

        # 澶?Agent 鍘嗗彶鍥炴斁閲嶈繛淇 + 鍗犱綅绗?
        self.assertIn("agentRunStreamIncomplete", chat_js)
        self.assertIn("AGENT_STREAM_MAX_STALLED_RECONNECTS", chat_js)
        self.assertIn("问问 DeepSeek", chat_js)

        # SW 棰勭紦瀛樼毊鑲?+ 鐗堟湰鍚屾鍒?1.6.6
        self.assertIn("/gemini.css", sw)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn('versionName "2.1.7"', build_gradle)
        self.assertIn("versionCode 218", build_gradle)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("create_pptx", readme)
        self.assertIn("## [1.6.6]", changelog)

    def test_v176_local_rag_data_infra_is_present(self) -> None:
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        local_rag = Path("deepseek_infra/infra/rag/local_rag.py").read_text(encoding="utf-8")
        files = Path("deepseek_infra/infra/rag/files.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        security = Path("docs/SECURITY.md").read_text(encoding="utf-8")

        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("class LocalRAGSettings", config)
        self.assertIn("LOCAL_RAG_BACKEND", config)
        self.assertIn("sqlite_vec.load", local_rag)
        self.assertIn("onnxruntime", local_rag)
        self.assertIn("Tokenizer.from_file", local_rag)
        self.assertIn("index_file_payload(payload", files)
        self.assertIn("search_files_index", tools)
        self.assertIn("/api/rag/status", server)
        self.assertIn("/api/rag/reindex", server)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn(".local-rag", readme)
        self.assertIn("## [1.7.6]", changelog)
        self.assertIn("适用版本：v2.1.7。", api)
        self.assertIn(".local-rag/rag.sqlite3", security)


    def test_v180_gateway_resiliency_is_present(self) -> None:
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        context_manager = Path("deepseek_infra/infra/gateway/context_manager.py").read_text(encoding="utf-8")
        observability = Path("deepseek_infra/infra/observability/observability.py").read_text(encoding="utf-8")
        resiliency = Path("deepseek_infra/infra/gateway/resiliency.py").read_text(encoding="utf-8")
        semantic_cache = Path("deepseek_infra/infra/gateway/semantic_cache.py").read_text(encoding="utf-8")
        deepseek_client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        multi_agent = Path("deepseek_infra/infra/agent_runtime/multi_agent.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        chat_js = Path("static/modules/chat.js").read_text(encoding="utf-8")
        styles = Path("static/styles.css").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        security = Path("docs/SECURITY.md").read_text(encoding="utf-8")

        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("class TracingSettings", config)
        self.assertIn("class SemanticCacheSettings", config)
        self.assertIn("class GatewaySettings", config)
        self.assertIn("GATEWAY_REQUEST_QUEUE_DB", config)
        self.assertIn("manage_request_body", context_manager)
        self.assertIn("stable_json_dumps", context_manager)
        self.assertIn("TRACE_DB", config)
        self.assertIn("SEMANTIC_CACHE_THRESHOLD", config)
        self.assertIn("trace_runs", observability)
        self.assertIn("trace_spans", observability)
        self.assertIn("with_trace_diagnostics", observability)
        self.assertIn("semantic_cache_items", semantic_cache)
        self.assertIn("cosine_similarity", semantic_cache)
        self.assertIn("request_queue_items", resiliency)
        self.assertIn("open_with_resiliency", resiliency)
        self.assertIn("gateway_status", resiliency)
        self.assertIn("semantic_cache_lookup", deepseek_client)
        self.assertIn("semantic_cache_store", deepseek_client)
        self.assertIn("diagnostics_with_gateway", deepseek_client)
        self.assertIn("traceId", multi_agent)
        self.assertIn("/api/traces/{trace_id}", server)
        self.assertIn("/api/semantic-cache/status", server)
        self.assertIn("/api/gateway/status", server)
        self.assertIn("formatGatewayResiliency", chat_js)
        self.assertIn("formatContextManager", chat_js)
        self.assertIn("data-trace-message", chat_js)
        self.assertIn("renderTracePanel", chat_js)
        self.assertIn(".trace-waterfall", styles)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn(".request-queue", readme)
        self.assertIn("## [1.8.0]", changelog)
        self.assertIn("GET `/api/gateway/status`", api)
        self.assertIn(".request-queue/queue.sqlite3", security)

    def test_v191_content_risk_graceful_degradation_is_present(self) -> None:
        utils = Path("deepseek_infra/core/utils.py").read_text(encoding="utf-8")
        errors = Path("deepseek_infra/core/errors.py").read_text(encoding="utf-8")
        deepseek_client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        chat_js = Path("static/modules/chat.js").read_text(encoding="utf-8")
        styles = Path("static/styles.css").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        # 后端：内容安全拦截识别 + 人性化文案 + 专用错误码
        self.assertIn("def is_content_risk_error", utils)
        self.assertIn("def humanize_upstream_error", utils)
        self.assertIn("内容安全提示", utils)
        self.assertIn("content exists risk", utils)
        self.assertIn("UPSTREAM_CONTENT_RISK", errors)
        self.assertIn("upstream_content_risk", errors)
        self.assertIn("humanize_upstream_error(raw_message)", deepseek_client)
        self.assertIn("is_content_risk_error(raw_message)", deepseek_client)

        # 前端：软展示、保留思考、可持久化的 contentFiltered 标记
        self.assertIn("function applyAssistantFailure", chat_js)
        self.assertIn("streamError.contentFiltered = true", chat_js)
        self.assertIn('event.code === "upstream_content_risk"', chat_js)
        self.assertIn("contentFiltered: Boolean(value.contentFiltered)", chat_js)
        self.assertIn(".message.error.content-filtered .bubble", styles)

        # 版本戳 + 前端资源缓存版本
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [1.9.1]", changelog)

    def test_v200_infra_platform_reposition_is_present(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        openai_api = Path("deepseek_infra/infra/gateway/openai_api.py").read_text(encoding="utf-8")
        metrics = Path("deepseek_infra/infra/observability/metrics.py").read_text(encoding="utf-8")
        health = Path("deepseek_infra/infra/observability/health.py").read_text(encoding="utf-8")

        # 包重命名 + infra 目录分层
        self.assertTrue(Path("deepseek_infra/infra/gateway/openai_api.py").is_file())
        self.assertTrue(Path("deepseek_infra/infra/agent_runtime/multi_agent.py").is_file())
        self.assertTrue(Path("deepseek_infra/infra/rag/local_rag.py").is_file())
        self.assertTrue(Path("deepseek_infra/infra/data/memory.py").is_file())
        self.assertFalse(Path("deepseek_mobile").exists())

        # 产品叙事重定位（v2.1.5 起定位升级为 agentic AI infrastructure platform）
        self.assertIn("# DeepSeek Infra", readme)
        self.assertIn("local-first agentic AI infrastructure platform", readme)
        self.assertIn("DeepSeek Infra", architecture)
        self.assertIn('FastAPI(title="DeepSeek Infra"', server)

        # OpenAI 兼容网关
        self.assertIn("/v1/chat/completions", server)
        self.assertIn("/v1/models", server)
        self.assertIn("def openai_to_internal_payload", openai_api)
        self.assertIn("chat.completion.chunk", openai_api)

        # 运维端点
        self.assertIn("/healthz", server)
        self.assertIn("/metrics", server)
        self.assertIn("def render_prometheus", metrics)
        self.assertIn("ai_requests_total", metrics)
        self.assertIn("def healthz", health)
        self.assertIn("def readyz", health)

        self.assertIn("## [2.0.0]", changelog)

    def test_v201_multi_provider_ollama_is_present(self) -> None:
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        base = Path("deepseek_infra/infra/gateway/providers/base.py").read_text(encoding="utf-8")
        ollama = Path("deepseek_infra/infra/gateway/providers/ollama.py").read_text(encoding="utf-8")
        registry = Path("deepseek_infra/infra/gateway/providers/registry.py").read_text(encoding="utf-8")
        openai_api = Path("deepseek_infra/infra/gateway/openai_api.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        self.assertTrue(Path("deepseek_infra/infra/gateway/providers/__init__.py").is_file())
        self.assertIn("class BaseLLMProvider", base)
        self.assertIn("class OllamaProvider", ollama)
        self.assertIn("/api/tags", ollama)
        self.assertIn("/api/chat", ollama)
        self.assertIn("OLLAMA_MODEL_PREFIX", ollama)
        self.assertIn("def resolve_provider", registry)
        self.assertIn("def model_catalog", registry)
        self.assertIn("class OllamaSettings", config)
        self.assertIn("OLLAMA_ENABLED", config)
        # /v1 routes through the provider registry
        self.assertIn("resolve_provider", openai_api)
        self.assertIn("openai_chat_completion", server)
        self.assertIn('"providers": providers_status()', server)
        self.assertIn("## [2.0.1]", changelog)

    def test_v202_ppt_outline_parser_enhancement_is_present(self) -> None:
        presentations = Path("deepseek_infra/infra/tool_runtime/presentations.py").read_text(encoding="utf-8")
        test_presentations = Path("tests/test_presentations.py").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        for symbol in (
            "_MARKDOWN_SLIDE_HEADING_RE",
            "_OUTLINE_META_TITLE_RE",
            "def _outline_slide_title",
            "def _looks_like_body_line",
            "def _looks_like_numbered_body_line",
        ):
            self.assertIn(symbol, presentations)
        self.assertIn("test_outline_text_accepts_markdown_and_chinese_slide_variants", test_presentations)
        self.assertIn("## [2.0.2]", changelog)

    def test_v204_context_engine_is_present(self) -> None:
        context_engine = Path("deepseek_infra/infra/gateway/context_engine.py").read_text(encoding="utf-8")
        context_manager = Path("deepseek_infra/infra/gateway/context_manager.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        test_context_engine = Path("tests/test_context_engine.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

        for symbol in (
            "def estimate_tokens",
            "def estimate_body_breakdown",
            "def context_window_for_model",
            "def available_input_tokens",
            "class TokenBudgetPlan",
            "def plan_token_budget",
            "def token_trim",
            "def base_context_id",
            "def build_context_diff",
            "def build_engine_diagnostics",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, context_engine)

        for token in (
            "from deepseek_infra.infra.gateway import context_engine",
            "context_engine.token_trim",
            "context_engine.build_engine_diagnostics",
            "tokenAwareTrimApplied",
        ):
            with self.subTest(token=token):
                self.assertIn(token, context_manager)

        self.assertIn("class ContextEngineSettings", config)
        self.assertIn("CONTEXT_ENGINE_ENABLED = settings.context_engine.enabled", config)
        self.assertIn("CONTEXT_ENGINE_MODEL_CONTEXT_WINDOWS", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("def test_token_trim_drops_oldest_and_preserves_system_anchors", test_context_engine)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.0.4]", changelog)
        self.assertIn("tokenBudget", api)
        self.assertIn("context_engine.py", architecture)

    def test_v205_durable_agent_runtime_is_present(self) -> None:
        agent_state = Path("deepseek_infra/infra/agent_runtime/agent_state.py").read_text(encoding="utf-8")
        agent_runs = Path("deepseek_infra/infra/agent_runtime/agent_runs.py").read_text(encoding="utf-8")
        multi_agent = Path("deepseek_infra/infra/agent_runtime/multi_agent.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        app = Path("deepseek_infra/app.py").read_text(encoding="utf-8")
        test_agent_state = Path("tests/test_agent_state.py").read_text(encoding="utf-8")
        test_agent_runs = Path("tests/test_agent_runs.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

        for symbol in (
            "NODE_STATES",
            "NODE_TRANSITIONS",
            "def can_transition",
            "def reduce_node_states",
            "def incomplete_plan_nodes",
            "def completed_node_ids",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, agent_state)

        for token in (
            "from deepseek_infra.infra.agent_runtime.agent_state import",
            "def resume_run",
            "def resume_orphaned_runs",
            'run["nodes"] = reduce_node_states',
        ):
            with self.subTest(token=token):
                self.assertIn(token, agent_runs)

        self.assertIn("completed_outputs", multi_agent)
        self.assertIn("def _outputs_in_plan_order", multi_agent)
        self.assertIn("class AgentRuntimeSettings", config)
        self.assertIn("AGENT_RUNTIME_AUTO_RESUME = settings.agent_runtime.auto_resume", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn('action == "resume"', server)
        self.assertIn("resume_run", server)
        self.assertIn("resume_orphaned_runs()", app)
        self.assertIn("def test_reduce_tracks_running_success_and_metrics", test_agent_state)
        self.assertIn("def test_resume_run_skips_completed_and_reruns_incomplete", test_agent_runs)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.0.5]", changelog)
        self.assertIn("/api/agent-runs/{run_id}/resume", api)
        self.assertIn("agent_state.py", architecture)

    def test_v206_agent_trace_span_tree_is_present(self) -> None:
        deepseek_client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        multi_agent = Path("deepseek_infra/infra/agent_runtime/multi_agent.py").read_text(encoding="utf-8")
        agent_timeline = Path("static/modules/agent_timeline.js").read_text(encoding="utf-8")
        chat = Path("static/modules/chat.js").read_text(encoding="utf-8")
        css = Path("static/styles.css").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        test_frontend = Path("tests/test_frontend_utils.py").read_text(encoding="utf-8")
        test_trace_tree = Path("tests/test_observability_trace_tree.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

        for token in (
            'name="context.build"',
            'name="memory.retrieve"',
            'name="rag.retrieve"',
            'name="tool.web_search"',
            "parent_span_id: str = \"\"",
            "parent_span_id=span_parent",
        ):
            with self.subTest(token=token):
                self.assertIn(token, deepseek_client)

        for token in (
            "import ensure_trace, finish_trace, start_span, with_trace_diagnostics",
            "name=f\"agent.{item['id']}\"",
            'name="agent.planner"',
            'name="agent.synthesizer"',
            "parent_span_id=agent_span.span_id",
        ):
            with self.subTest(token=token):
                self.assertIn(token, multi_agent)

        self.assertIn("export function buildTraceSpanTree", agent_timeline)
        self.assertIn("buildTraceSpanTree,", chat)
        self.assertIn("buildTraceSpanTree(spans)", chat)
        self.assertIn("renderTraceSpan(span, maxEnd, depth)", chat)
        self.assertIn(".trace-span.is-child", css)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("def test_build_trace_span_tree_nests_children_and_keeps_orphans", test_frontend)
        self.assertIn("def test_execute_agent_tier_nests_llm_span_under_agent_span", test_trace_tree)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.0.6]", changelog)
        self.assertIn("tool.web_search", api)
        self.assertIn("OpenTelemetry", architecture)

    def test_v207_semantic_cache_advanced_mechanisms_are_present(self) -> None:
        semantic_cache = Path("deepseek_infra/infra/gateway/semantic_cache.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        test_cache = Path("tests/test_observability_semantic_cache.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

        for symbol in (
            "def cache_version(",
            "def scope_for(",
            "def quality_score(",
            "def has_attachments(",
            "def _ensure_columns(",
            "LOW_QUALITY_MARKERS",
            "exact_only = has_attachments(payload)",
            '"low_quality"',
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, semantic_cache)

        for token in (
            "SEMANTIC_CACHE_VERSION = settings.semantic_cache.version",
            "SEMANTIC_CACHE_MIN_QUALITY = settings.semantic_cache.min_quality_score",
            "SEMANTIC_CACHE_ATTACHMENTS = settings.semantic_cache.cache_attachments",
            'app_version: str = "2.1.7"',
        ):
            with self.subTest(token=token):
                self.assertIn(token, config)

        self.assertIn("def test_semantic_cache_version_isolation", test_cache)
        self.assertIn("def test_semantic_cache_attachments_use_exact_match_only", test_cache)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.0.7]", changelog)
        self.assertIn("qualityScore", api)
        self.assertIn("semantic_cache.py", architecture)

    def test_v208_local_rag_data_plane_is_present(self) -> None:
        local_rag = Path("deepseek_infra/infra/rag/local_rag.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        test_rag = Path("tests/test_local_rag.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

        for symbol in (
            "def bm25_scores(",
            "def chunk_hash(",
            "def doc_version(",
            "def existing_doc_chunks(",
            "def chunk_lineage(",
            "def verify_citation(",
            "def evaluate_recall(",
            '"docVersion": new_version',
            "lexical_scores = bm25_scores(tokens, docs_terms)",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, local_rag)

        for token in (
            "LOCAL_RAG_BM25_K1 = settings.local_rag.bm25_k1",
            "LOCAL_RAG_INCREMENTAL = settings.local_rag.incremental",
            'app_version: str = "2.1.7"',
        ):
            with self.subTest(token=token):
                self.assertIn(token, config)

        self.assertIn("/api/rag/verify-citation", server)
        self.assertIn("/api/rag/eval", server)
        self.assertIn("local_rag.chunk_lineage(result)", tools)
        self.assertIn("def test_verify_citation_grounding", test_rag)
        self.assertIn("def test_incremental_index_skips_unchanged_document", test_rag)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.0.8]", changelog)
        self.assertIn("chunk lineage", api.lower())
        self.assertIn("local_rag.py", architecture)

    def test_v209_model_router_and_cascade_are_present(self) -> None:
        model_router = Path("deepseek_infra/infra/gateway/model_router.py").read_text(encoding="utf-8")
        deepseek_client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        chat = Path("static/modules/chat.js").read_text(encoding="utf-8")
        html = Path("static/index.html").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        test_router = Path("tests/test_model_router.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

        for symbol in (
            "def route_request(",
            "def cascade_plan(",
            "def quality_gate(",
            "def is_auto_request(",
            "class RouteDecision",
            "class CascadePlan",
            "def router_status(",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, model_router)

        for token in (
            "def call_deepseek_cascade(",
            "def judge_draft(",
            "model_router.route_request(payload)",
            '"modelCascade"',
            '"modelRouter"',
        ):
            with self.subTest(token=token):
                self.assertIn(token, deepseek_client)

        self.assertIn("class ModelRouterSettings", config)
        self.assertIn("MODEL_ROUTER_ENABLED = settings.model_router.enabled", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("emit_cascade_as_stream", server)
        self.assertIn("model_router_cascade_requested", server)

        self.assertIn('id="modelRouteSelect"', html)
        self.assertIn('id="cascadeEnabledInput"', html)
        self.assertIn("autoRoute: Boolean(state.autoRoute)", chat)
        self.assertIn('autoRoute: "deepseek-infra.auto-route"', chat)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("def test_cascade_escalates_when_gate_fails", test_router)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.0.9]", changelog)
        self.assertIn("modelCascade", api)
        self.assertIn("model_router.py", architecture)

    def test_v210_cost_and_token_budget_manager_is_present(self) -> None:
        budget_manager = Path("deepseek_infra/infra/gateway/budget_manager.py").read_text(encoding="utf-8")
        deepseek_client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        multi_agent = Path("deepseek_infra/infra/agent_runtime/multi_agent.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        chat = Path("static/modules/chat.js").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        test_budget = Path("tests/test_budget_manager.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")

        for symbol in (
            "def estimate_cost(",
            "def cost_from_usage(",
            "class BudgetPolicy",
            "class ToolBudget",
            "def record_spend(",
            "def daily_spend(",
            "def over_daily_budget(",
            "def should_downgrade(",
            "def record_request_spend(",
            "def budget_status(",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, budget_manager)

        for token in (
            "from deepseek_infra.infra.gateway import budget_manager",
            "budget_manager.record_request_spend(",
            "budget_manager.should_downgrade(",
            '"budgetDowngraded"',
        ):
            with self.subTest(token=token):
                self.assertIn(token, deepseek_client)

        self.assertIn("def agent_cost_for_diagnostics(", multi_agent)
        self.assertIn('"agentCostUsd"', multi_agent)
        self.assertIn("class BudgetSettings", config)
        self.assertIn("BUDGET_PRICING = settings.budget.pricing", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn('"/api/budget"', server)
        self.assertIn("def agent_exhausted(", deepseek_client)
        self.assertIn("function formatCostUsd(", chat)
        self.assertIn("state.budget = config.budget", chat)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("def test_build_request_downgrades_model_when_over_budget", test_budget)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.0.10]", changelog)
        self.assertIn("costUsd", api)
        self.assertIn("budget_manager.py", architecture)

    def test_v211_tool_policy_engine_is_present(self) -> None:
        tool_policy = Path("deepseek_infra/infra/tool_runtime/tool_policy.py").read_text(encoding="utf-8")
        tools = Path("deepseek_infra/infra/tool_runtime/tools.py").read_text(encoding="utf-8")
        deepseek_client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        multi_agent = Path("deepseek_infra/infra/agent_runtime/multi_agent.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        chat = Path("static/modules/chat.js").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        test_policy = Path("tests/test_tool_policy.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        security = Path("docs/SECURITY.md").read_text(encoding="utf-8")

        for symbol in (
            "class ToolMetadata",
            "class PolicyDecision",
            "class ToolPolicy",
            "def evaluate_url_safety(",
            "def evaluate_path_safety(",
            "def validate_arguments(",
            "def sanitize_tool_result(",
            "def capability_tools(",
            "def tool_policy_status(",
            "def write_audit_entry(",
            "CAPABILITY_PROFILES",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, tool_policy)

        for token in (
            "def tool_parameter_schemas(",
            "policy: ToolPolicy | None = None",
            "ToolPolicy.denial_output(decision)",
            "policy.sanitize_result(",
        ):
            with self.subTest(token=token):
                self.assertIn(token, tools)

        for token in (
            "def build_tool_policy(",
            "def diagnostics_with_tool_policy(",
            '"toolPolicy"',
            "policy=tool_policy",
        ):
            with self.subTest(token=token):
                self.assertIn(token, deepseek_client)

        self.assertIn("return capability_tools(agent_id)", multi_agent)
        self.assertIn('"capability": agent_id', multi_agent)
        self.assertIn("class ToolPolicySettings", config)
        self.assertIn("TOOL_POLICY_ENABLED = settings.tool_policy.enabled", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn('"/api/tool-policy"', server)
        self.assertIn("tool_policy_status()", server)
        self.assertIn("function formatToolPolicy(", chat)
        self.assertIn("state.toolPolicy = config.toolPolicy", chat)
        self.assertIn("deepseek-infra-v186", sw)
        self.assertIn("def test_capability_profile_denies_out_of_scope_tool", test_policy)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.1.0]", changelog)
        self.assertIn("tool-policy", api)
        self.assertIn("tool_policy.py", architecture)
        self.assertIn("Tool Policy Engine", security)

    def test_v212_eval_harness_is_present(self) -> None:
        harness = Path("deepseek_infra/infra/evaluation/harness.py").read_text(encoding="utf-8")
        run_rag = Path("evals/runners/run_rag_eval.py").read_text(encoding="utf-8")
        run_agent = Path("evals/runners/run_agent_eval.py").read_text(encoding="utf-8")
        rag_golden = Path("evals/golden/rag_questions.jsonl").read_text(encoding="utf-8")
        agent_golden = Path("evals/golden/agent_tasks.jsonl").read_text(encoding="utf-8")
        evals_readme = Path("evals/README.md").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        test_harness = Path("tests/test_eval_harness.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for symbol in (
            "def load_jsonl(",
            "def keyword_coverage(",
            "def recall_at_k(",
            "def citation_case(",
            "def tool_call_score(",
            "def tool_call_accuracy(",
            "def agent_success(",
            "def latency_benchmark(",
            "def cost_benchmark(",
            "def keyword_regression(",
            "class EvalReport",
            "def build_rag_report(",
            "def build_agent_report(",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, harness)

        self.assertIn("def evaluate(", run_rag)
        self.assertIn("search_files_index", run_rag)
        self.assertIn("index_file_payload", run_rag)
        self.assertIn("build_agent_report", run_agent)
        self.assertIn("tool_call_score", run_agent)
        self.assertIn("expected_source", rag_golden)
        self.assertIn("expected_tools", agent_golden)
        self.assertIn("RAG Recall@K", evals_readme)

        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("def test_rag_runner_evaluates_real_retrieval_offline", test_harness)
        # Pure backend + tooling change: no frontend, Service Worker cache stays put.
        self.assertIn("deepseek-infra-v186", sw)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("Evaluation Harness", readme)
        self.assertIn("## [2.1.1]", changelog)
        self.assertIn("evaluation/harness.py", architecture)

    def test_v213_request_scheduler_is_present(self) -> None:
        scheduler = Path("deepseek_infra/infra/gateway/scheduler.py").read_text(encoding="utf-8")
        deepseek_client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        resiliency = Path("deepseek_infra/infra/gateway/resiliency.py").read_text(encoding="utf-8")
        app = Path("deepseek_infra/app.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        conftest = Path("tests/conftest.py").read_text(encoding="utf-8")
        test_scheduler = Path("tests/test_scheduler.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        security = Path("docs/SECURITY.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for symbol in (
            "class TokenBucket",
            "class RequestScheduler",
            "class SchedulerOverloaded",
            "class SchedulerTimeout",
            "def _admit(",
            "def lease(",
            "def priority_for_payload(",
            "def record_dead_letter(",
            "def dead_letters(",
            "def recover_orphans(",
            "def scheduler_status(",
            "max_queue_depth",
            "PRIORITY_INTERACTIVE",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, scheduler)

        self.assertIn("scheduler.lease(", deepseek_client)
        self.assertIn("scheduler.priority_for_payload(", deepseek_client)
        self.assertIn('"scheduler"', resiliency)
        self.assertIn("recover_scheduler_orphans", app)
        self.assertIn("class SchedulerSettings", config)
        self.assertIn("SCHEDULER_ENABLED = settings.scheduler.enabled", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn('"/api/scheduler"', server)
        self.assertIn("scheduler_status", server)
        self.assertIn("SCHEDULER_DIR", conftest)
        self.assertIn("def test_backpressure_sheds_with_appstyle_503", test_scheduler)
        # Pure backend change: no frontend, Service Worker cache stays put.
        self.assertIn("deepseek-infra-v186", sw)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("请求调度", readme)
        self.assertIn("## [2.1.2]", changelog)
        self.assertIn("/api/scheduler", api)
        self.assertIn("scheduler.py", architecture)
        self.assertIn("backpressure", security)

    def test_v214_mcp_tool_hub_is_present(self) -> None:
        mcp_server = Path("deepseek_infra/infra/mcp/server.py").read_text(encoding="utf-8")
        mcp_registry = Path("deepseek_infra/infra/mcp/registry.py").read_text(encoding="utf-8")
        mcp_adapters = Path("deepseek_infra/infra/mcp/adapters.py").read_text(encoding="utf-8")
        mcp_permissions = Path("deepseek_infra/infra/mcp/permissions.py").read_text(encoding="utf-8")
        mcp_client = Path("deepseek_infra/infra/mcp/client.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        test_mcp = Path("tests/test_mcp.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for symbol in (
            'MCP_PROTOCOL_VERSION = "2025-06-18"',
            "def handle_mcp_message(",
            "def mcp_status(",
            '"tools/call"',
            '"resources/read"',
            '"prompts/get"',
            "METHOD_NOT_FOUND",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, mcp_server)

        self.assertIn("def mcp_tools(", mcp_registry)
        self.assertIn("GENERATED_URI_PREFIX", mcp_registry)
        self.assertIn("slides-outline", mcp_registry)
        self.assertIn("def call_hub_tool(", mcp_adapters)
        self.assertIn("connection_policy", mcp_adapters)
        self.assertIn("def hub_capability(", mcp_permissions)
        self.assertIn("class MCPClient", mcp_client)
        self.assertIn("Mcp-Session-Id", mcp_client)
        self.assertIn("class MCPSettings", config)
        self.assertIn("MCP_ENABLED = settings.mcp.enabled", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn('@api.post("/mcp")', server)
        self.assertIn('"/api/mcp"', server)
        self.assertIn("def test_client_initialize_list_and_call_roundtrip", test_mcp)
        # Pure backend change: no frontend, Service Worker cache stays put.
        self.assertIn("deepseek-infra-v186", sw)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("MCP Tool Hub", readme)
        self.assertIn("## [2.1.3]", changelog)
        self.assertIn("POST `/mcp`", api)
        self.assertIn("infra/mcp/server.py", architecture)

    def test_v215_a2a_agent_mesh_is_present(self) -> None:
        a2a = Path("deepseek_infra/infra/agent_runtime/a2a.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        conftest = Path("tests/conftest.py").read_text(encoding="utf-8")
        test_a2a = Path("tests/test_a2a.py").read_text(encoding="utf-8")
        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for symbol in (
            'A2A_PROTOCOL_VERSION = "0.3.0"',
            "def agent_card(",
            "def submit_message(",
            "def handle_a2a_message(",
            "def stream_message_events(",
            "def cancel_task(",
            "class A2AClient",
            "TASK_NOT_FOUND = -32001",
            "TASK_NOT_CANCELABLE = -32002",
            '"message/send"',
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, a2a)

        self.assertIn("class A2ASettings", config)
        self.assertIn("A2A_ENABLED = settings.a2a.enabled", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn('"/.well-known/agent-card.json"', server)
        self.assertIn('"/a2a/agents"', server)
        self.assertIn('@api.post("/a2a")', server)
        self.assertIn("A2A_TASKS_DIR", conftest)
        self.assertIn("def test_message_send_executes_task_to_completion", test_a2a)
        self.assertIn(".a2a/", gitignore)
        # Pure backend change: no frontend, Service Worker cache stays put.
        self.assertIn("deepseek-infra-v186", sw)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("A2A Agent Mesh", readme)
        self.assertIn("## [2.1.4]", changelog)
        self.assertIn("agent-card.json", api)
        self.assertIn("agent_runtime/a2a.py", architecture)

    def test_v216_context_taint_firewall_is_present(self) -> None:
        context_taint = Path("deepseek_infra/infra/gateway/context_taint.py").read_text(encoding="utf-8")
        tool_policy = Path("deepseek_infra/infra/tool_runtime/tool_policy.py").read_text(encoding="utf-8")
        deepseek_client = Path("deepseek_infra/infra/gateway/deepseek_client.py").read_text(encoding="utf-8")
        files = Path("deepseek_infra/infra/rag/files.py").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        server = Path("deepseek_infra/web/server.py").read_text(encoding="utf-8")
        test_taint = Path("tests/test_context_taint.py").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        api = Path("docs/API.md").read_text(encoding="utf-8")
        architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        for symbol in (
            "def scan_text(",
            "def classify_request_messages(",
            "def build_taint_report(",
            "def harden_search_context(",
            "def file_context_guard_line(",
            "UNTRUSTED_CONTENT_GUARD",
            'UNTRUSTED_WEB = "untrusted_web"',
            'UNTRUSTED_FILE = "untrusted_file"',
            "_EXFILTRATION_PATTERNS",
            "_TOOL_DIRECTIVE_PATTERNS",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, context_taint)

        self.assertIn("def arguments_contain_secret(", tool_policy)
        self.assertIn("secret_exfiltration_blocked", tool_policy)
        self.assertIn("taint_escalated_confirmation", tool_policy)
        self.assertIn("def mark_tainted(", tool_policy)
        self.assertIn("context_taint.harden_search_context(", deepseek_client)
        self.assertIn("context_taint.build_taint_report(", deepseek_client)
        self.assertIn('diagnostics["contextTaint"]', deepseek_client)
        self.assertIn("taint_report=prepared.diagnostics.get(\"contextTaint\")", deepseek_client)
        self.assertIn("context_taint_file_guard_line", files)
        self.assertIn("class ContextTaintSettings", config)
        self.assertIn("TAINT_ENABLED = settings.context_taint.enabled", config)
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn('"/api/taint"', server)
        self.assertIn("def test_tainted_turn_escalates_dangerous_tools_to_confirmation", test_taint)
        # Pure backend change: no frontend, Service Worker cache stays put.
        self.assertIn("deepseek-infra-v186", sw)

        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("Taint", readme)
        self.assertIn("## [2.1.5]", changelog)
        self.assertIn("/api/taint", api)
        self.assertIn("context_taint.py", architecture)

    def test_v217_credibility_and_verifiability_assets_are_present(self) -> None:
        """v2.1.7：README 里的 Infra 叙事必须落到可点击 / 可复现 / 可部署 / 可评测的资产上。"""
        readme = Path("README.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        config = Path("deepseek_infra/core/config.py").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")

        # 1. 实现状态矩阵：9 模块 Status/Code/Tests/Demo，README 链接它
        status_doc = Path("docs/IMPLEMENTATION_STATUS.md").read_text(encoding="utf-8")
        self.assertIn("| # | Module | Status | Code | Tests | Demo |", status_doc)
        self.assertIn("Context Taint Firewall", status_doc)
        self.assertIn("docs/IMPLEMENTATION_STATUS.md", readme)

        # 2. README 模块表的代码位置可点击且指向真实目录
        self.assertIn("[`infra/gateway/`](deepseek_infra/infra/gateway/)", readme)
        self.assertTrue(Path("deepseek_infra/infra/mcp").is_dir())

        # 3. 一键 Demo：四个脚本 + DEMO.md
        for example in (
            "examples/openai_compatible_client.py",
            "examples/run_agent_dag_demo.py",
            "examples/local_rag_demo.py",
            "examples/mcp_tool_demo.py",
        ):
            with self.subTest(example=example):
                self.assertTrue(Path(example).is_file())
        self.assertIn("verify_citation", Path("examples/local_rag_demo.py").read_text(encoding="utf-8"))
        self.assertIn("tools/call", Path("examples/mcp_tool_demo.py").read_text(encoding="utf-8"))
        self.assertIn("2 分钟", Path("docs/DEMO.md").read_text(encoding="utf-8"))

        # 4. 部署资产：Docker / Compose / .env 模板 / 部署文档，密钥不入库不入包
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("/healthz", dockerfile)
        self.assertIn("DEEPSEEK_MOBILE_ROOT=/data", dockerfile)
        self.assertIn("deepseek-data:/data", Path("docker-compose.yml").read_text(encoding="utf-8"))
        self.assertIn("DEEPSEEK_API_KEY=", Path(".env.example").read_text(encoding="utf-8"))
        self.assertIn(".env", Path(".dockerignore").read_text(encoding="utf-8"))
        self.assertIn(".env", Path(".gitignore").read_text(encoding="utf-8"))
        self.assertIn('".env",', Path("scripts/release.py").read_text(encoding="utf-8"))
        self.assertTrue(Path("docs/DEPLOYMENT.md").is_file())

        # 5. Benchmarks：四个脚本 + README 实测数字小节
        for bench in (
            "benchmarks/bench_chat_latency.py",
            "benchmarks/bench_agent_dag.py",
            "benchmarks/bench_rag_retrieval.py",
            "benchmarks/bench_semantic_cache.py",
        ):
            with self.subTest(bench=bench):
                self.assertTrue(Path(bench).is_file())
        self.assertIn("## Benchmarks（基准与评测）", readme)

        # 6. 第三条评测线：Tool Policy / 注入防御
        self.assertIn("secret_exfiltration_via_url", Path("evals/golden/tool_policy_cases.jsonl").read_text(encoding="utf-8"))
        run_tool_eval = Path("evals/runners/run_tool_eval.py").read_text(encoding="utf-8")
        self.assertIn("injectionDefensePassRate", run_tool_eval)
        harness = Path("deepseek_infra/infra/evaluation/harness.py").read_text(encoding="utf-8")
        self.assertIn("toolPolicyPassRate", harness)
        self.assertIn("Prompt Injection Defense Pass", harness)

        # 7. 威胁模型 + CI 安全扫描（高危基线清零）
        threat_model = Path("docs/THREAT_MODEL.md").read_text(encoding="utf-8")
        self.assertIn("SSRF", threat_model)
        self.assertIn("run_tool_eval.py", threat_model)
        self.assertIn("THREAT_MODEL.md", Path("docs/SECURITY.md").read_text(encoding="utf-8"))
        ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        for scanner in ("pip-audit", "bandit", "detect-secrets"):
            with self.subTest(scanner=scanner):
                self.assertIn(scanner, ci)
        self.assertTrue(Path(".secrets.baseline").is_file())
        self.assertIn("usedforsecurity=False", Path("deepseek_infra/infra/gateway/context_engine.py").read_text(encoding="utf-8"))

        # 8. 架构总览图进第一屏；9. Roadmap 节
        self.assertTrue(Path("docs/assets/architecture.svg").is_file())
        self.assertIn("docs/assets/architecture.svg", readme)
        self.assertIn("## Roadmap", readme)

        # 版本联动：纯后端 / 文档 / 工具链改动，前端 Service Worker 缓存不动
        self.assertIn('app_version: str = "2.1.7"', config)
        self.assertIn("version-2.1.7-blue", readme)
        self.assertIn("## [2.1.7]", changelog)
        self.assertIn("deepseek-infra-v186", sw)

    def test_v203_slides_skill_quality_upgrade_is_present(self) -> None:
        slides_skill = Path("deepseek_infra/infra/tool_runtime/slides_skill.py").read_text(encoding="utf-8")
        presentations = Path("deepseek_infra/infra/tool_runtime/presentations.py").read_text(encoding="utf-8")
        deepseek_request = Path("tests/test_deepseek_request.py").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

        for marker in (
            "win the contact-sheet test",
            "noun-swap test",
            "Blocking anti-patterns",
        ):
            self.assertIn(marker, slides_skill)
        # 与本应用不符的 pptxgenjs / artifact-tool 参考已移除，运行时只剩 create_pptx 边界
        self.assertNotIn("pptxgenjs", slides_skill)
        self.assertIn('self.assertIn("contact-sheet", dynamic_context)', deepseek_request)
        # 渲染器升级：统一 _rule helper 取代散落的描边/盒子，去掉写死英文 kicker 与填充卡片
        self.assertIn("def _rule(", presentations)
        self.assertNotIn("Key Points", presentations)
        self.assertNotIn("F8FAFC", presentations)
        self.assertIn("## [2.0.3]", changelog)


if __name__ == "__main__":
    unittest.main()
