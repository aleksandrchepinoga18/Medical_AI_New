// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PersonalChat } from "../components/chat";
import { ChatAnswer } from "../components/chat-answer";
import { ChatHttpError, type ChatEvent } from "../lib/chat-api";

const mocks = vi.hoisted(() => ({ request: vi.fn(), stream: vi.fn(), router: { replace: vi.fn() } }));
vi.mock("next/navigation", () => ({ useRouter: () => mocks.router }));
vi.mock("../lib/chat-api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../lib/chat-api")>(),
  chatRequest: mocks.request, readChatStream: mocks.stream,
}));
const localModel = { llm_provider: "ollama", llm_model: "qwen2.5:7b-instruct" };
const cloudModel = { llm_provider: "gigachat", llm_model: "GigaChat-3-Ultra" };
const availableProviders = {
  ollama: { model: localModel.llm_model, status: "ready" },
  gigachat: { model: cloudModel.llm_model, status: "ready" },
};
afterEach(() => { cleanup(); vi.resetAllMocks(); });
it("renders an escaped Markdown subset without HTML or unverified links", () => {
  const url = "https://pubmed.ncbi.nlm.nih.gov/19671910/";
  const citation = {
    kind: "pubmed" as const, pmid: "19671910", doi: null, year: 2009, authors: [],
    journal: null, filename: "Synthetic publication", source_url: url, sources: [],
  };
  const text = [
    "\\*\\*Аннотация\\*\\*",
    "- *Сведения* \\[S1\\]",
    `- [Официальная запись](${url})`,
    "- [Внешний адрес](https://example.org/fake)",
    "<img src=x onerror=alert(1)> [Запуск](javascript:alert(1))",
  ].join("\n");
  render(<ChatAnswer text={text} sources={[citation]} />);
  expect(screen.getByText("Аннотация").tagName).toBe("STRONG");
  expect(screen.getAllByRole("listitem")).toHaveLength(3);
  expect(screen.getByText("Сведения").tagName).toBe("EM");
  expect(screen.getByText(/\[S1\]/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Официальная запись" })).toHaveAttribute("href", url);
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(document.querySelector("img")).toBeNull();
});

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

it("identifies public sources alongside personal citations", async () => {
  const pubmedUrl = "https://pubmed.ncbi.nlm.nih.gov/12345678/";
  mocks.request.mockImplementation(async (path: string) => Response.json(path ? {
    ...localModel,
    messages: [{
      id: "mixed", role: "assistant", status: "complete", content: "[S1] [S2] [S3]",
      sources: [
        {
          source_id: "S1", document_id: "synthetic-doc", document_number: 2,
          source_url: "/documents/synthetic-doc", filename: "results.pdf", sources: [],
        },
        {
          source_id: "S2", kind: "medical_knowledge", knowledge_source_id: "ref",
          chunk_id: "fragment", version_id: "version", version: "1",
          start_char: 0, end_char: 12, source_url: "https://example.org/reference/",
          filename: "Synthetic reference", sources: [{ kind: "page", index: 2 }],
        },
        {
          source_id: "S3", kind: "pubmed", pmid: "12345678", doi: null, journal: null,
          year: 2024, authors: [], source_url: pubmedUrl,
          filename: "Synthetic article", sources: [],
        },
      ],
    }],
  } : [{ id: "one", title: "Saved" }]));
  render(<PersonalChat />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Saved" }));
  expect(await screen.findByRole("link", { name: "[S1] № 2 · results.pdf" }))
    .toHaveAttribute("href", "/documents/synthetic-doc");
  expect(screen.getByRole("link", { name: "[S2] Медицинская база · Synthetic reference" }))
    .toHaveAttribute("href", "https://example.org/reference/");
  expect(screen.getByRole("link", { name: "[S3] PubMed · Synthetic article" }))
    .toHaveAttribute("href", pubmedUrl);
  expect(screen.getByText(/PMID 12345678/)).toBeInTheDocument();
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

it("selects either provider per send and clears consent on provider change", async () => {
  mocks.request.mockImplementation(async (path: string, method = "GET") => {
    if (method === "POST") return new Response("");
    return Response.json(path ? {
      ...localModel, llm_providers: availableProviders, messages: [],
    } : [{ id: "one", title: "Saved" }]);
  });
  mocks.stream.mockResolvedValue(undefined);
  render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  const selector = screen.getByRole("combobox", { name: "Провайдер модели" });
  const submit = screen.getByRole("button", { name: "Отправить" });
  const input = screen.getByLabelText("Сообщение");
  await user.type(input, "Synthetic cloud");
  expect(selector).toHaveValue("ollama");
  await user.selectOptions(selector, "gigachat");
  expect(submit).toBeDisabled();
  expect(mocks.request.mock.calls.some(call => call[1] === "POST")).toBe(false);
  await user.click(screen.getByRole("checkbox"));
  await user.selectOptions(selector, "ollama");
  await user.selectOptions(selector, "gigachat");
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(submit).toBeDisabled();
  await user.click(screen.getByRole("checkbox"));
  await user.click(submit);
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith(
    "/one/messages", "POST",
    { content: "Synthetic cloud", llm_provider: "gigachat", external_consent: "gigachat" },
    expect.anything(),
  ));
  await waitFor(() => expect(selector).toBeEnabled());
  expect(selector).toHaveValue("gigachat");
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  await user.selectOptions(selector, "ollama");
  await user.type(input, "Synthetic local");
  await user.click(submit);
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith(
    "/one/messages", "POST", { content: "Synthetic local", llm_provider: "ollama" }, expect.anything(),
  ));
});

it.each([
  ["not_configured", "Не настроен"],
  ["unavailable", "Недоступен"],
])("shows disabled GigaChat with status %s", async (status, label) => {
  mocks.request.mockImplementation(async (path: string) => Response.json(path ? {
    ...cloudModel,
    llm_providers: {
      ...availableProviders, gigachat: { model: cloudModel.llm_model, status },
    },
    messages: [],
  } : [{ id: "one", title: "Saved" }]));
  render(<PersonalChat />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Saved" }));
  await user.type(screen.getByLabelText("Сообщение"), "Synthetic");
  expect(screen.getByRole("option", { name: `GigaChat-3-Ultra — ${label}` })).toBeDisabled();
  expect(screen.getByText(`GigaChat — ${label} на сервере.`)).toBeInTheDocument();
  const selector = screen.getByRole("combobox", { name: "Провайдер модели" });
  expect(selector).toHaveValue("gigachat");
  expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  await user.selectOptions(selector, "ollama");
  expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled();
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
