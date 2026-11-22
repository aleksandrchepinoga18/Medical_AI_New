// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import KnowledgePage from "../app/(private)/knowledge/page";
import { AppShell } from "../components/app-shell";
import { KnowledgeAdmin } from "../components/knowledge/admin";
import { KnowledgeApiError, type Source, type SourceStatus } from "../lib/knowledge-api";

const api = vi.hoisted(() => ({
  access: vi.fn(), listSources: vi.fn(), uploadSource: vi.fn(), editSource: vi.fn(),
  reviewPublish: vi.fn(), setSourceStatus: vi.fn(), searchKnowledge: vi.fn(),
}));
vi.mock("../lib/knowledge-access", () => ({ hasKnowledgeAccess: api.access }));
vi.mock("../lib/knowledge-api", async original => ({
  ...await original<typeof import("../lib/knowledge-api")>(), ...api,
}));
vi.mock("next/headers", () => ({ cookies: async () => ({
  get: () => ({ value: "synthetic-session" }),
}) }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/knowledge",
  redirect: (path: string) => { throw new Error(path); },
}));

function source(status: SourceStatus = "uploaded", chunkCount = 0): Source {
  return {
    id: "synthetic-source", title: "Synthetic guide", material_type: "synthetic",
    author: "Synthetic author", publisher: "Synthetic publisher", url: "https://example.org/guide",
    specialty: "Synthetic", language: "en", jurisdiction: "Synthetic",
    doi: null, pmid: null, publication_date: null, update_date: null,
    current_version: {
      id: "synthetic-version", version: "1", format: "pdf", status, chunk_count: chunkCount,
      sha256: "a".repeat(64), review_revision: "review-revision", review_note: null,
    },
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  api.listSources.mockResolvedValue({ items: [source()], next_after: null });
});
afterEach(cleanup);

it("restricts direct page access and navigation to administrators", async () => {
  api.access.mockResolvedValue(false);
  const denied = render(await KnowledgePage());
  expect(screen.getByRole("alert")).toHaveTextContent("Административный раздел недоступен");
  expect(api.listSources).not.toHaveBeenCalled();
  denied.unmount();
  const shell = render(<AppShell>Content</AppShell>);
  expect(screen.queryByRole("link", { name: "Медицинская база знаний" })).toBeNull();
  shell.rerender(<AppShell knowledgeAdmin>Content</AppShell>);
  expect(screen.getByRole("link", { name: "Медицинская база знаний" })).toHaveAttribute("href", "/knowledge");
});

it("hides operations when the registry rejects access", async () => {
  api.listSources.mockRejectedValue(new KnowledgeApiError(403, "Synthetic denial"));
  render(<KnowledgeAdmin />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Synthetic denial");
  expect(screen.queryByRole("button", { name: "Добавить источник" })).toBeNull();
  expect(api.setSourceStatus).not.toHaveBeenCalled();
});

it("uploads a synthetic PDF with bibliography", async () => {
  api.listSources.mockResolvedValue({ items: [], next_after: null });
  api.uploadSource.mockResolvedValue(source());
  const user = userEvent.setup();
  render(<KnowledgeAdmin />);
  await screen.findByText("Источников пока нет.");
  await user.click(screen.getByRole("button", { name: "Добавить источник" }));
  for (const [label, value] of Object.entries({
    "Название": "Synthetic guide", "Тип материала": "synthetic",
    "Автор или организация": "Synthetic author", "Издатель": "Synthetic publisher",
    "Официальный HTTPS URL": "https://example.org/guide", "Направление": "Synthetic",
    "Код языка": "en", "Страна или юрисдикция": "Synthetic", "Версия": "1",
  })) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  const file = new File(["%PDF-synthetic"], "guide.pdf", { type: "application/pdf" });
  const input = screen.getByLabelText("Файл PDF или DOCX");
  await user.upload(input, file);
  fireEvent.submit(input.closest("form")!);
  expect(await screen.findByText("Загружен")).toBeInTheDocument();
  expect(api.uploadSource).toHaveBeenCalledWith(expect.objectContaining({
    title: "Synthetic guide", version: "1", url: "https://example.org/guide",
  }), file, undefined);
  expect(api.reviewPublish).not.toHaveBeenCalled();
});

it("replaces a source version from the additional menu", async () => {
  const user = userEvent.setup();
  const old = source("verified");
  api.listSources.mockResolvedValue({ items: [old], next_after: null });
  const replacement = source();
  replacement.current_version = { ...replacement.current_version, id: "second", version: "2" };
  api.uploadSource.mockResolvedValue(replacement);
  render(<KnowledgeAdmin />);
  await user.click(await screen.findByText("Дополнительно"));
  await user.click(screen.getByRole("button", { name: "Загрузить новую версию" }));
  expect(screen.getByLabelText("Название")).toHaveValue(old.title);
  fireEvent.change(screen.getByLabelText("Версия"), { target: { value: "2" } });
  const input = screen.getByLabelText("Файл PDF или DOCX");
  const file = new File(["synthetic"], "replacement.docx");
  await user.upload(input, file);
  fireEvent.submit(input.closest("form")!);
  expect(await screen.findByText(/Версия 2/)).toBeInTheDocument();
  expect(api.uploadSource).toHaveBeenCalledWith(expect.objectContaining({ version: "2" }), file, old.id);
  expect(api.reviewPublish).not.toHaveBeenCalled();
});

it("downloads the current version, confirms publication and moves the source to the archive", async () => {
  const user = userEvent.setup();
  api.reviewPublish.mockResolvedValueOnce(source("published", 1));
  api.setSourceStatus.mockResolvedValueOnce(source("outdated", 1));
  api.searchKnowledge.mockResolvedValue({ items: [{
    chunk_id: "synthetic-chunk", score: 0.75, text: "<b>Synthetic fragment</b>",
    locators: [{ kind: "page", index: 1, start: 0, end: 20 }],
    source: { id: "synthetic-source", title: "Synthetic guide", version: "1",
      url: "https://example.org/guide", version_id: "synthetic-version" },
  }] });
  render(<KnowledgeAdmin />);
  expect(await screen.findByRole("link", { name: "Скачать оригинал" })).toHaveAttribute(
    "href", "/api/knowledge/sources/synthetic-source/versions/synthetic-version/original",
  );
  await user.click(screen.getByRole("button", { name: "Подтвердить и опубликовать" }));
  const dialogue = within(await screen.findByRole("dialog"));
  expect(dialogue.queryByRole("textbox")).toBeNull();
  expect(api.reviewPublish).not.toHaveBeenCalled();
  await user.click(dialogue.getByRole("button", { name: "Подтвердить и опубликовать" }));
  expect(await screen.findByText("Доступен для поиска")).toBeInTheDocument();
  expect(api.reviewPublish).toHaveBeenCalledOnce();
  expect(api.reviewPublish).toHaveBeenCalledWith(source());
  fireEvent.change(screen.getByLabelText("Запрос"), { target: { value: "Synthetic query" } });
  await user.click(screen.getByRole("button", { name: "Найти в базе" }));
  expect(await screen.findByText("<b>Synthetic fragment</b>")).toBeInTheDocument();
  expect(screen.getByText("Synthetic guide · Версия 1")).toBeInTheDocument();
  expect(screen.getByText("Страница 1 · RRF 0.7500")).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "https://example.org/guide" })).toHaveLength(2);
  expect(api.searchKnowledge).toHaveBeenCalledWith("Synthetic query", 5);
  await user.click(screen.getByText("Дополнительно"));
  await user.click(screen.getByRole("button", { name: "Снять с публикации" }));
  expect(await screen.findByText("Архивный")).toBeInTheDocument();
  expect(api.setSourceStatus).toHaveBeenCalledWith(source("published", 1), "outdated");
  expect(screen.queryByText("<b>Synthetic fragment</b>")).toBeNull();
});

it("processes a batch in sequence and lets the administrator retry a failed item", async () => {
  const user = userEvent.setup();
  const first = source();
  const second = { ...source(), id: "second-source", title: "Second guide" };
  second.current_version = { ...second.current_version, id: "v2", review_revision: "revision2" };
  const secondPublished = {
    ...second, current_version: { ...second.current_version, status: "published", chunk_count: 1 },
  };
  api.listSources.mockResolvedValue({ items: [first, second], next_after: null });
  let finishFirst: (source: Source) => void = () => {};
  api.reviewPublish.mockImplementationOnce(() => new Promise<Source>(resolve => { finishFirst = resolve; }))
    .mockRejectedValueOnce(new KnowledgeApiError(503, "Ошибка индексации"))
    .mockResolvedValueOnce(secondPublished);
  render(<KnowledgeAdmin />);
  await user.click(await screen.findByRole("checkbox", { name: "Выбрать Synthetic guide" }));
  await user.click(screen.getByRole("checkbox", { name: "Выбрать Second guide" }));
  await user.click(screen.getByRole("button", { name: "Подтвердить и опубликовать выбранные" }));
  const dialogue = within(await screen.findByRole("dialog"));
  expect(dialogue.getAllByRole("link", { name: "Скачать оригинал" })).toHaveLength(2);
  await user.click(dialogue.getByRole("button", { name: "Подтвердить и опубликовать" }));
  expect(await screen.findByText("Обрабатывается")).toBeInTheDocument();
  expect(api.reviewPublish).toHaveBeenCalledTimes(1);
  finishFirst(source("published", 1));
  expect(await screen.findByText("Опубликовано: 1. С ошибкой: 1.")).toBeInTheDocument();
  expect(screen.getByText("Ошибка")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Ошибка индексации");
  expect(api.reviewPublish).toHaveBeenNthCalledWith(2, second);
  await user.click(screen.getByRole("button", { name: "Повторить обработку" }));
  const retry = within(await screen.findByRole("dialog"));
  await user.click(retry.getByRole("button", { name: "Подтвердить и опубликовать" }));
  expect(await screen.findByText("Опубликовано: 1. С ошибкой: 0.")).toBeInTheDocument();
  expect(screen.getAllByText("Доступен для поиска")).toHaveLength(2);
  expect(api.reviewPublish).toHaveBeenNthCalledWith(3, second);
});
