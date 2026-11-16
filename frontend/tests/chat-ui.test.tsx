// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PersonalChat } from "../components/chat";
import type { ChatEvent } from "../lib/chat-api";

const mocks = vi.hoisted(() => ({ request: vi.fn(), stream: vi.fn(), router: { replace: vi.fn() } }));
vi.mock("next/navigation", () => ({ useRouter: () => mocks.router }));
vi.mock("../lib/chat-api", () => ({ chatRequest: mocks.request, readChatStream: mocks.stream }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
it("opens saved history and keeps a failed message for retry", async () => {
  mocks.request.mockImplementation(async (path: string, method = "GET") => {
    if (method === "POST") throw new Error("HTTP 503");
    if (!path) return Response.json([{ id: "one", title: "Saved" }]);
    return Response.json({ messages: [{ id: "m", role: "assistant", content: "<script>text</script>",
      status: "complete", sources: [{ document_id: "doc", filename: "synthetic.pdf",
        source_url: "/documents/doc#source-page-2", sources: [{ kind: "page", index: 2 }] }] }] });
  });
  render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  expect(await screen.findByText("<script>text</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByRole("link", { name: "synthetic.pdf" })).toHaveAttribute("href", "/documents/doc#source-page-2");
  await user.type(screen.getByLabelText("Сообщение"), "synthetic question");
  await user.click(screen.getByRole("button", { name: "Отправить" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("недоступен");
  expect(screen.getByLabelText("Сообщение")).toHaveValue("synthetic question");
});

it("keeps streaming through state updates and aborts only when Stop is clicked", async () => {
  let signal: AbortSignal | undefined;
  let emit: ((event: ChatEvent) => void) | undefined;
  mocks.request.mockImplementation(async (path: string, method = "GET", _body?: unknown,
    requestSignal?: AbortSignal) => {
    if (method === "POST") {
      signal = requestSignal;
      return new Response("");
    }
    return Response.json(path ? { messages: [] } : [{ id: "one", title: "Saved" }]);
  });
  mocks.stream.mockImplementation((_response: Response, onEvent: (event: ChatEvent) => void) => {
    emit = onEvent;
    return new Promise<void>((_resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("Stopped", "AbortError")));
    });
  });
  render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  await user.type(await screen.findByLabelText("Сообщение"), "Synthetic");
  await user.click(screen.getByRole("button", { name: "Отправить" }));
  await waitFor(() => expect(emit).toBeDefined());
  act(() => emit?.({ type: "delta", text: "Synthetic streamed text" }));
  expect(await screen.findByText("Synthetic streamed text")).toBeInTheDocument();
  expect(signal?.aborted).toBe(false);
  await user.click(screen.getByRole("button", { name: "Остановить" }));
  expect(signal?.aborted).toBe(true);
  expect(await screen.findByRole("alert")).toHaveTextContent("Генерация остановлена");
});
