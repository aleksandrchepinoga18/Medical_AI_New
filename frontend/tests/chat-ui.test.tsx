// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PersonalChat } from "../components/chat";
import { ChatHttpError, type ChatEvent } from "../lib/chat-api";

const mocks = vi.hoisted(() => ({ request: vi.fn(), stream: vi.fn(), router: { replace: vi.fn() } }));
vi.mock("next/navigation", () => ({ useRouter: () => mocks.router }));
vi.mock("../lib/chat-api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../lib/chat-api")>(),
  chatRequest: mocks.request, readChatStream: mocks.stream,
}));
const localModel = { llm_provider: "ollama", llm_model: "qwen2.5:7b-instruct" };
const cloudModel = { llm_provider: "gigachat", llm_model: "GigaChat-3-Ultra" };
afterEach(() => { cleanup(); vi.resetAllMocks(); });
it("opens saved history and keeps a failed message for retry", async () => {
  mocks.request.mockImplementation(async (path: string, method = "GET") => {
    if (method === "POST") throw new ChatHttpError(503);
    if (!path) return Response.json([{ id: "one", title: "Saved" }]);
    return Response.json({ ...localModel, messages: [{
      id: "m", role: "assistant", content: "<script>text</script>",
      status: "complete", sources: [{ document_id: "doc", filename: "synthetic.pdf",
        source_id: "S1", source_url: "/documents/doc#source-page-2",
        sources: [{ kind: "page", index: 2 }] }] }] });
  });
  render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  expect(await screen.findByText("Активная модель — Ollama / qwen2.5:7b-instruct"))
    .toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(await screen.findByText("<script>text</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByRole("link", { name: "[S1] synthetic.pdf" })).toHaveAttribute("href", "/documents/doc#source-page-2");
  await user.type(screen.getByLabelText("Сообщение"), "synthetic question");
  await user.click(screen.getByRole("button", { name: "Отправить" }));
  expect(mocks.request).toHaveBeenCalledWith(
    "/one/messages", "POST", { content: "synthetic question" }, expect.anything(),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("недоступен");
  expect(screen.getByLabelText("Сообщение")).toHaveValue("synthetic question");
});

it("keeps streaming through state and router updates until Stop is clicked", async () => {
  let signal: AbortSignal | undefined;
  let emit: ((event: ChatEvent) => void) | undefined;
  mocks.request.mockImplementation(async (path: string, method = "GET", _body?: unknown,
    requestSignal?: AbortSignal) => {
    if (method === "POST") {
      signal = requestSignal;
      return new Response("");
    }
    return Response.json(path ? { ...localModel, messages: [] } : [{ id: "one", title: "Saved" }]);
  });
  mocks.stream.mockImplementation((_response: Response, onEvent: (event: ChatEvent) => void) => {
    emit = onEvent;
    return new Promise<void>((_resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("Stopped", "AbortError")));
    });
  });
  const view = render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  await user.type(await screen.findByLabelText("Сообщение"), "Synthetic");
  await user.click(screen.getByRole("button", { name: "Отправить" }));
  await waitFor(() => expect(emit).toBeDefined());
  act(() => emit?.({ type: "delta", text: "Synthetic streamed text" }));
  expect(await screen.findByText("Synthetic streamed text")).toBeInTheDocument();
  expect(signal?.aborted).toBe(false);
  mocks.router = { replace: vi.fn() };
  view.rerender(<PersonalChat />);
  expect(signal?.aborted).toBe(false);
  await user.click(screen.getByRole("button", { name: "Остановить" }));
  expect(signal?.aborted).toBe(true);
  expect(await screen.findByRole("alert")).toHaveTextContent("Генерация остановлена");
});

it("requires fresh cloud consent for each message", async () => {
  mocks.request.mockImplementation(async (path: string, method = "GET") => {
    if (method === "POST") return new Response("");
    return Response.json(path ? { ...cloudModel, messages: [] } : [
      { id: "one", title: "Saved" },
    ]);
  });
  mocks.stream.mockResolvedValue(undefined);
  render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  expect(await screen.findByText("Активная модель — GigaChat-3-Ultra")).toBeInTheDocument();
  await user.type(screen.getByLabelText("Сообщение"), "Synthetic");
  const submit = screen.getByRole("button", { name: "Отправить" });
  const consent = screen.getByRole("checkbox", { name: /Разрешаю отправить в облачный GigaChat/ });
  expect(submit).toBeDisabled();
  expect(consent).not.toBeChecked();
  await user.click(consent);
  await user.click(submit);
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith(
    "/one/messages", "POST", { content: "Synthetic", external_consent: "gigachat" }, expect.anything(),
  ));
  await waitFor(() => expect(consent).toBeEnabled());
  expect(consent).not.toBeChecked();
  await user.type(screen.getByLabelText("Сообщение"), "Another");
  expect(submit).toBeDisabled();
});

it("loads the cloud model after a rejected send from a tab opened on Ollama", async () => {
  let switched = false;
  mocks.request.mockImplementation(async (path: string, method = "GET") => {
    if (method === "POST") {
      switched = true;
      throw new ChatHttpError(403, "Подтвердите согласие на облачную обработку в GigaChat.");
    }
    if (!path) return Response.json([{ id: "one", title: "Saved" }]);
    return Response.json({ ...(switched ? cloudModel : localModel), messages: [] });
  });
  render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  await user.type(screen.getByLabelText("Сообщение"), "Synthetic");
  const submit = screen.getByRole("button", { name: "Отправить" });
  await user.click(submit);
  expect(await screen.findByRole("alert")).toHaveTextContent("Подтвердите согласие");
  expect(await screen.findByText("Активная модель — GigaChat-3-Ultra")).toBeInTheDocument();
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(screen.getByLabelText("Сообщение")).toHaveValue("Synthetic");
  expect(submit).toBeDisabled();
});

it("blocks sending until the backend response identifies the model", async () => {
  mocks.request.mockImplementation(async (path: string) => Response.json(
    path ? { messages: [] } : [{ id: "one", title: "Saved" }],
  ));
  render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  await user.type(screen.getByLabelText("Сообщение"), "Synthetic");
  expect(screen.getByRole("status")).toHaveTextContent("Не удалось определить активную модель");
  expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
});

it("shows a saved local fallback while GigaChat is the active model", async () => {
  mocks.request.mockImplementation(async (path: string) => Response.json(path ? {
    ...cloudModel,
    messages: [{
      id: "fallback", role: "assistant", status: "complete", sources: [],
      content: "Резервный ответ · локальная обработка\n\nСинтетический результат",
    }],
  } : [{ id: "one", title: "Saved" }]));
  render(<PersonalChat />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Saved" }));
  expect(await screen.findByText("Активная модель — GigaChat-3-Ultra")).toBeInTheDocument();
  expect(screen.getByText(/Резервный ответ · локальная обработка/))
    .toHaveTextContent("Синтетический результат");
});
