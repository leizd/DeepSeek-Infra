/** Skill Workbench UI, Custom Skill Builder, Skill Packs and Eval Dashboard - v2.6.5 frontend integration. */

const SKILL_API = "/api/skills";
const PROJECT_API = "/api/workspace/projects";
const FIELD_TYPES = ["string", "textarea", "number", "integer", "enum", "boolean"];
const ARTIFACT_TYPES = ["md", "docx", "pdf", "pptx"];
const TOOL_OPTIONS = [
  { id: "search_files", label: "File search", risk: "read-only", description: "Search cached project and local files." },
  { id: "read_file_chunk", label: "Read file chunk", risk: "read-only", description: "Read bounded file snippets." },
  { id: "list_project_files", label: "List project files", risk: "read-only", description: "Inspect project file inventory." },
  { id: "web_search", label: "Web search", risk: "network", description: "Search the web through the approved tool runtime." },
  { id: "fetch_url", label: "Read URL", risk: "network", description: "Fetch URL content with SSRF protections." },
  { id: "create_document", label: "Create document", risk: "filesystem", description: "Generate document artifacts." },
  { id: "create_pptx", label: "Create PPT", risk: "filesystem", description: "Generate slide deck artifacts." },
  { id: "create_mindmap", label: "Create mindmap", risk: "filesystem", description: "Generate mindmap artifacts." },
  { id: "python_eval", label: "Math sandbox", risk: "requires approval", description: "Run constrained Python/math snippets." },
  { id: "recall_memory", label: "Recall memory", risk: "read-only", description: "Read memory context when policy allows it." },
  { id: "suggest_memory", label: "Suggest memory", risk: "safe", description: "Suggest memory candidates without committing them." },
];

let apiFetch = globalApiFetch;
let showToast = defaultShowToast;
let beforeOpenPanel = () => {};
let onPanelStateChange = () => {};
let onProjectOpen = () => {};
let getActiveProjectId = () => "";

function globalApiFetch(url, options = {}) {
  return fetch(url, { ...options, headers: options.headers || {} });
}

function defaultShowToast(message) {
  if (typeof window !== "undefined") window.alert(message);
}

const state = {
  skills: [],
  projects: [],
  packs: [],
  evalReport: null,
  evalCases: [],
  activeProjectId: "",
  search: "",
  runningSkillId: "",
  recentRuns: [],
  builder: {
    mode: "create",
    originalSkillId: "",
    saveAndRun: false,
  },
};

const els = {};

export function initSkillWorkbench(options = {}) {
  apiFetch = options.apiFetch || globalApiFetch;
  showToast = options.showToast || defaultShowToast;
  beforeOpenPanel = typeof options.beforeOpenPanel === "function" ? options.beforeOpenPanel : beforeOpenPanel;
  onPanelStateChange = typeof options.onPanelStateChange === "function" ? options.onPanelStateChange : onPanelStateChange;
  onProjectOpen = typeof options.onProjectOpen === "function" ? options.onProjectOpen : onProjectOpen;
  getActiveProjectId = typeof options.getActiveProjectId === "function" ? options.getActiveProjectId : getActiveProjectId;
  cacheElements();
  if (!els.skillPanel) return;
  bindEvents();
  loadSkills();
  loadProjects();
  loadPacks();
}

export function renderProjectSkillBinding(projectId, container) {
  if (!container) return;
  container.replaceChildren();
  if (!projectId) return;
  ensureSkillsLoaded()
    .then(() => fetchProjectSkills(projectId))
    .then((binding) => renderProjectSkillBindingForm(projectId, binding, container))
    .catch((error) => {
      container.textContent = `读取 Skill 绑定失败：${error.message || error}`;
    });
}

function cacheElements() {
  const ids = [
    "skillPanel",
    "closeSkillPanelButton",
    "skillButton",
    "skillSearchInput",
    "skillNewButton",
    "skillImportButton",
    "skillExportAllButton",
    "skillImportInput",
    "skillBuilderHost",
    "skillBuilderForm",
    "skillBuilderTitle",
    "skillBuilderSource",
    "skillBuilderCloseButton",
    "skillBuilderIdInput",
    "skillBuilderNameInput",
    "skillBuilderVersionInput",
    "skillBuilderDescriptionInput",
    "skillBuilderPromptInput",
    "skillBuilderFieldList",
    "skillBuilderAddFieldButton",
    "skillBuilderOutputModeSelect",
    "skillBuilderToolPicker",
    "skillBuilderMemoryScopeSelect",
    "skillBuilderMemoryReadInput",
    "skillBuilderMemoryWriteInput",
    "skillBuilderArtifactAutoSaveInput",
    "skillBuilderArtifactTypes",
    "skillBuilderProjectBindingInput",
    "skillBuilderPreview",
    "skillBuilderPreviewTitle",
    "skillBuilderPreviewBody",
    "skillBuilderError",
    "skillBuilderPreviewButton",
    "skillBuilderValidateButton",
    "skillBuilderDryRunButton",
    "skillBuilderSaveRunButton",
    "skillBuilderSaveButton",
    "skillPacksButton",
    "skillPacksHost",
    "skillPacksCloseButton",
    "skillPacksSource",
    "skillPacksHint",
    "skillPackImportButton",
    "skillPackImportInput",
    "skillPackImportSummary",
    "skillBuiltinPackList",
    "skillCustomPackList",
    "skillEvalButton",
    "skillEvalHost",
    "skillEvalCloseButton",
    "skillEvalSource",
    "skillEvalRunButton",
    "skillEvalExportJsonButton",
    "skillEvalExportMarkdownButton",
    "skillEvalCopySummaryButton",
    "skillEvalSummary",
    "skillEvalCaseForm",
    "skillEvalCaseSkillSelect",
    "skillEvalCaseIdInput",
    "skillEvalArtifactsInput",
    "skillEvalInputTextarea",
    "skillEvalKeywordsInput",
    "skillEvalPathsInput",
    "skillEvalForbiddenInput",
    "skillEvalProjectBindingInput",
    "skillEvalSaveCaseButton",
    "skillEvalCaseList",
    "skillEvalSkillList",
    "skillEvalPackList",
    "skillBuiltinList",
    "skillCustomList",
    "skillRecentRunList",
    "skillRunHost",
    "skillRunForm",
    "skillRunTitle",
    "skillRunFields",
    "skillRunProjectSelect",
    "skillRunModeSelect",
    "skillRunCancelButton",
    "skillRunSubmitButton",
    "skillRunResult",
    "skillRunResultBody",
    "skillRunResultActions",
    "skillRunSavedItemsLink",
    "skillRunArtifactsLink",
    "projectSkills",
    "projectSkillsBody",
  ];
  for (const id of ids) {
    els[id] = document.querySelector(`#${id}`);
  }
}

function bindEvents() {
  els.closeSkillPanelButton?.addEventListener("click", closeSkillPanel);
  els.skillButton?.addEventListener("click", openSkillPanel);
  els.skillSearchInput?.addEventListener("input", () => {
    state.search = (els.skillSearchInput.value || "").trim().toLowerCase();
    renderSkillPanel();
  });
  els.skillNewButton?.addEventListener("click", () => openSkillBuilder({ mode: "create" }));
  els.skillPacksButton?.addEventListener("click", openPacksHost);
  els.skillEvalButton?.addEventListener("click", openEvalHost);
  els.skillEvalCloseButton?.addEventListener("click", closeEvalHost);
  els.skillEvalRunButton?.addEventListener("click", runSkillEval);
  els.skillEvalExportJsonButton?.addEventListener("click", exportSkillEvalJson);
  els.skillEvalExportMarkdownButton?.addEventListener("click", exportSkillEvalMarkdown);
  els.skillEvalCopySummaryButton?.addEventListener("click", copySkillEvalSummary);
  els.skillEvalCaseForm?.addEventListener("submit", saveSkillEvalCase);
  els.skillPacksCloseButton?.addEventListener("click", closePacksHost);
  els.skillPackImportButton?.addEventListener("click", () => {
    if (els.skillPackImportInput) {
      els.skillPackImportInput.value = "";
      els.skillPackImportInput.click();
    }
  });
  els.skillPackImportInput?.addEventListener("change", importPackFromFile);
  els.skillBuiltinPackList?.addEventListener("click", onPackListClick);
  els.skillCustomPackList?.addEventListener("click", onPackListClick);
  els.skillImportButton?.addEventListener("click", () => {
    if (els.skillImportInput) {
      els.skillImportInput.value = "";
      els.skillImportInput.click();
    }
  });
  els.skillImportInput?.addEventListener("change", importSkillFromFile);
  els.skillExportAllButton?.addEventListener("click", exportAllCustomSkills);
  els.skillBuiltinList?.addEventListener("click", onSkillListClick);
  els.skillCustomList?.addEventListener("click", onSkillListClick);
  els.skillBuilderCloseButton?.addEventListener("click", closeSkillBuilder);
  els.skillBuilderAddFieldButton?.addEventListener("click", () => addBuilderField());
  els.skillBuilderForm?.addEventListener("submit", onSkillBuilderSubmit);
  els.skillBuilderPreviewButton?.addEventListener("click", previewBuilderConfig);
  els.skillBuilderValidateButton?.addEventListener("click", validateBuilderConfig);
  els.skillBuilderDryRunButton?.addEventListener("click", dryRunBuilderConfig);
  els.skillBuilderSaveRunButton?.addEventListener("click", () => saveBuilderConfig({ runAfterSave: true }));
  els.skillBuilderFieldList?.addEventListener("click", onBuilderFieldListClick);
  els.skillBuilderFieldList?.addEventListener("change", onBuilderFieldListChange);
  els.skillRunCancelButton?.addEventListener("click", closeSkillRunHost);
  els.skillRunForm?.addEventListener("submit", onSkillRunSubmit);
  els.skillRunProjectSelect?.addEventListener("change", () => {
    state.activeProjectId = els.skillRunProjectSelect.value || "";
  });
  els.skillRunSavedItemsLink?.addEventListener("click", (event) => {
    event.preventDefault();
    openProjectPath("saved-items", state.activeProjectId);
  });
  els.skillRunArtifactsLink?.addEventListener("click", (event) => {
    event.preventDefault();
    openProjectPath("artifacts", state.activeProjectId);
  });
  renderBuilderToolPicker();
  renderBuilderArtifactTypes();
}

function openSkillPanel() {
  if (!els.skillPanel) return;
  beforeOpenPanel();
  closeSkillRunHost();
  closePacksHost();
  closeEvalHost();
  loadSkills();
  loadProjects();
  loadPacks();
  els.skillPanel.classList.add("open");
  els.skillPanel.setAttribute("aria-hidden", "false");
  onPanelStateChange();
}

function closeSkillPanel() {
  if (!els.skillPanel) return;
  closeSkillBuilder();
  closePacksHost();
  closeEvalHost();
  els.skillPanel.classList.remove("open");
  els.skillPanel.setAttribute("aria-hidden", "true");
  onPanelStateChange();
}

function openSkillRunHost(skill) {
  if (!els.skillRunHost || !skill) return;
  closeSkillBuilder();
  closePacksHost();
  closeEvalHost();
  els.skillRunHost.hidden = false;
  els.skillRunTitle.textContent = `运行 · ${skill.name || skill.skillId}`;
  els.skillRunTitle.dataset.skillId = skill.skillId;
  renderRunFields(skill.inputSchema || {});
  populateProjectSelect();
  if (els.skillRunResult) els.skillRunResult.hidden = true;
}

function closeSkillRunHost() {
  if (els.skillRunHost) els.skillRunHost.hidden = true;
  if (els.skillRunResult) els.skillRunResult.hidden = true;
  state.runningSkillId = "";
}

function populateProjectSelect() {
  if (!els.skillRunProjectSelect) return;
  state.activeProjectId = getActiveProjectId() || state.activeProjectId || "";
  els.skillRunProjectSelect.replaceChildren();
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "不绑定项目";
  els.skillRunProjectSelect.append(none);
  for (const project of state.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.name;
    if (project.id === state.activeProjectId) option.selected = true;
    els.skillRunProjectSelect.append(option);
  }
}

function renderRunFields(schema) {
  if (!els.skillRunFields) return;
  els.skillRunFields.replaceChildren();
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  for (const [key, prop] of Object.entries(properties)) {
    const label = document.createElement("label");
    label.className = "skill-field";
    const span = document.createElement("span");
    span.textContent = `${prop.title || key}${required.has(key) ? " *" : ""}`;
    let input;
    if (prop.enum && Array.isArray(prop.enum)) {
      input = document.createElement("select");
      for (const choice of prop.enum) {
        const option = document.createElement("option");
        option.value = String(choice);
        option.textContent = String(choice);
        input.append(option);
      }
    } else if ((prop.type === "string" || prop.type === "text") && (prop.maxLength || 0) > 120) {
      input = document.createElement("textarea");
      input.rows = Math.min(8, Math.max(3, Math.ceil((prop.maxLength || 500) / 80)));
    } else {
      input = document.createElement("input");
      input.type = prop.type === "number" || prop.type === "integer" ? "number" : "text";
    }
    input.dataset.runField = key;
    input.dataset.runType = prop.type || "string";
    if (prop.description) input.placeholder = prop.description;
    if (required.has(key)) input.required = true;
    label.append(span, input);
    els.skillRunFields.append(label);
  }
  if (!Object.keys(properties).length) {
    const note = document.createElement("p");
    note.className = "panel-empty";
    note.textContent = "该 Skill 不需要输入参数。";
    els.skillRunFields.append(note);
  }
}

function collectRunInput() {
  const input = {};
  for (const field of els.skillRunFields?.querySelectorAll("[data-run-field]") || []) {
    const key = field.dataset.runField;
    const type = field.dataset.runType;
    let value = field.value;
    if (type === "number" || type === "integer") value = value === "" ? null : Number(value);
    if (value !== null && value !== "") input[key] = value;
  }
  return input;
}

async function onSkillRunSubmit(event) {
  event.preventDefault();
  const skillId = els.skillRunTitle?.dataset.skillId;
  if (!skillId || !els.skillRunSubmitButton) return;
  const input = collectRunInput();
  const projectId = els.skillRunProjectSelect?.value || "";
  const offline = els.skillRunModeSelect?.value === "offline";
  els.skillRunSubmitButton.disabled = true;
  els.skillRunSubmitButton.textContent = "运行中…";
  try {
    const payload = {
      action: "run",
      skillId,
      input,
      projectId,
      offline,
      persist: true,
    };
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await parseJsonResponse(response);
    if (!data.ok) throw new Error(data.error || "运行失败");
    state.runningSkillId = skillId;
    renderRunResult(data, projectId);
    if (projectId) loadRecentRuns(projectId);
  } catch (error) {
    showToast(`Skill 运行失败：${error.message || error}`);
  } finally {
    els.skillRunSubmitButton.disabled = false;
    els.skillRunSubmitButton.textContent = "运行";
  }
}

function renderRunResult(result, projectId) {
  if (!els.skillRunResult || !els.skillRunResultBody) return;
  els.skillRunResult.hidden = false;
  els.skillRunResultBody.replaceChildren();
  const output = result.output || {};
  const content = String(output.content || output.text || JSON.stringify(output, null, 2));
  const pre = document.createElement("pre");
  pre.className = "skill-run-content";
  pre.textContent = content.slice(0, 4000);
  els.skillRunResultBody.append(pre);

  const meta = document.createElement("p");
  meta.className = "skill-run-meta-text";
  meta.textContent = `runId: ${result.skillRunId || ""}${projectId ? ` · project: ${projectId}` : ""}`;
  els.skillRunResultBody.append(meta);

  const linked = [];
  for (const item of result.savedItems || []) {
    linked.push(`Saved Item: ${item.title || item.id || item.savedItemId || "saved item"}`);
  }
  for (const artifact of result.artifacts || []) {
    linked.push(`Artifact: ${artifact.name || artifact.title || artifact.id || artifact.artifactId || "artifact"}`);
  }
  if (linked.length) {
    const list = document.createElement("ul");
    list.className = "skill-run-linked-list";
    for (const text of linked) {
      const row = document.createElement("li");
      row.textContent = text;
      list.append(row);
    }
    els.skillRunResultBody.append(list);
  }

  if (els.skillRunResultActions) {
    const hasLinks = Boolean(projectId) && ((result.savedItems?.length) || (result.artifacts?.length));
    els.skillRunResultActions.hidden = !hasLinks;
  }
}

async function loadSkills() {
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "list" }),
    });
    const data = await parseJsonResponse(response);
    state.skills = Array.isArray(data.skills) ? data.skills : [];
    renderSkillPanel();
  } catch (error) {
    showToast(`读取 Skill 列表失败：${error.message || error}`);
  }
}

async function ensureSkillsLoaded() {
  if (state.skills.length) return;
  await loadSkills();
}

async function loadProjects() {
  try {
    const response = await apiFetch(`${PROJECT_API}`, { method: "GET" });
    const data = await parseJsonResponse(response);
    state.projects = Array.isArray(data.projects) ? data.projects : [];
    populateProjectSelect();
  } catch {
    state.projects = [];
  }
}

async function loadRecentRuns(projectId) {
  if (!projectId) {
    state.recentRuns = [];
    renderRecentRuns();
    return;
  }
  try {
    const response = await apiFetch(`${PROJECT_API}/${projectId}/skill-runs?limit=20`, { method: "GET" });
    const data = await parseJsonResponse(response);
    state.recentRuns = Array.isArray(data.skillRuns) ? data.skillRuns : [];
    renderRecentRuns();
  } catch {
    state.recentRuns = [];
    renderRecentRuns();
  }
}

function renderSkillPanel() {
  if (!els.skillBuiltinList || !els.skillCustomList) return;
  const query = state.search;
  const matches = (skill) =>
    !query || [skill.name, skill.description, skill.skillId].join(" ").toLowerCase().includes(query);
  renderSkillCards(els.skillBuiltinList, state.skills.filter((skill) => skill.builtin && matches(skill)), "没有匹配的内置 Skill。");
  renderSkillCards(els.skillCustomList, state.skills.filter((skill) => !skill.builtin && matches(skill)), "还没有自定义 Skill，可从内置 Skill 导出后导入。");
  renderRecentRuns();
  populateEvalSkillSelect();
}

function renderSkillCards(host, skills, emptyText) {
  host.replaceChildren();
  if (!skills.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = emptyText;
    host.append(empty);
    return;
  }
  for (const skill of skills) {
    host.append(renderSkillCard(skill));
  }
}

function renderSkillCard(skill) {
  const card = document.createElement("article");
  card.className = "skill-card";
  card.classList.toggle("disabled", Boolean(skill.disabled));
  card.dataset.skillId = skill.skillId;

  const body = document.createElement("div");
  body.className = "skill-card-body";

  const title = document.createElement("h4");
  title.textContent = skill.name || skill.skillId;
  const description = document.createElement("p");
  description.textContent = skill.description || "";
  body.append(title, description);

  const tags = document.createElement("div");
  tags.className = "skill-card-tags";
  const builtinTag = document.createElement("span");
  builtinTag.className = "skill-tag";
  builtinTag.textContent = skill.builtin ? "内置" : "自定义";
  tags.append(builtinTag);
  const toolsTag = document.createElement("span");
  toolsTag.className = "skill-tag";
  toolsTag.textContent = `${(skill.allowedTools || []).length} 个工具`;
  tags.append(toolsTag);
  body.append(tags);

  const actions = document.createElement("div");
  actions.className = "skill-card-actions";

  const run = document.createElement("button");
  run.type = "button";
  run.className = "seek-primary-button";
  run.dataset.skillRun = skill.skillId;
  run.textContent = "运行";
  actions.append(run);

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "secondary-button";
  toggle.dataset.skillToggle = skill.skillId;
  toggle.textContent = skill.disabled ? "启用" : "禁用";
  actions.append(toggle);

  const exportButton = document.createElement("button");
  exportButton.type = "button";
  exportButton.className = "secondary-button";
  exportButton.dataset.skillExport = skill.skillId;
  exportButton.textContent = "导出";
  actions.append(exportButton);

  const clone = document.createElement("button");
  clone.type = "button";
  clone.className = "secondary-button";
  clone.dataset.skillClone = skill.skillId;
  clone.textContent = skill.builtin ? "克隆" : "复制";
  actions.append(clone);

  if (!skill.builtin) {
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "secondary-button";
    edit.dataset.skillEdit = skill.skillId;
    edit.textContent = "编辑";
    actions.append(edit);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger-button";
    remove.dataset.skillDelete = skill.skillId;
    remove.textContent = "删除";
    actions.append(remove);
  }

  card.append(body, actions);
  return card;
}

function onSkillListClick(event) {
  const target = event.target instanceof Element ? event.target : event.target?.parentElement;
  const runButton = target?.closest("button[data-skill-run]");
  if (runButton) {
    const skill = state.skills.find((item) => item.skillId === runButton.dataset.skillRun);
    if (skill) openSkillRunHost(skill);
    return;
  }
  const toggleButton = target?.closest("button[data-skill-toggle]");
  if (toggleButton) {
    toggleSkill(toggleButton.dataset.skillToggle);
    return;
  }
  const exportButton = target?.closest("button[data-skill-export]");
  if (exportButton) {
    exportSkill(exportButton.dataset.skillExport);
    return;
  }
  const cloneButton = target?.closest("button[data-skill-clone]");
  if (cloneButton) {
    cloneSkill(cloneButton.dataset.skillClone);
    return;
  }
  const editButton = target?.closest("button[data-skill-edit]");
  if (editButton) {
    editSkill(editButton.dataset.skillEdit);
    return;
  }
  const deleteButton = target?.closest("button[data-skill-delete]");
  if (deleteButton) {
    deleteSkill(deleteButton.dataset.skillDelete);
  }
}

function defaultBuilderSkill() {
  return {
    skillId: `skill_custom_${Date.now().toString(36)}`,
    name: "My Custom Skill",
    description: "A reusable custom Skill.",
    version: "1.0.0",
    systemPrompt: "You are a focused Skill. Follow the input schema, use only allowed tools, and return concise markdown.",
    inputSchema: {
      type: "object",
      properties: {
        topic: {
          type: "string",
          title: "Topic",
          description: "What should this Skill work on?",
          maxLength: 500,
        },
      },
      required: ["topic"],
      additionalProperties: false,
    },
    outputSchema: defaultOutputSchema("content"),
    allowedTools: ["search_files"],
    memoryPolicy: { scope: "project", read: true, write: false },
    artifactPolicy: { autoSave: true, types: ["md"] },
    projectBinding: { enabled: true },
    exampleInputs: [{ topic: "Example topic" }],
  };
}

function openSkillBuilder({ mode = "create", skill = null } = {}) {
  if (!els.skillBuilderHost) return;
  closeSkillRunHost();
  closePacksHost();
  closeEvalHost();
  state.builder.mode = mode;
  state.builder.originalSkillId = mode === "edit" ? (skill?.skillId || "") : "";
  state.builder.saveAndRun = false;
  const source = skill ? cloneForBuilder(skill, mode) : defaultBuilderSkill();
  populateBuilderForm(source, mode);
  els.skillBuilderHost.hidden = false;
  els.skillBuilderHost.scrollIntoView({ block: "start", behavior: "smooth" });
}

function cloneForBuilder(skill, mode) {
  const clone = JSON.parse(JSON.stringify(skill || defaultBuilderSkill()));
  delete clone.builtin;
  delete clone.createdAt;
  delete clone.updatedAt;
  delete clone.disabled;
  if (mode === "clone") {
    clone.skillId = nextCloneSkillId(clone.skillId || "skill_custom");
    clone.name = `${clone.name || "Custom Skill"} Copy`;
    clone.description = clone.description || "Cloned custom Skill.";
  }
  return clone;
}

function nextCloneSkillId(skillId) {
  const base = String(skillId || "skill_custom").replace(/[^A-Za-z0-9_:-]/g, "_").slice(0, 60);
  const candidate = `${base}_custom`;
  if (!state.skills.some((item) => item.skillId === candidate)) return candidate;
  return `${base}_${Date.now().toString(36)}`.slice(0, 80);
}

function populateBuilderForm(skill, mode) {
  if (!els.skillBuilderForm) return;
  const title = mode === "edit" ? "Edit Custom Skill" : mode === "clone" ? "Clone as Custom Skill" : "New Custom Skill";
  els.skillBuilderTitle.textContent = title;
  els.skillBuilderSource.textContent =
    mode === "clone"
      ? "Start from a built-in Skill, then change schema, tools and policies before saving as custom."
      : "Create a reusable Skill from prompt, schema, tools and workspace policy.";
  els.skillBuilderIdInput.value = skill.skillId || "";
  els.skillBuilderIdInput.readOnly = mode === "edit";
  els.skillBuilderNameInput.value = skill.name || "";
  els.skillBuilderVersionInput.value = skill.version || "1.0.0";
  els.skillBuilderDescriptionInput.value = skill.description || "";
  els.skillBuilderPromptInput.value = skill.systemPrompt || "";
  els.skillBuilderOutputModeSelect.value = outputModeFromSchema(skill.outputSchema || {});
  els.skillBuilderMemoryScopeSelect.value = skill.memoryPolicy?.scope || "none";
  els.skillBuilderMemoryReadInput.checked = Boolean(skill.memoryPolicy?.read);
  els.skillBuilderMemoryWriteInput.checked = Boolean(skill.memoryPolicy?.write);
  els.skillBuilderArtifactAutoSaveInput.checked = Boolean(skill.artifactPolicy?.autoSave);
  els.skillBuilderProjectBindingInput.checked = Boolean(skill.projectBinding?.enabled);
  setCheckedValues("builder-tool", skill.allowedTools || []);
  setCheckedValues("builder-artifact", skill.artifactPolicy?.types || []);
  renderBuilderFields(skill.inputSchema || {});
  hideBuilderMessage();
  hideBuilderPreview();
}

function closeSkillBuilder() {
  if (els.skillBuilderHost) els.skillBuilderHost.hidden = true;
  hideBuilderMessage();
}

function renderBuilderToolPicker() {
  if (!els.skillBuilderToolPicker) return;
  els.skillBuilderToolPicker.replaceChildren();
  for (const tool of TOOL_OPTIONS) {
    const label = document.createElement("label");
    label.className = "skill-tool-option";
    label.innerHTML = `
      <input type="checkbox" data-builder-tool value="${tool.id}" />
      <span>
        <strong>${tool.label}</strong>
        <small>${tool.description}</small>
      </span>
      <em data-risk="${tool.risk}">${tool.risk}</em>
    `;
    els.skillBuilderToolPicker.append(label);
  }
}

function renderBuilderArtifactTypes() {
  if (!els.skillBuilderArtifactTypes) return;
  els.skillBuilderArtifactTypes.replaceChildren();
  for (const type of ARTIFACT_TYPES) {
    const label = document.createElement("label");
    label.className = "skill-builder-chip";
    label.innerHTML = `<input type="checkbox" data-builder-artifact value="${type}" /> ${type}`;
    els.skillBuilderArtifactTypes.append(label);
  }
}

function setCheckedValues(kind, values) {
  const selected = new Set(values || []);
  const selector = kind === "builder-tool" ? "[data-builder-tool]" : "[data-builder-artifact]";
  for (const input of els.skillBuilderHost?.querySelectorAll(selector) || []) {
    input.checked = selected.has(input.value);
  }
}

function renderBuilderFields(schema) {
  if (!els.skillBuilderFieldList) return;
  els.skillBuilderFieldList.replaceChildren();
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  for (const [key, prop] of Object.entries(properties)) {
    addBuilderField({
      key,
      title: prop.title || key,
      description: prop.description || "",
      type: fieldTypeFromSchema(prop),
      required: required.has(key),
      defaultValue: prop.default ?? "",
      enumOptions: Array.isArray(prop.enum) ? prop.enum.join(", ") : "",
      maxLength: prop.maxLength || "",
    });
  }
  if (!Object.keys(properties).length) addBuilderField();
}

function addBuilderField(field = {}) {
  if (!els.skillBuilderFieldList) return;
  const row = document.createElement("article");
  row.className = "skill-builder-field-row";
  row.dataset.builderFieldRow = "true";
  row.innerHTML = `
    <div class="skill-builder-field-grid">
      <label><span>Key</span><input data-builder-field-key type="text" placeholder="topic" /></label>
      <label><span>Title</span><input data-builder-field-title type="text" placeholder="Topic" /></label>
      <label><span>Type</span><select data-builder-field-type></select></label>
      <label><span>Required</span><select data-builder-field-required><option value="true">required</option><option value="false">optional</option></select></label>
    </div>
    <label class="skill-builder-field-row-description"><span>Description</span><input data-builder-field-description type="text" placeholder="Help text shown in run form" /></label>
    <div class="skill-builder-field-grid compact">
      <label><span>Default</span><input data-builder-field-default type="text" /></label>
      <label data-builder-enum-wrap><span>Enum options</span><input data-builder-field-enum type="text" placeholder="quick, standard, deep" /></label>
      <label><span>Max length</span><input data-builder-field-max type="number" min="1" step="1" /></label>
      <button class="danger-button" data-builder-field-remove type="button">Remove</button>
    </div>
  `;
  const typeSelect = row.querySelector("[data-builder-field-type]");
  for (const type of FIELD_TYPES) {
    const option = document.createElement("option");
    option.value = type;
    option.textContent = type;
    typeSelect.append(option);
  }
  row.querySelector("[data-builder-field-key]").value = field.key || "";
  row.querySelector("[data-builder-field-title]").value = field.title || "";
  row.querySelector("[data-builder-field-type]").value = field.type || "string";
  row.querySelector("[data-builder-field-required]").value = field.required === false ? "false" : "true";
  row.querySelector("[data-builder-field-description]").value = field.description || "";
  row.querySelector("[data-builder-field-default]").value = field.defaultValue ?? "";
  row.querySelector("[data-builder-field-enum]").value = field.enumOptions || "";
  row.querySelector("[data-builder-field-max]").value = field.maxLength || "";
  updateBuilderFieldRow(row);
  els.skillBuilderFieldList.append(row);
}

function onBuilderFieldListClick(event) {
  const target = event.target instanceof Element ? event.target : event.target?.parentElement;
  const remove = target?.closest("[data-builder-field-remove]");
  if (!remove) return;
  remove.closest("[data-builder-field-row]")?.remove();
  if (!els.skillBuilderFieldList?.querySelector("[data-builder-field-row]")) addBuilderField();
}

function onBuilderFieldListChange(event) {
  const target = event.target instanceof Element ? event.target : null;
  const row = target?.closest("[data-builder-field-row]");
  if (row) updateBuilderFieldRow(row);
}

function updateBuilderFieldRow(row) {
  const type = row.querySelector("[data-builder-field-type]")?.value || "string";
  const enumWrap = row.querySelector("[data-builder-enum-wrap]");
  if (enumWrap) enumWrap.hidden = type !== "enum";
}

function fieldTypeFromSchema(prop) {
  if (Array.isArray(prop.enum)) return "enum";
  if (prop.type === "string" && Number(prop.maxLength || 0) > 120) return "textarea";
  if (FIELD_TYPES.includes(prop.type)) return prop.type;
  return "string";
}

function outputModeFromSchema(schema) {
  return schema.properties?.title ? "title_content" : "content";
}

function defaultOutputSchema(mode) {
  const properties = {
    content: { type: "string" },
    mode: { type: "string" },
  };
  const required = ["content"];
  if (mode === "title_content") {
    properties.title = { type: "string" };
    required.unshift("title");
  }
  return { type: "object", properties, required, additionalProperties: true };
}

function buildSkillConfigFromBuilder() {
  const skillId = (els.skillBuilderIdInput?.value || "").trim();
  const fields = collectBuilderFields();
  return {
    skillId,
    name: (els.skillBuilderNameInput?.value || "").trim(),
    description: (els.skillBuilderDescriptionInput?.value || "").trim(),
    version: (els.skillBuilderVersionInput?.value || "1.0.0").trim(),
    systemPrompt: (els.skillBuilderPromptInput?.value || "").trim(),
    inputSchema: buildInputSchema(fields),
    outputSchema: defaultOutputSchema(els.skillBuilderOutputModeSelect?.value || "content"),
    allowedTools: selectedBuilderValues("[data-builder-tool]"),
    memoryPolicy: {
      scope: els.skillBuilderMemoryScopeSelect?.value || "none",
      read: Boolean(els.skillBuilderMemoryReadInput?.checked),
      write: Boolean(els.skillBuilderMemoryWriteInput?.checked),
    },
    artifactPolicy: {
      autoSave: Boolean(els.skillBuilderArtifactAutoSaveInput?.checked),
      types: selectedBuilderValues("[data-builder-artifact]"),
    },
    projectBinding: { enabled: Boolean(els.skillBuilderProjectBindingInput?.checked) },
    exampleInputs: [sampleInputFromFields(fields)],
  };
}

function collectBuilderFields() {
  const fields = [];
  for (const row of els.skillBuilderFieldList?.querySelectorAll("[data-builder-field-row]") || []) {
    const key = (row.querySelector("[data-builder-field-key]")?.value || "").trim();
    if (!key) continue;
    const type = row.querySelector("[data-builder-field-type]")?.value || "string";
    fields.push({
      key,
      title: (row.querySelector("[data-builder-field-title]")?.value || key).trim(),
      description: (row.querySelector("[data-builder-field-description]")?.value || "").trim(),
      type,
      required: row.querySelector("[data-builder-field-required]")?.value !== "false",
      defaultValue: row.querySelector("[data-builder-field-default]")?.value ?? "",
      enumOptions: (row.querySelector("[data-builder-field-enum]")?.value || "").split(",").map((item) => item.trim()).filter(Boolean),
      maxLength: Number(row.querySelector("[data-builder-field-max]")?.value || 0),
    });
  }
  return fields;
}

function buildInputSchema(fields) {
  const properties = {};
  const required = [];
  for (const field of fields) {
    const prop = builderFieldToSchema(field);
    properties[field.key] = prop;
    if (field.required) required.push(field.key);
  }
  return { type: "object", properties, required, additionalProperties: false };
}

function builderFieldToSchema(field) {
  const prop = {
    type: field.type === "textarea" || field.type === "enum" ? "string" : field.type,
    title: field.title || field.key,
  };
  if (field.description) prop.description = field.description;
  if (field.type === "enum") prop.enum = field.enumOptions.length ? field.enumOptions : ["option"];
  if (field.type === "textarea") prop.maxLength = field.maxLength || 2000;
  if (field.maxLength && (field.type === "string" || field.type === "textarea")) prop.maxLength = field.maxLength;
  const defaultValue = parseBuilderDefault(field);
  if (defaultValue !== undefined) prop.default = defaultValue;
  return prop;
}

function parseBuilderDefault(field) {
  const value = field.defaultValue;
  if (value === "") return undefined;
  if (field.type === "number") return Number(value);
  if (field.type === "integer") return parseInt(value, 10);
  if (field.type === "boolean") return value === "true" || value === "1" || value.toLowerCase() === "yes";
  return value;
}

function selectedBuilderValues(selector) {
  return Array.from(els.skillBuilderHost?.querySelectorAll(selector) || [])
    .filter((input) => input.checked)
    .map((input) => input.value);
}

function sampleInputFromFields(fields) {
  const input = {};
  for (const field of fields) {
    if (!field.required && field.defaultValue === "") continue;
    const defaultValue = parseBuilderDefault(field);
    if (defaultValue !== undefined) {
      input[field.key] = defaultValue;
    } else if (field.type === "boolean") {
      input[field.key] = true;
    } else if (field.type === "number") {
      input[field.key] = 1.5;
    } else if (field.type === "integer") {
      input[field.key] = 1;
    } else if (field.type === "enum") {
      input[field.key] = field.enumOptions[0] || "option";
    } else {
      input[field.key] = `Sample ${field.title || field.key}`;
    }
  }
  return input;
}

function previewBuilderConfig() {
  try {
    const config = buildSkillConfigFromBuilder();
    showBuilderPreview("Skill JSON Preview", config);
    setBuilderMessage("Preview generated locally.", "ok");
  } catch (error) {
    setBuilderMessage(error.message || String(error), "error");
  }
}

async function validateBuilderConfig() {
  const config = buildSkillConfigFromBuilder();
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "validate", skill: config }),
    });
    const data = await parseJsonResponse(response);
    showBuilderPreview("Validated Skill JSON", data.skill || config);
    setBuilderMessage("Schema validation passed.", "ok");
    return data.skill || config;
  } catch (error) {
    setBuilderMessage(`Validation failed: ${error.message || error}`, "error");
    throw error;
  }
}

async function dryRunBuilderConfig() {
  const config = await validateBuilderConfig();
  const input = sampleInputFromFields(collectBuilderFields());
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "dry_run", skill: config, input, offline: true, persist: false }),
    });
    const data = await parseJsonResponse(response);
    showBuilderPreview("Dry Run Offline Result", { input, output: data.output, policy: data.policy });
    setBuilderMessage("Offline dry-run passed.", "ok");
  } catch (error) {
    setBuilderMessage(`Dry run failed: ${error.message || error}`, "error");
  }
}

async function onSkillBuilderSubmit(event) {
  event.preventDefault();
  await saveBuilderConfig({ runAfterSave: false });
}

async function saveBuilderConfig({ runAfterSave = false } = {}) {
  const config = buildSkillConfigFromBuilder();
  const mode = state.builder.mode;
  const action = mode === "edit" ? "update" : "create";
  const payload = action === "update"
    ? { action, skillId: state.builder.originalSkillId || config.skillId, patch: config }
    : { action, skill: config, overwrite: false };
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await parseJsonResponse(response);
    await loadSkills();
    const saved = data.skill || config;
    setBuilderMessage(`Saved ${saved.name || saved.skillId}.`, "ok");
    if (runAfterSave) {
      closeSkillBuilder();
      openSkillRunHost(saved);
    }
  } catch (error) {
    setBuilderMessage(`Save failed: ${error.message || error}`, "error");
  }
}

function showBuilderPreview(title, payload) {
  if (!els.skillBuilderPreview || !els.skillBuilderPreviewBody) return;
  els.skillBuilderPreview.hidden = false;
  if (els.skillBuilderPreviewTitle) els.skillBuilderPreviewTitle.textContent = title;
  els.skillBuilderPreviewBody.textContent = `${JSON.stringify(payload, null, 2)}\n`;
}

function hideBuilderPreview() {
  if (els.skillBuilderPreview) els.skillBuilderPreview.hidden = true;
  if (els.skillBuilderPreviewBody) els.skillBuilderPreviewBody.textContent = "";
}

function setBuilderMessage(message, kind = "error") {
  if (!els.skillBuilderError) return;
  els.skillBuilderError.hidden = false;
  els.skillBuilderError.dataset.kind = kind;
  els.skillBuilderError.textContent = message;
}

function hideBuilderMessage() {
  if (!els.skillBuilderError) return;
  els.skillBuilderError.hidden = true;
  els.skillBuilderError.textContent = "";
}

function cloneSkill(skillId) {
  const skill = state.skills.find((item) => item.skillId === skillId);
  if (skill) openSkillBuilder({ mode: "clone", skill });
}

function editSkill(skillId) {
  const skill = state.skills.find((item) => item.skillId === skillId);
  if (skill) openSkillBuilder({ mode: "edit", skill });
}

async function toggleSkill(skillId) {
  const skill = state.skills.find((item) => item.skillId === skillId);
  if (!skill) return;
  const action = skill.disabled ? "enable" : "disable";
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, skillId }),
    });
    await parseJsonResponse(response);
    await loadSkills();
    showToast(`${skill.name || skillId} 已${action === "enable" ? "启用" : "禁用"}`);
  } catch (error) {
    showToast(`切换 Skill 状态失败：${error.message || error}`);
  }
}

async function exportSkill(skillId) {
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "export", skillId }),
    });
    const data = await parseJsonResponse(response);
    if (!data.ok) throw new Error(data.error || "导出失败");
    const text = `${JSON.stringify(data.skill || {}, null, 2)}\n`;
    downloadTextFile(text, `skill-${skillId}.json`, "application/json;charset=utf-8");
  } catch (error) {
    showToast(`导出 Skill 失败：${error.message || error}`);
  }
}

async function exportAllCustomSkills() {
  const custom = state.skills.filter((item) => !item.builtin);
  if (!custom.length) {
    showToast("没有可导出的自定义 Skill");
    return;
  }
  try {
    const exported = [];
    for (const skill of custom) {
      const response = await apiFetch(SKILL_API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "export", skillId: skill.skillId }),
      });
      const data = await parseJsonResponse(response);
      if (data.ok && data.skill) exported.push(data.skill);
    }
    const text = `${JSON.stringify({ exportedAt: new Date().toISOString(), skills: exported }, null, 2)}\n`;
    downloadTextFile(text, `deepseek-skills-${new Date().toISOString().slice(0, 10)}.json`, "application/json;charset=utf-8");
    showToast(`已导出 ${exported.length} 个自定义 Skill`);
  } catch (error) {
    showToast(`导出失败：${error.message || error}`);
  }
}

async function importSkillFromFile(event) {
  const file = event.target?.files?.[0];
  if (!file) return;
  try {
    const text = await file.text();
    const payload = JSON.parse(text);
    const skills = Array.isArray(payload.skills) ? payload.skills : [payload];
    let imported = 0;
    for (const skill of skills) {
      if (!skill.skillId) continue;
      const response = await apiFetch(SKILL_API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "import", skill, overwrite: true }),
      });
      const data = await parseJsonResponse(response);
      if (data.ok) imported += 1;
    }
    showToast(`已导入 ${imported} 个 Skill`);
    await loadSkills();
  } catch (error) {
    showToast(`导入失败：${error.message || error}`);
  } finally {
    if (els.skillImportInput) els.skillImportInput.value = "";
  }
}

async function deleteSkill(skillId) {
  const skill = state.skills.find((item) => item.skillId === skillId);
  if (!skill) return;
  if (!window.confirm(`删除自定义 Skill「${skill.name || skillId}」？`)) return;
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "delete", skillId }),
    });
    await parseJsonResponse(response);
    await loadSkills();
  } catch (error) {
    showToast(`删除失败：${error.message || error}`);
  }
}

// --- Skill Eval Dashboard (v2.6.5) -------------------------------------------

function openEvalHost() {
  if (!els.skillEvalHost) return;
  closeSkillBuilder();
  closeSkillRunHost();
  closePacksHost();
  els.skillEvalHost.hidden = false;
  populateEvalSkillSelect();
  loadEvalCases();
  if (!state.evalReport) renderEmptyEvalDashboard();
}

function closeEvalHost() {
  if (els.skillEvalHost) els.skillEvalHost.hidden = true;
}

function renderEmptyEvalDashboard() {
  if (els.skillEvalSummary) {
    els.skillEvalSummary.replaceChildren();
    els.skillEvalSummary.append(evalMetricCard("Status", "Not run", "Run offline eval to score Skills and Packs."));
    els.skillEvalSummary.append(evalMetricCard("Cases", String(state.evalCases.length || 0), "Golden plus local cases."));
    els.skillEvalSummary.append(evalMetricCard("Regression", "Pending", "Compare current report with baseline."));
  }
  renderEvalRows(els.skillEvalSkillList, [], "No Skill eval report yet.");
  renderEvalRows(els.skillEvalPackList, [], "No Pack eval report yet.");
}

async function runSkillEval() {
  if (!els.skillEvalRunButton) return;
  els.skillEvalRunButton.disabled = true;
  els.skillEvalRunButton.textContent = "Running...";
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "eval_report", scope: "all" }),
    });
    const data = await parseJsonResponse(response);
    if (!data.ok || !data.report) throw new Error(data.error || "Skill eval failed");
    state.evalReport = data.report;
    renderEvalDashboard(data.report);
    showToast(`Skill eval ${data.report.status || "completed"}: ${data.report.summary?.caseCount || 0} cases`);
  } catch (error) {
    showToast(`Skill eval failed: ${error.message || error}`);
  } finally {
    els.skillEvalRunButton.disabled = false;
    els.skillEvalRunButton.textContent = "Run Offline Eval";
  }
}

async function loadEvalCases() {
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "list_eval_cases" }),
    });
    const data = await parseJsonResponse(response);
    state.evalCases = Array.isArray(data.cases) ? data.cases : [];
    renderEvalCases();
    if (!state.evalReport) renderEmptyEvalDashboard();
  } catch {
    state.evalCases = [];
    renderEvalCases();
  }
}

async function saveSkillEvalCase(event) {
  event.preventDefault();
  const skillId = els.skillEvalCaseSkillSelect?.value || "";
  const caseId = (els.skillEvalCaseIdInput?.value || "").trim();
  let input = {};
  try {
    input = JSON.parse(els.skillEvalInputTextarea?.value || "{}");
  } catch {
    showToast("Input JSON is invalid.");
    return;
  }
  const payload = {
    action: "create_eval_case",
    case: {
      caseId,
      skillId,
      input,
      expectedKeywords: splitList(els.skillEvalKeywordsInput?.value || ""),
      requiredOutputPaths: splitList(els.skillEvalPathsInput?.value || ""),
      forbidden: splitList(els.skillEvalForbiddenInput?.value || ""),
      expectedArtifactTypes: splitList(els.skillEvalArtifactsInput?.value || ""),
      projectBindingRequired: Boolean(els.skillEvalProjectBindingInput?.checked),
    },
  };
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await parseJsonResponse(response);
    if (!data.ok) throw new Error(data.error || "Save failed");
    els.skillEvalCaseForm?.reset();
    populateEvalSkillSelect();
    await loadEvalCases();
    showToast(`Saved eval case ${data.case?.caseId || caseId}`);
  } catch (error) {
    showToast(`Save eval case failed: ${error.message || error}`);
  }
}

function renderEvalDashboard(report) {
  const summary = report.summary || {};
  if (els.skillEvalSummary) {
    els.skillEvalSummary.replaceChildren();
    els.skillEvalSummary.append(evalMetricCard("Status", report.status || "UNKNOWN", `${summary.failedCases || 0} failed cases`));
    els.skillEvalSummary.append(evalMetricCard("Score", `${summary.overallScore ?? 0}`, `${Math.round((summary.passRate || 0) * 100)}% pass rate`));
    els.skillEvalSummary.append(evalMetricCard("Coverage", `${summary.skillCount || 0} Skills`, `${summary.packCount || 0} Packs / ${summary.caseCount || 0} cases`));
    els.skillEvalSummary.append(evalMetricCard("Regressions", `${summary.regressionCount || 0}`, report.regression?.status || "PASS"));
  }
  renderEvalRows(els.skillEvalSkillList, report.skillResults || [], "No Skill results.");
  renderEvalRows(els.skillEvalPackList, report.packResults || [], "No Pack results.");
}

function evalMetricCard(label, value, hint) {
  const card = document.createElement("article");
  card.className = "skill-eval-metric";
  const strong = document.createElement("strong");
  strong.textContent = value;
  const span = document.createElement("span");
  span.textContent = label;
  const small = document.createElement("small");
  small.textContent = hint || "";
  card.append(span, strong, small);
  return card;
}

function renderEvalRows(host, rows, emptyText) {
  if (!host) return;
  host.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = emptyText;
    host.append(empty);
    return;
  }
  for (const row of rows) {
    const item = document.createElement("article");
    item.className = "skill-eval-row";
    item.dataset.status = row.status || "UNKNOWN";
    const title = document.createElement("div");
    title.className = "skill-eval-row-title";
    const strong = document.createElement("strong");
    strong.textContent = row.name || row.skillId || row.packId || "Eval target";
    const span = document.createElement("span");
    span.textContent = row.skillId || row.packId || "";
    title.append(strong, span);
    const status = document.createElement("span");
    status.className = "skill-eval-status";
    status.textContent = row.status || "UNKNOWN";
    const score = document.createElement("span");
    score.textContent = `${row.overallScore ?? 0}`;
    const cases = document.createElement("span");
    cases.textContent = `${row.caseCount || 0} cases`;
    const failed = document.createElement("span");
    failed.textContent = `${(row.failedCases || []).length} failed`;
    item.append(title, status, score, cases, failed);
    host.append(item);
  }
}

function renderEvalCases() {
  if (!els.skillEvalCaseList) return;
  els.skillEvalCaseList.replaceChildren();
  if (!state.evalCases.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = "No eval cases loaded.";
    els.skillEvalCaseList.append(empty);
    return;
  }
  for (const item of state.evalCases.slice(0, 24)) {
    const row = document.createElement("article");
    row.className = "skill-eval-case-row";
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.name || item.caseId;
    const meta = document.createElement("span");
    meta.textContent = `${item.skillId || ""} · ${(item.expectedKeywords || []).length} keywords · ${(item.expectedArtifactTypes || []).join(",") || "no artifacts"}`;
    body.append(title, meta);
    row.append(body);
    if (item.source === "user") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "danger-button";
      button.textContent = "Delete";
      button.addEventListener("click", () => deleteSkillEvalCase(item.caseId));
      row.append(button);
    }
    els.skillEvalCaseList.append(row);
  }
}

async function deleteSkillEvalCase(caseId) {
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "delete_eval_case", caseId }),
    });
    const data = await parseJsonResponse(response);
    if (!data.ok) throw new Error(data.error || "Delete failed");
    await loadEvalCases();
  } catch (error) {
    showToast(`Delete eval case failed: ${error.message || error}`);
  }
}

function populateEvalSkillSelect() {
  if (!els.skillEvalCaseSkillSelect) return;
  const current = els.skillEvalCaseSkillSelect.value;
  els.skillEvalCaseSkillSelect.replaceChildren();
  for (const skill of state.skills) {
    const option = document.createElement("option");
    option.value = skill.skillId;
    option.textContent = `${skill.name || skill.skillId} (${skill.skillId})`;
    if (skill.skillId === current) option.selected = true;
    els.skillEvalCaseSkillSelect.append(option);
  }
}

function exportSkillEvalJson() {
  if (!state.evalReport) {
    showToast("Run Skill eval before exporting.");
    return;
  }
  downloadTextFile(`${JSON.stringify(state.evalReport, null, 2)}\n`, `skill-eval-${new Date().toISOString().slice(0, 10)}.json`, "application/json");
}

function exportSkillEvalMarkdown() {
  if (!state.evalReport) {
    showToast("Run Skill eval before exporting.");
    return;
  }
  downloadTextFile(skillEvalMarkdown(state.evalReport), `skill-eval-${new Date().toISOString().slice(0, 10)}.md`, "text/markdown;charset=utf-8");
}

async function copySkillEvalSummary() {
  if (!state.evalReport) {
    showToast("Run Skill eval before copying a summary.");
    return;
  }
  const text = skillEvalSummaryText(state.evalReport);
  try {
    await navigator.clipboard?.writeText(text);
    showToast("Skill eval summary copied.");
  } catch {
    showToast(text);
  }
}

function skillEvalSummaryText(report) {
  const summary = report.summary || {};
  return `Skill Eval ${report.status || "UNKNOWN"} · score ${summary.overallScore ?? 0} · pass ${Math.round((summary.passRate || 0) * 100)}% · ${summary.caseCount || 0} cases · ${summary.regressionCount || 0} regressions`;
}

function skillEvalMarkdown(report) {
  const lines = [
    "# Skill Eval Report",
    "",
    `- Status: ${report.status || "UNKNOWN"}`,
    `- Summary: ${skillEvalSummaryText(report)}`,
    "",
    "| Skill | Score | Pass Rate | Cases | Failed |",
    "| --- | ---: | ---: | ---: | ---: |",
  ];
  for (const row of report.skillResults || []) {
    lines.push(`| ${row.skillId || row.name || ""} | ${row.overallScore ?? 0} | ${Math.round((row.passRate || 0) * 100)}% | ${row.caseCount || 0} | ${(row.failedCases || []).length} |`);
  }
  lines.push("", "| Pack | Score | Pass Rate | Cases | Failed |", "| --- | ---: | ---: | ---: | ---: |");
  for (const row of report.packResults || []) {
    lines.push(`| ${row.packId || row.name || ""} | ${row.overallScore ?? 0} | ${Math.round((row.passRate || 0) * 100)}% | ${row.caseCount || 0} | ${(row.failedCases || []).length} |`);
  }
  return `${lines.join("\n")}\n`;
}

function splitList(text) {
  return String(text || "")
    .split(/[,;\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

// --- Skill Packs --------------------------------------------------------------

async function loadPacks() {
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "list_packs" }),
    });
    const data = await parseJsonResponse(response);
    state.packs = Array.isArray(data.packs) ? data.packs : [];
    renderPacks();
  } catch {
    state.packs = [];
  }
}

function renderPacks() {
  if (!els.skillBuiltinPackList || !els.skillCustomPackList) return;
  renderPackCards(els.skillBuiltinPackList, state.packs.filter((pack) => pack.builtin), "没有内置 Pack。");
  renderPackCards(els.skillCustomPackList, state.packs.filter((pack) => !pack.builtin), "还没有自定义 Pack，可导入 .skillpack.json。");
}

function renderPackCards(host, packs, emptyText) {
  host.replaceChildren();
  if (!packs.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = emptyText;
    host.append(empty);
    return;
  }
  for (const pack of packs) {
    host.append(renderPackCard(pack));
  }
}

function renderPackCard(pack) {
  const card = document.createElement("article");
  card.className = "skill-pack-card";
  card.dataset.packId = pack.packId;

  const body = document.createElement("div");
  body.className = "skill-pack-card-body";
  const title = document.createElement("h4");
  title.textContent = pack.name || pack.packId;
  const description = document.createElement("p");
  description.textContent = pack.description || "";
  body.append(title, description);

  const skills = Array.isArray(pack.skills) ? pack.skills : [];
  const skillList = document.createElement("ul");
  skillList.className = "skill-pack-skills";
  for (const entry of skills) {
    const li = document.createElement("li");
    li.textContent = entry.name || entry.skillId || "";
    skillList.append(li);
  }
  body.append(skillList);

  const tags = document.createElement("div");
  tags.className = "skill-card-tags";
  const typeTag = document.createElement("span");
  typeTag.className = "skill-tag";
  typeTag.textContent = pack.builtin ? "内置 Pack" : "自定义 Pack";
  const countTag = document.createElement("span");
  countTag.className = "skill-tag";
  countTag.textContent = `${skills.length} 个 Skill`;
  tags.append(typeTag, countTag);
  body.append(tags);

  const actions = document.createElement("div");
  actions.className = "skill-card-actions";
  const install = document.createElement("button");
  install.type = "button";
  install.className = "seek-primary-button";
  install.dataset.packInstall = pack.packId;
  install.textContent = "安装到项目";
  actions.append(install);
  const exportButton = document.createElement("button");
  exportButton.type = "button";
  exportButton.className = "secondary-button";
  exportButton.dataset.packExport = pack.packId;
  exportButton.textContent = "导出";
  actions.append(exportButton);
  if (!pack.builtin) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger-button";
    remove.dataset.packDelete = pack.packId;
    remove.textContent = "删除";
    actions.append(remove);
  }

  card.append(body, actions);
  return card;
}

function openPacksHost() {
  if (!els.skillPacksHost) return;
  closeSkillBuilder();
  closeSkillRunHost();
  closeEvalHost();
  els.skillPacksHost.hidden = false;
  loadPacks();
}

function closePacksHost() {
  if (els.skillPacksHost) els.skillPacksHost.hidden = true;
}

function onPackListClick(event) {
  const target = event.target instanceof Element ? event.target : event.target?.parentElement;
  const installButton = target?.closest("button[data-pack-install]");
  if (installButton) {
    installPack(installButton.dataset.packInstall);
    return;
  }
  const exportButton = target?.closest("button[data-pack-export]");
  if (exportButton) {
    exportPack(exportButton.dataset.packExport);
    return;
  }
  const deleteButton = target?.closest("button[data-pack-delete]");
  if (deleteButton) {
    deletePack(deleteButton.dataset.packDelete);
  }
}

async function installPack(packId) {
  const pack = state.packs.find((item) => item.packId === packId);
  const projectId = getActiveProjectId();
  if (!projectId) {
    showToast("请先打开一个项目再安装 Pack。");
    return;
  }
  try {
    const response = await apiFetch(`${PROJECT_API}/${projectId}/skill-packs/${encodeURIComponent(packId)}/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await parseJsonResponse(response);
    showToast(`已安装 Pack「${pack?.name || packId}」，启用 ${data.skills?.enabledSkills?.length || 0} 个 Skill。`);
  } catch (error) {
    showToast(`安装失败：${error.message || error}`);
  }
}

async function exportPack(packId) {
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "export_pack", packId }),
    });
    const data = await parseJsonResponse(response);
    downloadTextFile(JSON.stringify(data.pack, null, 2), `${packId}.skillpack.json`, "application/json");
  } catch (error) {
    showToast(`导出失败：${error.message || error}`);
  }
}

async function importPackFromFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const text = await file.text();
    const config = JSON.parse(text);
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "import_pack", pack: config, onConflict: "error" }),
    });
    const data = await parseJsonResponse(response);
    showPackImportSummary(data);
    await loadPacks();
    await loadSkills();
  } catch (error) {
    showPackImportSummary({ ok: false, error: error.message || String(error) });
  }
}

function showPackImportSummary(summary) {
  if (!els.skillPackImportSummary) return;
  els.skillPackImportSummary.replaceChildren();
  els.skillPackImportSummary.hidden = false;
  const box = document.createElement("div");
  box.className = "skill-pack-summary";
  if (summary.ok) {
    const title = document.createElement("strong");
    title.textContent = `导入 Pack：${summary.name || summary.packId || ""}`;
    box.append(title);
    const installed = document.createElement("p");
    installed.textContent = `已安装 Skills：${(summary.installedSkills || []).join("、") || "无"}`;
    box.append(installed);
    if (summary.skippedSkills?.length) {
      const skipped = document.createElement("p");
      skipped.textContent = `跳过（已存在）：${summary.skippedSkills.join("、")}`;
      box.append(skipped);
    }
    if (summary.unresolvedReferences?.length) {
      const unresolved = document.createElement("p");
      unresolved.className = "skill-pack-warning";
      unresolved.textContent = `未解析引用：${summary.unresolvedReferences.join("、")}`;
      box.append(unresolved);
    }
    const tools = (summary.toolPermissions || []).flatMap((item) => item.allowedTools || []);
    const approvalTools = tools.filter((tool) => tool.requiresApproval).map((tool) => tool.tool);
    if (approvalTools.length) {
      const warn = document.createElement("p");
      warn.className = "skill-pack-warning";
      warn.textContent = `需要人工确认的工具：${approvalTools.join("、")}`;
      box.append(warn);
    }
  } else {
    const error = document.createElement("p");
    error.className = "skill-pack-warning";
    error.textContent = `导入失败：${summary.error || "未知错误"}`;
    box.append(error);
  }
  els.skillPackImportSummary.append(box);
}

async function deletePack(packId) {
  const pack = state.packs.find((item) => item.packId === packId);
  if (!pack) return;
  if (!window.confirm(`删除自定义 Pack「${pack.name || packId}」？（不会删除 Pack 内的 Skill。）`)) return;
  try {
    const response = await apiFetch(SKILL_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "delete_pack", packId }),
    });
    await parseJsonResponse(response);
    await loadPacks();
  } catch (error) {
    showToast(`删除失败：${error.message || error}`);
  }
}

function renderRecentRuns() {
  if (!els.skillRecentRunList) return;
  els.skillRecentRunList.replaceChildren();
  if (!state.recentRuns.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = "暂无最近运行。";
    els.skillRecentRunList.append(empty);
    return;
  }
  for (const run of state.recentRuns) {
    const row = document.createElement("article");
    row.className = "skill-run-row";
    row.innerHTML = `<div><strong></strong><span></span></div><time></time>`;
    row.querySelector("strong").textContent = run.skillId || "skill";
    row.querySelector("span").textContent = run.status || "completed";
    row.querySelector("time").textContent = run.completedAt ? new Date(run.completedAt).toLocaleString() : "";
    els.skillRecentRunList.append(row);
  }
}

async function fetchProjectSkills(projectId) {
  const response = await apiFetch(`${PROJECT_API}/${projectId}/skills`, { method: "GET" });
  const data = await parseJsonResponse(response);
  return data.skills || { enabledSkills: [], defaultSkill: "", recentSkills: [] };
}

function renderProjectSkillBindingForm(projectId, binding, container) {
  container.replaceChildren();
  const allSkills = state.skills.length ? state.skills : [];
  const enabled = new Set(binding.enabledSkills || []);

  const enabledLabel = document.createElement("label");
  enabledLabel.className = "project-skill-field";
  enabledLabel.innerHTML = "<span>启用 Skills</span>";
  const enabledSelect = document.createElement("select");
  enabledSelect.multiple = true;
  enabledSelect.dataset.projectSkillBinding = "enabled";
  for (const skill of allSkills) {
    const option = document.createElement("option");
    option.value = skill.skillId;
    option.textContent = skill.name || skill.skillId;
    option.selected = enabled.has(skill.skillId);
    enabledSelect.append(option);
  }
  enabledLabel.append(enabledSelect);
  container.append(enabledLabel);

  const defaultLabel = document.createElement("label");
  defaultLabel.className = "project-skill-field";
  defaultLabel.innerHTML = "<span>默认 Skill</span>";
  const defaultSelect = document.createElement("select");
  defaultSelect.dataset.projectSkillBinding = "default";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "无";
  defaultSelect.append(none);
  for (const skillId of enabled) {
    const skill = allSkills.find((item) => item.skillId === skillId);
    const option = document.createElement("option");
    option.value = skillId;
    option.textContent = skill ? skill.name || skillId : skillId;
    option.selected = skillId === binding.defaultSkill;
    defaultSelect.append(option);
  }
  defaultLabel.append(defaultSelect);
  container.append(defaultLabel);

  enabledSelect.addEventListener("change", () => {
    const selected = Array.from(enabledSelect.selectedOptions).map((option) => option.value);
    defaultSelect.replaceChildren();
    const none2 = document.createElement("option");
    none2.value = "";
    none2.textContent = "无";
    defaultSelect.append(none2);
    for (const skillId of selected) {
      const skill = allSkills.find((item) => item.skillId === skillId);
      const option = document.createElement("option");
      option.value = skillId;
      option.textContent = skill ? skill.name || skillId : skillId;
      defaultSelect.append(option);
    }
    if (!selected.includes(defaultSelect.value)) defaultSelect.value = "";
    saveProjectSkillBinding(projectId);
  });

  defaultSelect.addEventListener("change", () => saveProjectSkillBinding(projectId));

  const enabledPacks = Array.isArray(binding.enabledPacks) ? binding.enabledPacks : [];
  if (enabledPacks.length) {
    const packs = document.createElement("p");
    packs.className = "project-skill-recent";
    packs.dataset.projectSkillBinding = "packs";
    packs.textContent = `已安装 Packs：${enabledPacks.join("、")}`;
    container.append(packs);
  }

  if (binding.recentSkills?.length) {
    const recent = document.createElement("p");
    recent.className = "project-skill-recent";
    recent.textContent = `最近使用：${binding.recentSkills.join("、")}`;
    container.append(recent);
  }
}

async function saveProjectSkillBinding(projectId) {
  const container = els.projectSkillsBody;
  if (!container) return;
  const enabledSelect = container.querySelector('[data-project-skill-binding="enabled"]');
  const defaultSelect = container.querySelector('[data-project-skill-binding="default"]');
  const enabled = Array.from(enabledSelect?.selectedOptions || []).map((option) => option.value);
  const defaultSkill = defaultSelect?.value || "";
  try {
    await apiFetch(`${PROJECT_API}/${projectId}/skills`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabledSkills: enabled, defaultSkill }),
    });
  } catch (error) {
    showToast(`保存 Skill 绑定失败：${error.message || error}`);
  }
}

async function parseJsonResponse(response) {
  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text || "{}");
  } catch {
    data = { ok: false, error: text || "Invalid JSON" };
  }
  if (!response.ok) {
    throw new Error(data.error || data.detail || text || `HTTP ${response.status}`);
  }
  return data;
}

function downloadTextFile(text, filename, mimeType) {
  const blob = new Blob([text], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function openProjectPath(tab, projectId) {
  if (!projectId) return;
  onProjectOpen({ tab, projectId });
}

export function isSkillPanelOpen() {
  return Boolean(els.skillPanel?.classList.contains("open"));
}

export function closeSkillWorkbench() {
  closeSkillPanel();
}
