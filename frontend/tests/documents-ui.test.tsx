// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DocumentList } from "../components/documents/document-list";
import { UploadForm } from "../components/documents/upload-form";
import { DocumentDetails } from "../components/documents/document-details";
import { DocumentApiError } from "../lib/documents-api";

const mocks = vi.hoisted(() => ({
  router: { push: vi.fn(), replace: vi.fn(), refresh: vi.fn() },
  list: vi.fn(), upload: vi.fn(), retry: vi.fn(), download: vi.fn(), details: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => mocks.router }));
vi.mock("../components/documents/index-panel", () => ({ IndexPanel: () => null }));
vi.mock("../lib/documents-api", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/documents-api")>(),
  listDocuments: mocks.list, uploadDocument: mocks.upload, retryDocument: mocks.retry,
  downloadDocument: mocks.download, getDocument: mocks.details,
}));

const item = {
  id: "synthetic-id", document_number: 7, index_status: "ready",
  original_filename: "synthetic.pdf", format: "pdf", size_bytes: 123,
  sha256: "0".repeat(64), status: "failed", error_code: "PROCESSING_TIMEOUT",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => vi.resetAllMocks());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows an empty state and upload navigation", async () => {
  mocks.list.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 25 });
  render(<DocumentList />);
  expect(await screen.findByText("Здесь появятся ваши документы")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Загрузить документ" })).toHaveAttribute("href", "/documents/upload");
});

it("shows retry and download actions and a processing error", async () => {
  mocks.list.mockResolvedValue({ items: [item], total: 1, offset: 0, limit: 25 });
  mocks.retry.mockResolvedValue({ ...item, status: "queued" });
  mocks.download.mockResolvedValue(undefined);
  render(<DocumentList />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Повторить" }));
  expect(mocks.retry).toHaveBeenCalledWith("synthetic-id");
  await waitFor(() => expect(screen.getByRole("button", { name: "Скачать synthetic.pdf" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Скачать synthetic.pdf" }));
  expect(mocks.download).toHaveBeenCalledWith("synthetic-id", "synthetic.pdf");
  expect(screen.getByText("Превышено время обработки.")).toBeInTheDocument();
});

it("redirects an expired session", async () => {
  mocks.list.mockRejectedValue(new DocumentApiError(401));
  render(<DocumentList />);
  await waitFor(() => expect(mocks.router.replace).toHaveBeenCalledWith("/login"));
});

it("shows the persistent number and searches the server with the selected order", async () => {
  mocks.list.mockResolvedValue({ items: [item], total: 1, offset: 0, limit: 25 });
  render(<DocumentList />);
  expect(await screen.findByText("№ 7 · synthetic.pdf")).toBeInTheDocument();
  expect(screen.getByText("Индекс готов")).toBeInTheDocument();
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Поиск по номеру, имени или дате"), "№ 7");
  await user.click(screen.getByRole("button", { name: "Найти" }));
  await user.selectOptions(screen.getByLabelText("Сортировка"), "number");
  await waitFor(() => expect(mocks.list).toHaveBeenLastCalledWith(
    0, expect.any(AbortSignal), "№ 7", "number", "desc",
  ));
});

it("keeps the same upload key after a network failure", async () => {
  const firstKey = "00000000-0000-4000-8000-000000000001";
  const randomUUID = vi.fn()
    .mockReturnValueOnce(firstKey)
    .mockReturnValueOnce("00000000-0000-4000-8000-000000000002");
  vi.stubGlobal("crypto", { randomUUID });
  mocks.upload.mockRejectedValueOnce(new TypeError("network failure")).mockResolvedValueOnce(item);
  render(<UploadForm />);
  const user = userEvent.setup();
  const file = new File(["%PDF-synthetic"], "synthetic.pdf", { type: "application/pdf" });
  const input = screen.getByLabelText<HTMLInputElement>("Документ");
  await user.upload(input, file);
  expect(input.files?.[0]).toBe(file);
  const form = input.closest("form");
  expect(form).not.toBeNull();
  expect(screen.getByRole("button", { name: "Загрузить" })).toBeEnabled();
  // user-event mocks the wrapper's FileList, while jsdom's native required check
  // reads the internal list. Dispatch submit to test the handler, not that mismatch.
  fireEvent.submit(form!);
  await waitFor(() => expect(mocks.upload).toHaveBeenCalledTimes(1));
  expect(await screen.findByRole("alert")).toHaveTextContent("Соединение прервано");
  expect(mocks.router.push).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole("button", { name: "Загрузить" })).toBeEnabled());
  fireEvent.submit(form!);
  await waitFor(() => expect(mocks.router.push).toHaveBeenCalledWith("/documents"));
  expect(mocks.upload).toHaveBeenCalledTimes(2);
  expect(mocks.upload.mock.calls[0]).toEqual([file, firstKey]);
  expect(mocks.upload.mock.calls[1]).toEqual([file, firstKey]);
  expect(mocks.upload.mock.calls[0][1]).toBe(mocks.upload.mock.calls[1][1]);
  expect(randomUUID).toHaveBeenCalledTimes(1);
});

it("renders extracted document text as text rather than HTML", async () => {
  mocks.details.mockResolvedValue({ ...item, status: "ready", error_code: null,
    text_version: { version: 1, normalized_text: "<script>synthetic</script>", locators: [] } });
  render(<DocumentDetails id="synthetic-id" />);
  expect(await screen.findByText("<script>synthetic</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
});
