export const PROJECTS_QUERY_KEY = ["projects"] as const;

export const SKILLS_QUERY_KEY = ["skills"] as const;

export const MEMORIES_QUERY_KEY = ["memories"] as const;

export function projectSkillBindingQueryKey(projectId: string) {
  return ["projects", projectId, "skillBinding"] as const;
}
