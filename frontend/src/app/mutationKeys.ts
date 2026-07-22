export const mutationKeys = {
  projectList: {
    create: ["project-list", "create"] as const,
    rename: ["project-list", "rename"] as const,
    remove: ["project-list", "remove"] as const,
    upload: ["project-list", "upload"] as const,
  },
  projectBinding: {
    save: (projectId: string) => ["project-binding", projectId, "save"] as const,
  },
  skillList: {
    create: ["skill-list", "create"] as const,
    update: ["skill-list", "update"] as const,
    toggle: ["skill-list", "toggle"] as const,
    remove: ["skill-list", "remove"] as const,
  },
  memoryList: {
    save: ["memory-list", "save"] as const,
    remove: ["memory-list", "remove"] as const,
    clear: ["memory-list", "clear"] as const,
  },
};

export const PROJECT_LIST_MUTATION_KEYS = [
  mutationKeys.projectList.create,
  mutationKeys.projectList.rename,
  mutationKeys.projectList.remove,
  mutationKeys.projectList.upload,
] as const;

export const SKILL_LIST_MUTATION_KEYS = [
  mutationKeys.skillList.create,
  mutationKeys.skillList.update,
  mutationKeys.skillList.toggle,
  mutationKeys.skillList.remove,
] as const;

export const MEMORY_LIST_MUTATION_KEYS = [
  mutationKeys.memoryList.save,
  mutationKeys.memoryList.remove,
  mutationKeys.memoryList.clear,
] as const;

export function mutationKeyEquals(
  actual: readonly unknown[] | undefined,
  expected: readonly unknown[],
): boolean {
  return Boolean(
    Array.isArray(actual)
      && actual.length === expected.length
      && actual.every((value, index) => value === expected[index]),
  );
}

export function ownsMutationKey(
  actual: readonly unknown[] | undefined,
  ownedKeys: readonly (readonly unknown[])[],
): boolean {
  return ownedKeys.some((key) => mutationKeyEquals(actual, key));
}
