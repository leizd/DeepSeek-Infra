import { useState } from "react";

import type { SimpleSkillDraft, Skill } from "../../api/skillsApi";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSkills } from "../../contexts/SkillsContext";
import { Icon } from "../../shared/ui/Icon";
import { runUiAction } from "../../shared/runUiAction";

const emptyDraft: SimpleSkillDraft = { name: "", description: "", systemPrompt: "" };

function SkillForm({
  initial,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initial: SimpleSkillDraft;
  submitLabel: string;
  onSubmit(draft: SimpleSkillDraft): Promise<void>;
  onCancel(): void;
}) {
  const [draft, setDraft] = useState(initial);
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="skill-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!draft.name.trim() || !draft.systemPrompt.trim()) return;
        setBusy(true);
        runUiAction(onSubmit(draft), { onSettled: () => setBusy(false) });
      }}
    >
      <input
        aria-label="技能名称"
        placeholder="技能名称"
        maxLength={120}
        value={draft.name}
        onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))}
      />
      <input
        aria-label="技能描述"
        placeholder="一句话描述（可选）"
        maxLength={600}
        value={draft.description}
        onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))}
      />
      <textarea
        aria-label="技能提示词"
        placeholder="系统提示词：定义这个技能的角色、流程与输出要求"
        rows={5}
        maxLength={20_000}
        value={draft.systemPrompt}
        onChange={(event) => setDraft((current) => ({ ...current, systemPrompt: event.target.value }))}
      />
      <div className="message-edit-actions">
        <button className="message-action primary" type="submit" disabled={busy || !draft.name.trim() || !draft.systemPrompt.trim()}>
          {submitLabel}
        </button>
        <button className="message-action" type="button" onClick={onCancel}>取消</button>
      </div>
    </form>
  );
}

function SkillCard({ skill }: { skill: Skill }) {
  const skills = useSkills();
  const [editing, setEditing] = useState(false);
  const updating = skills.isUpdatingSkill(skill.skillId);
  const toggling = skills.isTogglingSkill(skill.skillId);
  const removing = skills.isRemovingSkill(skill.skillId);
  return (
    <li className={skill.disabled ? "skill-card disabled" : "skill-card"}>
      <div className="skill-card-header">
        <div>
          <strong>{skill.name}</strong>
          {skill.description && <p>{skill.description}</p>}
        </div>
        <div className="conversation-item-actions">
          <button className="message-action" type="button" disabled={toggling || removing || updating} onClick={() => runUiAction(skills.toggle(skill))}>
            {toggling ? "…" : skill.disabled ? "启用" : "禁用"}
          </button>
          {!skill.builtin && (
            <>
              <button className="message-action" type="button" disabled={removing || toggling || updating} onClick={() => setEditing((value) => !value)}>编辑</button>
              <button
                className="message-action"
                type="button"
                disabled={removing || toggling || updating}
                onClick={() => {
                  if (!window.confirm(`确定删除技能“${skill.name}”？`)) return;
                  runUiAction(skills.remove(skill.skillId));
                }}
              >
                {removing ? "…" : "删除"}
              </button>
            </>
          )}
        </div>
      </div>
      {editing && (
        <SkillForm
          initial={{ name: skill.name, description: skill.description, systemPrompt: skill.systemPrompt }}
          submitLabel="保存"
          onSubmit={async (draft) => {
            await skills.update({ ...draft, skillId: skill.skillId });
            setEditing(false);
          }}
          onCancel={() => setEditing(false)}
        />
      )}
    </li>
  );
}

export function SkillsDrawer() {
  const overlay = useOverlay();
  const skills = useSkills();
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  if (overlay.activeOverlay !== "skills") return null;

  const normalized = query.trim().toLowerCase();
  const filtered = normalized
    ? skills.skills.filter((skill) => skill.name.toLowerCase().includes(normalized) || skill.description.toLowerCase().includes(normalized))
    : skills.skills;
  const builtin = filtered.filter((skill) => skill.builtin);
  const custom = filtered.filter((skill) => !skill.builtin);

  return (
    <section className="settings-drawer workspace-drawer" role="dialog" aria-modal="true" aria-label="技能">
      <div className="drawer-heading">
        <div>
          <p className="eyebrow">SKILLS</p>
          <h2>技能</h2>
        </div>
        <button type="button" aria-label="关闭技能面板" onClick={overlay.closeOverlay}><Icon name="close" /></button>
      </div>
      <div className="workspace-toolbar">
        <label className="history-search">
          <span className="sr-only">搜索技能</span>
          <input value={query} placeholder="搜索技能" onChange={(event) => setQuery(event.target.value)} />
        </label>
        {skills.refreshing && <span className="workspace-sync-status" role="status" aria-live="polite">同步中…</span>}
        <button className="message-action" type="button" onClick={() => setCreating((value) => !value)}>
          {creating ? "收起" : "新建技能"}
        </button>
      </div>
      {creating && (
        <SkillForm
          initial={emptyDraft}
          submitLabel="创建技能"
          onSubmit={async (draft) => {
            await skills.create(draft);
            setCreating(false);
          }}
          onCancel={() => setCreating(false)}
        />
      )}
      {skills.error && (
        <div className="workspace-error" role="alert">
          <span>{skills.error}</span>
          <button type="button" onClick={() => runUiAction(skills.recover())}>重新同步</button>
        </div>
      )}
      <h3 className="workspace-section-title">自定义</h3>
      <ul className="skill-list">
        {!custom.length && <p className="history-empty">{skills.loading ? "加载中…" : "还没有自定义技能"}</p>}
        {custom.map((skill) => <SkillCard key={skill.skillId} skill={skill} />)}
      </ul>
      <h3 className="workspace-section-title">内置</h3>
      <ul className="skill-list">
        {builtin.map((skill) => <SkillCard key={skill.skillId} skill={skill} />)}
      </ul>
    </section>
  );
}
