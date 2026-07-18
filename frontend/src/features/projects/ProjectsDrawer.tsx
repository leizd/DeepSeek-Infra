import { useEffect, useState } from "react";

import { useOverlay } from "../../contexts/OverlayContext";
import { useFilePreview } from "../../contexts/FilePreviewContext";
import { useProjects } from "../../contexts/ProjectsContext";
import { useSkills } from "../../contexts/SkillsContext";
import { formatBytes } from "../attachments/attachmentMapper";
import type { Project } from "../../api/projectsApi";

function ProjectSkillBinding({ project }: { project: Project }) {
  const skills = useSkills();
  const [enabled, setEnabled] = useState<string[]>([]);
  const [defaultSkill, setDefaultSkill] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setLoaded(false);
    void skills.loadBinding(project.id).then((binding) => {
      setEnabled(binding.enabledSkills);
      setDefaultSkill(binding.defaultSkill);
      setLoaded(true);
    });
  }, [project.id, skills]);

  function save(nextEnabled: string[], nextDefault: string) {
    setEnabled(nextEnabled);
    setDefaultSkill(nextDefault);
    setSaving(true);
    void skills.saveBinding(project.id, nextEnabled, nextDefault).finally(() => setSaving(false));
  }

  if (!skills.skills.length) return null;
  return (
    <section className="project-skill-binding" aria-label="项目技能绑定">
      <h3>项目技能{saving ? "（保存中…）" : ""}</h3>
      {!loaded && <p className="history-empty">加载绑定中…</p>}
      {loaded && (
        <>
          <div className="project-skill-options">
            {skills.skills.map((skill) => (
              <label key={skill.skillId} className={skill.disabled ? "skill-option disabled" : "skill-option"}>
                <input
                  type="checkbox"
                  checked={enabled.includes(skill.skillId)}
                  disabled={skill.disabled}
                  onChange={(event) => {
                    const next = event.target.checked
                      ? [...enabled, skill.skillId]
                      : enabled.filter((id) => id !== skill.skillId);
                    save(next, next.includes(skill.skillId) ? defaultSkill : defaultSkill === skill.skillId ? "" : defaultSkill);
                  }}
                />
                <span>{skill.name}</span>
              </label>
            ))}
          </div>
          <label className="project-default-skill">
            <span>默认技能</span>
            <select value={defaultSkill} onChange={(event) => save(enabled, event.target.value)}>
              <option value="">无</option>
              {enabled.map((skillId) => (
                <option key={skillId} value={skillId}>
                  {skills.skills.find((skill) => skill.skillId === skillId)?.name ?? skillId}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
    </section>
  );
}

export function ProjectsDrawer() {
  const overlay = useOverlay();
  const projects = useProjects();
  const preview = useFilePreview();
  const [name, setName] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  if (overlay.activeOverlay !== "projects") return null;
  const active = projects.activeProject;

  return (
    <section className="settings-drawer workspace-drawer" role="dialog" aria-modal="true" aria-label="项目">
      <div className="drawer-heading">
        <div>
          <p className="eyebrow">PROJECTS</p>
          <h2>项目</h2>
        </div>
        <button type="button" aria-label="关闭项目面板" onClick={overlay.closeOverlay}>×</button>
      </div>
      <form
        className="project-create-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!name.trim()) return;
          void projects.create(name).then(() => setName(""));
        }}
      >
        <input value={name} maxLength={60} placeholder="新项目名称" onChange={(event) => setName(event.target.value)} />
        <button type="submit" disabled={!name.trim()}>创建</button>
      </form>
      {projects.error && <p className="message-error" role="alert">{projects.error}</p>}
      <div className="workspace-list">
        {!projects.projects.length && <p className="history-empty">{projects.loading ? "加载中…" : "还没有项目"}</p>}
        {projects.projects.map((project) => (
          <div className={project.id === projects.activeProjectId ? "workspace-item active" : "workspace-item"} key={project.id}>
            {renamingId === project.id ? (
              <input
                className="conversation-rename-input"
                aria-label="重命名项目"
                autoFocus
                value={renameDraft}
                onChange={(event) => setRenameDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    void projects.rename(project.id, renameDraft);
                    setRenamingId(null);
                  }
                  if (event.key === "Escape") setRenamingId(null);
                }}
                onBlur={() => setRenamingId(null)}
              />
            ) : (
              <button className="workspace-open" type="button" onClick={() => projects.setActive(project.id === projects.activeProjectId ? "" : project.id)}>
                <span>{project.name}</span>
                <small>{project.documents.length} 个文档{project.id === projects.activeProjectId ? " · 已启用" : ""}</small>
              </button>
            )}
            <div className="conversation-item-actions">
              <button className="conversation-tool" type="button" title="重命名" aria-label={`重命名项目 ${project.name}`} onClick={() => { setRenamingId(project.id); setRenameDraft(project.name); }}>✎</button>
              <button className="conversation-tool danger" type="button" title="删除" aria-label={`删除项目 ${project.name}`} onClick={() => void projects.remove(project.id)}>×</button>
            </div>
          </div>
        ))}
      </div>
      {active && (
        <section className="project-documents" aria-label="项目文档">
          <div className="project-documents-header">
            <h3>《{active.name}》的文档</h3>
            <label className="message-action project-upload-button">
              {projects.uploading ? "上传中…" : "上传文档"}
              <input
                className="sr-only"
                type="file"
                multiple
                disabled={projects.uploading}
                onChange={(event) => {
                  if (event.target.files?.length) void projects.uploadDocuments(event.target.files);
                  event.target.value = "";
                }}
              />
            </label>
          </div>
          {!active.documents.length && <p className="history-empty">还没有文档</p>}
          <ul className="project-document-list">
            {active.documents.map((document) => (
              <li key={document.id}>
                <button
                  type="button"
                  className="project-document"
                  onClick={() =>
                    preview.open({
                      name: document.name,
                      type: document.type,
                      size: document.size,
                      kind: document.kind,
                      fileId: document.fileId,
                      projectId: document.projectId || active.id,
                      sourceAvailable: document.sourceAvailable,
                      preview: document.preview,
                      pageCount: document.pageCount,
                      charCount: document.charCount,
                      chunkCount: document.chunkCount,
                      chunked: document.chunked,
                    })
                  }
                >
                  <span className="attachment-kind" aria-hidden="true">{document.kind}</span>
                  <span className="attachment-name">{document.name}</span>
                  <span className="message-attachment-meta">{formatBytes(document.size)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      {active && <ProjectSkillBinding project={active} />}
    </section>
  );
}
