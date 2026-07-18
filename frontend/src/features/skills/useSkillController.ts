import { useCallback, useEffect, useState } from "react";

import {
  createSkill,
  deleteSkill,
  fetchProjectSkillBinding,
  listSkills,
  saveProjectSkillBinding,
  setSkillDisabled,
  updateSkillPrompt,
  type ProjectSkillBinding,
  type SimpleSkillDraft,
  type Skill,
} from "../../api/skillsApi";

export interface SkillController {
  skills: readonly Skill[];
  loading: boolean;
  error: string;
  refresh(): Promise<void>;
  toggle(skill: Skill): Promise<void>;
  remove(skillId: string): Promise<void>;
  create(draft: SimpleSkillDraft): Promise<void>;
  update(draft: SimpleSkillDraft & { skillId: string }): Promise<void>;
  loadBinding(projectId: string): Promise<ProjectSkillBinding>;
  saveBinding(projectId: string, enabledSkills: readonly string[], defaultSkill: string): Promise<void>;
}

export function useSkillController(): SkillController {
  const [skills, setSkills] = useState<readonly Skill[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSkills(await listSkills());
    } catch (reason) {
      setError(reason instanceof Error && reason.message ? reason.message : "技能列表加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggle = useCallback(
    async (skill: Skill) => {
      await setSkillDisabled(skill.skillId, !skill.disabled);
      await refresh();
    },
    [refresh],
  );

  const remove = useCallback(
    async (skillId: string) => {
      await deleteSkill(skillId);
      await refresh();
    },
    [refresh],
  );

  const create = useCallback(
    async (draft: SimpleSkillDraft) => {
      await createSkill(draft);
      await refresh();
    },
    [refresh],
  );

  const update = useCallback(
    async (draft: SimpleSkillDraft & { skillId: string }) => {
      await updateSkillPrompt(draft);
      await refresh();
    },
    [refresh],
  );

  const loadBinding = useCallback((projectId: string) => fetchProjectSkillBinding(projectId), []);

  const saveBinding = useCallback(async (projectId: string, enabledSkills: readonly string[], defaultSkill: string) => {
    await saveProjectSkillBinding(projectId, { enabledSkills, defaultSkill });
  }, []);

  return { skills, loading, error, refresh, toggle, remove, create, update, loadBinding, saveBinding };
}
