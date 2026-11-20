export type DocumentItem = {
  id: string; document_number: number; original_filename: string; format: string;
  size_bytes: number; sha256: string; index_status?: string;
  status: string; error_code: string | null; created_at: string; updated_at: string;
};
export type DocumentPage = { items: DocumentItem[]; total: number; offset: number; limit: number };
export type ExtractionJob = { job_id: string; status: string; error_code: string | null; text_version: number };
export type Measurement = {
  name: string; value: string; unit: string | null; reference: string | null; date: string | null;
  reported_page: number | null; source: { page: number; bbox: number[] } | null;
  confirmation: "confirmed" | "unverified" | "ambiguous";
};
export type ExtractionReport = {
  provider: "pymupdf_tables" | "gigachat_ultra"; provider_version: string; model: string | null;
  measurements: Measurement[]; truncated: boolean;
};
export type DocumentDetails = DocumentItem & {
  text_version: { version: number; normalized_text: string; extractor_version: string;
    locators: Array<Record<string, string | number>>;
    structured_result?: { local: ExtractionReport | null; external: ExtractionReport | null } | null;
  } | null;
  local_refresh?: ExtractionJob | null;
  external_extraction?: ExtractionJob | null;
  external_extraction_enabled?: boolean;
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

export type DocumentOrder = "number" | "name" | "date";

export async function listDocuments(offset: number, signal?: AbortSignal,
  query = "", sort: DocumentOrder = "date", direction: "asc" | "desc" = "desc",
): Promise<DocumentPage> {
  const params = new URLSearchParams({ offset: String(offset), limit: "25", q: query, sort, direction });
  return (await checked(await fetch(`/api/documents?${params}`, {
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

export async function refreshExtraction(id: string): Promise<ExtractionJob> {
  const token = await csrf();
  return (await checked(await fetch(`/api/documents/${encodeURIComponent(id)}/refresh-extraction`, {
    method: "POST", cache: "no-store", headers: { "X-CSRF-Token": token },
  }))).json();
}

export async function authorizeUltra(
  id: string, sha256: string, textVersion: number,
): Promise<ExtractionJob> {
  const token = await csrf();
  return (await checked(await fetch(`/api/documents/${encodeURIComponent(id)}/external-extraction`, {
    method: "POST", cache: "no-store",
    headers: { "X-CSRF-Token": token, "Content-Type": "application/json" },
    body: JSON.stringify({ provider: "gigachat_ultra", consent: true, sha256, text_version: textVersion }),
  }))).json();
}

export function previewOriginal(id: string, page?: number): string {
  return `/api/documents/${encodeURIComponent(id)}/download?inline=true${page ? `#page=${page}` : ""}`;
}

export async function downloadDocument(id: string, filename: string): Promise<void> {
  const response = await checked(await fetch(`/api/documents/${encodeURIComponent(id)}/download`, {
    cache: "no-store",
  }));
  saveDownload(await response.blob(), filename);
}

export function downloadExtractedText(text: string, filename: string): void {
  saveDownload(new Blob([text], { type: "text/plain;charset=utf-8" }),
    `${filename.replace(/\.[^.]+$/, "")}-извлечённый.txt`);
}

function saveDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
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
export const activeIndexStatuses = new Set(["pending", "published", "processing"]);
export const extractionJobStatuses: Record<string, string> = {
  pending: "В очереди", published: "В очереди", processing: "Обрабатывается",
  succeeded: "Завершено", failed: "Ошибка",
};
export const indexStatusNames: Record<string, string> = {
  disabled: "Поиск выключен", not_indexed: "Индекс отсутствует", pending: "Индекс в очереди",
  published: "Индекс в очереди", processing: "Индексация", ready: "Индекс готов",
  failed: "Ошибка индексации", deleted: "Индекс удалён", outdated: "Индекс устарел",
  unavailable: "Индекс недоступен",
};
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
    EXTERNAL_TIMEOUT: "GigaChat не ответил за отведённое время.",
    EXTERNAL_API_ERROR: "GigaChat не вернул пригодный результат извлечения.",
    EXTERNAL_MODEL_MISMATCH: "Ответ получен от другой модели.",
    EXTERNAL_DISABLED: "Внешнее извлечение отключено.",
    EXTERNAL_NOT_CONFIGURED: "Подключение GigaChat не настроено.",
    EXTERNAL_TLS_CONFIG: "Не удалось проверить настройки сертификатов GigaChat.",
    EXTERNAL_TLS_ERROR: "Не удалось проверить сертификат GigaChat. Проверьте доверенные CA в worker.",
    EXTERNAL_NETWORK_ERROR: "Не удалось установить соединение с GigaChat.",
    EXTERNAL_AUTH_ERROR: "GigaChat отклонил авторизацию. Проверьте ключ и область доступа.",
    EXTERNAL_UPLOAD_UNCONFIRMED: "Статус загрузки в GigaChat неизвестен. Повторная отправка заблокирована.",
    EXTERNAL_CLEANUP_UNCONFIRMED: "Удаление файла в GigaChat не подтверждено. Повторная отправка заблокирована.",
    EXTERNAL_INTERRUPTED: "Внешнее извлечение прервано. Повторная отправка заблокирована.",
  };
  return messages[code] ?? "Обработка не удалась. Можно повторить попытку или загрузить другой файл.";
}

export function canRetry(document: DocumentItem): boolean {
  return document.status === "failed" && !["UPLOAD_INCOMPLETE", "ORIGINAL_MISSING", "CHECKSUM_MISMATCH"].includes(document.error_code ?? "");
}

export function canDownload(document: DocumentItem): boolean {
  return document.status !== "uploading" && !["UPLOAD_INCOMPLETE", "ORIGINAL_MISSING", "CHECKSUM_MISMATCH"].includes(document.error_code ?? "");
}
