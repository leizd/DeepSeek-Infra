// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LauncherPage } from "./LauncherPage";

const settings = { deepseek_api_key: "sk-owned-ui-fixture", tavily_api_key: "tv-owned-ui-fixture", host: "127.0.0.1", port: 8123, allow_lan: false, ocr_enabled: true, auth_disabled: false };
const stopped = { status: "stopped", computer_url: "", phone_urls: [], error: "", logs: [] };
function mockService(overrides: Record<string, (body: unknown) => Response> = {}) {
  const requests: Array<{ path: string; body: unknown }> = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, options?: RequestInit) => {
    const body = options?.body ? JSON.parse(String(options.body)) : undefined;
    requests.push({ path, body });
    if (overrides[path]) return overrides[path](body);
    return new Response(JSON.stringify(path.endsWith("settings") ? settings : stopped), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return requests;
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("native configuration launcher", () => {
  it("loads all retained options, masks credentials and explicitly reveals them", async () => {
    mockService();
    render(<LauncherPage />);
    const input = await screen.findByLabelText("DeepSeek API Key") as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe(settings.deepseek_api_key));
    expect(input.type).toBe("password");
    expect((screen.getByLabelText("启用 OCR") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("端口") as HTMLInputElement).value).toBe("8123");
    fireEvent.click(screen.getByRole("button", { name: "显示 DeepSeek API Key" }));
    expect(input.type).toBe("text");
    expect(screen.getByRole("button", { name: "启动服务" })).toBeTruthy();
  });

  it("does not start without accepting the existing missing-key confirmation", async () => {
    const requests = mockService();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<LauncherPage />);
    const key = await screen.findByLabelText("DeepSeek API Key");
    await waitFor(() => expect((key as HTMLInputElement).value).toBe(settings.deepseek_api_key));
    fireEvent.change(key, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "启动服务" }));
    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect(requests.some((r) => r.path.endsWith("/start"))).toBe(false);
  });

  it("only clears saved credentials after confirmation and restores the form defaults", async () => {
    const requests = mockService();
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<LauncherPage />);
    const key = await screen.findByLabelText("DeepSeek API Key");
    await waitFor(() => expect((key as HTMLInputElement).value).toBe(settings.deepseek_api_key));
    fireEvent.click(screen.getByRole("button", { name: "清空保存的 Key" }));
    expect(requests.some((r) => r.path.endsWith("/clear"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "清空保存的 Key" }));
    await waitFor(() => expect((key as HTMLInputElement).value).toBe(""));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect((screen.getByLabelText("端口") as HTMLInputElement).value).toBe("8000");
  });

  it("retries an occupied port only after the user accepts the server's confirmation", async () => {
    let attempt = 0;
    const requests = mockService({ "/launcher/v1/start": () => ++attempt === 1
      ? new Response(JSON.stringify({ code: "PORT_IN_USE", error: "端口已被占用" }), { status: 409 })
      : new Response(JSON.stringify({ ...stopped, status: "starting" }), { status: 200 }) });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<LauncherPage />);
    await waitFor(() => expect((screen.getByLabelText("DeepSeek API Key") as HTMLInputElement).value).toBe(settings.deepseek_api_key));
    fireEvent.click(screen.getByRole("button", { name: "启动服务" }));
    await waitFor(() => expect(attempt).toBe(2));
    const retried = requests.filter((r) => r.path.endsWith("/start"))[1].body as { confirm_next_port: boolean; config: typeof settings };
    expect(retried.confirm_next_port).toBe(true);
    expect(retried.config.ocr_enabled).toBe(true);
  });
});
