export type DocumentItem = {
  id: string; original_filename: string; format: string; size_bytes: number; sha256: string;
  status: string; error_code: string | null; created_at: string; updated_at: string;
};
export type DocumentPage = { items: DocumentItem[]; total: number; offset: number; limit: number };
export type DocumentDetails = DocumentItem & {
  text_version: { version: number; normalized_text: string; extractor_version: string;
    locators: Array<Record<string, string | number>> } | null;
};

export class DocumentApiError extends Error {
  constructor(public status: number) {
    super(status === 401 ? "Сессия завершена. Войдите снова."
      : status === 403 ? "Проверка безопасности не пройдена. Обновите страницу."
      : status === 404 ? "Документ не найден."
      : status === 409 ? "Операция недоступна для текущего состояния документа."
      : status === 413 ? "Размер файла превышает допустимый лимит."
      : status === 415 ? "Поддерживаются только корректные PDF и DOCX."
      : status === 429 ? "Достигнут лимит документов или повторных попыток."
      : "Сервис временно недоступен. Проверьте список перед повторной загрузкой.");
  }
}

async function checked(response: Response): Promise<Response> {
  if (!response.ok) throw new DocumentApiError(response.status);
  return response;
}

async function csrf(): Promise<string> {
  const response = await checked(await fetch("/api/auth/csrf", { cache: "no-store" }));
  return (await response.json()).csrf_token;
}

export async function listDocuments(offset: number, signal?: AbortSignal): Promise<DocumentPage> {
  return (await checked(await fetch(`/api/documents?offset=${offset}&limit=25`, {
    cache: "no-store", signal,
  }))).json();
}

export async function getDocument(id: string, signal?: AbortSignal): Promise<DocumentDetails> {
  return (await checked(await fetch(`/api/documents/${encodeURIComponent(id)}`, {
    cache: "no-store", signal,
  }))).json();
}

export async function uploadDocument(file: File, key: string): Promise<DocumentItem> {
  const token = await csrf();
  const body = new FormData();
  body.append("file", file);
  return (await checked(await fetch("/api/documents", {
    method: "POST", cache: "no-store", body,
    headers: { "X-CSRF-Token": token, "Idempotency-Key": key },
  }))).json();
}

export async function retryDocument(id: string): Promise<DocumentItem> {
  const token = await csrf();
  return (await checked(await fetch(`/api/documents/${encodeURIComponent(id)}/retry`, {
    method: "POST", cache: "no-store", headers: { "X-CSRF-Token": token },
  }))).json();
}

export async function downloadDocument(id: string, filename: string): Promise<void> {
  const response = await checked(await fetch(`/api/documents/${encodeURIComponent(id)}/download`, {
    cache: "no-store",
  }));
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const statusNames: Record<string, string> = {
  uploading: "Загружается", queued: "В очереди", processing: "Обрабатывается",
  ready: "Готов", failed: "Ошибка", needs_ocr: "Нужен OCR",
};
export const activeStatuses = new Set(["uploading", "queued", "processing"]);
export function processingMessage(code: string | null): string {
  if (!code) return "";
  const messages: Record<string, string> = {
    OCR_REQUIRED: "Обнаружены страницы без текста. Распознавание сканов пока недоступно.",
    NO_TEXT: "В документе не найден текст.", PASSWORD_PROTECTED: "Документ защищён паролем.",
    PAGE_LIMIT: "Превышен лимит страниц.", TEXT_LIMIT: "Превышен лимит извлечённого текста.",
    ARCHIVE_LIMIT: "Превышен лимит распакованного документа.",
    PROCESSING_TIMEOUT: "Превышено время обработки.",
    UPLOAD_INCOMPLETE: "Оригинал не получен полностью. Загрузите документ заново.",
    CHECKSUM_MISMATCH: "Контрольная сумма оригинала не совпала. Нужна новая загрузка.",
    ORIGINAL_MISSING: "Оригинал недоступен. Нужна новая загрузка.",
  };
  return messages[code] ?? "Обработка не удалась. Можно повторить попытку или загрузить другой файл.";
}

export function canRetry(document: DocumentItem): boolean {
  return document.status === "failed" && !["UPLOAD_INCOMPLETE", "ORIGINAL_MISSING", "CHECKSUM_MISMATCH"].includes(document.error_code ?? "");
}

export function canDownload(document: DocumentItem): boolean {
  return document.status !== "uploading" && !["UPLOAD_INCOMPLETE", "ORIGINAL_MISSING", "CHECKSUM_MISMATCH"].includes(document.error_code ?? "");
}
