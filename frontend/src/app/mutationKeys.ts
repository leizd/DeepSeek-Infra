export const mutationKeys = {
  projects: {
    create: ["projects", "create"] as const,
    rename: ["projects", "rename"] as const,
    remove: ["projects", "remove"] as const,
    upload: ["projects", "upload"] as const,
  },
  skills: {
    create: ["skills", "create"] as const,
    update: ["skills", "update"] as const,
    toggle: ["skills", "toggle"] as const,
    remove: ["skills", "remove"] as const,
  },
  memories: {
    remove: ["memories", "remove"] as const,
    clear: ["memories", "clear"] as const,
  },
};
