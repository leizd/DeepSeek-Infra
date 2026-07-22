import { useState } from "react";

import { useOverlay } from "../../contexts/OverlayContext";
import { useFilePreview } from "../../contexts/FilePreviewContext";
import { useProjects } from "../../contexts/ProjectsContext";
import { useSkills } from "../../contexts/SkillsContext";
import { Icon } from "../../shared/ui/Icon";
import { runUiAction } from "../../shared/runUiAction";
import { formatBytes } from "../attachments/attachmentMapper";
import { useProjectSkillBinding } from "./useProjectSkillBinding";
import type { Project } from "../../api/projectsApi";

function ProjectSkillBinding({ project }: { project: Project }) {
  const skills = useSkills();
  const binding = useProjectSkillBinding(project.id);
  const enabled = binding.binding?.enabledSkills ?? [];
  const defaultSkill = binding.binding?.defaultSkill ?? "";

  function save(nextEnabled: string[], nextDefault: string) {
    runUiAction(
      binding.save({
        enabledSkills: nextEnabled,
        defaultSkill: nextDefault,
        recentSkills: binding.binding?.recentSkills ?? [],
        enabledPacks: binding.binding?.enabledPacks ?? [],
      }),
    );
  }

  if (!skills.skills.length) return null;
  return (
    <section className="project-skill-binding" aria-label="项目技能绑定">
      <h3>项目技能{binding.saving ? "（保存中…）" : binding.refreshing ? "（同步中…）" : ""}</h3>
      {binding.error ? (
        <div className="workspace-error" role="alert">
          <span>{binding.error instanceof Error && binding.error.message ? binding.error.message : "绑定加载失败"}</span>
          <button type="button" onClick={() => runUiAction(binding.retry())}>重试</button>
        </div>
      ) : binding.loading ? (
        <p className="history-empty">加载绑定中…</p>
      ) : (
        <>
          <div className="project-skill-options">
            {skills.skills.map((skill) => (
              <label key={skill.skillId} className={skill.disabled ? "skill-option disabled" : "skill-option"}>
                <input
                  type="checkbox"
                  checked={enabled.includes(skill.skillId)}
                  disabled={skill.disabled || binding.saving}
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
            <select value={defaultSkill} disabled={binding.saving} onChange={(event) => save(enabled, event.target.value)}>
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
  const activeUploading = active ? projects.isUploadingProject(active.id) : false;

  return (
    <section className="settings-drawer workspace-drawer" role="dialog" aria-modal="true" aria-label="项目">
      <div className="drawer-heading">
        <div>
          <p className="eyebrow">PROJECTS</p>
          <h2>项目</h2>
        </div>
        <button type="button" aria-label="关闭项目面板" onClick={overlay.closeOverlay}><Icon name="close" /></button>
      </div>
      <form
        className="project-create-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!name.trim()) return;
          runUiAction(projects.create(name), { onSuccess: () => setName("") });
        }}
      >
        <input value={name} maxLength={60} placeholder="新项目名称" onChange={(event) => setName(event.target.value)} />
        <button type="submit" disabled={!name.trim() || projects.creating}>{projects.creating ? "创建中…" : "创建"}</button>
      </form>
      {projects.refreshing && <span className="workspace-sync-status" role="status" aria-live="polite">同步中…</span>}
      {projects.error && (
        <div className="workspace-error" role="alert">
          <span>{projects.error}</span>
          <button type="button" onClick={() => runUiAction(projects.recover())}>重新同步</button>
        </div>
      )}
      <div className="workspace-list">
        {!projects.projects.length && <p className="history-empty">{projects.loading ? "加载中…" : "还没有项目"}</p>}
        {projects.projects.map((project) => {
          const renaming = projects.isRenamingProject(project.id);
          const removing = projects.isRemovingProject(project.id);
          const uploading = projects.isUploadingProject(project.id);
          return (
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
                      event.preventDefault();
                      runUiAction(projects.rename(project.id, renameDraft), {
                        onSuccess: () => setRenamingId(null),
                      });
                    }
                    if (event.key === "Escape") setRenamingId(null);
                  }}
                  onBlur={() => {
                    if (!renaming) setRenamingId(null);
                  }}
                />
              ) : (
                <button className="workspace-open" type="button" onClick={() => projects.setActive(project.id === projects.activeProjectId ? "" : project.id)}>
                  <span>{project.name}</span>
                  <small>{project.documents.length} 个文档{project.id === projects.activeProjectId ? " · 已启用" : ""}</small>
                </button>
              )}
              <div className="conversation-item-actions">
                <button
                  className="conversation-tool"
                  type="button"
                  title="重命名"
                  aria-label={`重命名项目 ${project.name}`}
                  disabled={renaming || removing || uploading}
                  onClick={() => { setRenamingId(project.id); setRenameDraft(project.name); }}
                >
                  ✎
                </button>
                <button
                  className="conversation-tool danger"
                  type="button"
                  title="删除"
                  aria-label={`删除项目 ${project.name}`}
                  disabled={removing || renaming || uploading}
                  onClick={() => {
                    if (!window.confirm(`确定删除项目“${project.name}”？`)) return;
                    runUiAction(projects.remove(project.id));
                  }}
                >
                  <Icon name="close" />
                </button>
              </div>
            </div>
          );
        })}
      </div>
      {active && (
        <section className="project-documents" aria-label="项目文档">
          <div className="project-documents-header">
            <h3>《{active.name}》的文档</h3>
            <label className="message-action project-upload-button">
              {activeUploading ? "上传中…" : "上传文档"}
              <input
                className="sr-only"
                type="file"
                multiple
                disabled={activeUploading}
                onChange={(event) => {
                  if (event.target.files?.length) runUiAction(projects.uploadDocuments(event.target.files));
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
      {active && <ProjectSkillBinding key={active.id} project={active} />}
    </section>
  );
}
