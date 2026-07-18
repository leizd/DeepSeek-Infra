import { describe, expect, it } from "vitest";

import { createImagePreviews, scaledDimensions } from "./imagePreview";

describe("scaledDimensions", () => {
  it("scales the long side down to maxSide keeping ratio", () => {
    expect(scaledDimensions(3200, 1600, 1600)).toEqual({ width: 1600, height: 800 });
    expect(scaledDimensions(1600, 3200, 1600)).toEqual({ width: 800, height: 1600 });
  });

  it("keeps small images unchanged", () => {
    expect(scaledDimensions(80, 40, 96)).toEqual({ width: 80, height: 40 });
  });

  it("guards invalid input", () => {
    expect(scaledDimensions(0, 100, 96)).toEqual({ width: 0, height: 0 });
    expect(scaledDimensions(100, 100, 0)).toEqual({ width: 0, height: 0 });
  });

  it("never rounds below one pixel", () => {
    expect(scaledDimensions(10000, 1, 96).height).toBe(1);
  });
});

describe("createImagePreviews", () => {
  it("returns null outside a browser environment", async () => {
    const file = new File([new Uint8Array(4)], "a.png", { type: "image/png" });
    await expect(createImagePreviews(file)).resolves.toBeNull();
  });
});
