// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PersonalSearch } from "../components/documents/personal-search";
import { IndexPanel } from "../components/documents/index-panel";
import { DocumentApiError } from "../lib/documents-api";

const mocks = vi.hoisted(() => ({
  search: vi.fn(), status: vi.fn(), reindex: vi.fn(), remove: vi.fn(), replace: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => mocks }));
vi.mock("../lib/rag-api", () => ({
  searchDocuments: mocks.search, indexStatus: mocks.status,
  reindexDocument: mocks.reindex, removeDocumentIndex: mocks.remove,
}));
beforeEach(() => vi.resetAllMocks());
afterEach(() => cleanup());

it("shows source links and renders retrieved text without executing markup", async () => {
  mocks.search.mockResolvedValue({ items: [{ chunk_id: "chunk", document_id: "doc",
    text: "<script>synthetic</script>", score: 0.75, filename: "synthetic.pdf",
    sources: [{ kind: "page", index: 2 }], source_url: "/documents/doc#source-page-2" }] });
  render(<PersonalSearch />);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Что найти"), "synthetic");
  await user.click(screen.getByRole("button", { name: "Найти" }));
  expect(await screen.findByText("<script>synthetic</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByRole("link", { name: "synthetic.pdf" }))
    .toHaveAttribute("href", "/documents/doc#source-page-2");
  expect(screen.getByText(/Страница 2/)).toBeInTheDocument();
});

it("redirects an expired search session", async () => {
  mocks.search.mockRejectedValue(new DocumentApiError(401));
  render(<PersonalSearch />);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Что найти"), "synthetic");
  await user.click(screen.getByRole("button", { name: "Найти" }));
  await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login"));
});

it("keeps extraction and search status separate and schedules reindexing", async () => {
  mocks.status.mockResolvedValue({ status: "failed", operation: "index", attempts: 5 });
  mocks.reindex.mockResolvedValue({ status: "pending" });
  render(<IndexPanel id="synthetic" ready />);
  expect(await screen.findByText("Подготовка к поиску не удалась")).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Подготовить заново" }));
  expect(mocks.reindex).toHaveBeenCalledWith("synthetic");
});
