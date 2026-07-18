import { describe, expect, it } from "vitest";

import { projectDocumentsToAttachments } from "./useProjectController";
import type { Project } from "../../api/projectsApi";

const project: Project = {
  id: "proj-1",
  name: "调研",
  documents: [
    {
      id: "d1",
      name: "report.pdf",
      type: "application/pdf",
      size: 100,
      kind: "pdf",
      fileId: "f1",
      projectId: "proj-1",
      sourceAvailable: true,
      preview: "预览",
      pageCount: 3,
      charCount: 900,
      chunkCount: 2,
      chunked: true,
      createdAt: 1,
    },
  ],
  createdAt: 1,
  updatedAt: 2,
};

describe("projectDocumentsToAttachments", () => {
  it("maps documents to chat attachments preserving file references", () => {
    const attachments = projectDocumentsToAttachments(project);
    expect(attachments).toHaveLength(1);
    expect(attachments[0]).toMatchObject({
      name: "report.pdf",
      kind: "pdf",
      fileId: "f1",
      projectId: "proj-1",
      chunkCount: 2,
    });
  });

  it("falls back to the project id for the attachment projectId", () => {
    const attachments = projectDocumentsToAttachments({
      ...project,
      documents: [{ ...project.documents[0], projectId: "" }],
    });
    expect(attachments[0].projectId).toBe("proj-1");
  });
});
