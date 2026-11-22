import { csrf, processingMessage } from "./documents-api";

export type SourceStatus = "uploaded" | "verified" | "published" | "outdated" | "rejected";
export type SourceMetadata = {
  title: string; material_type: string; author: string; publisher: string; url: string;
  specialty: string; language: string; jurisdiction: string;
  doi: string | null; pmid: string | null; publication_date: string | null; update_date: string | null;
};
export type SourceVersion = {
  id: string; version: string; status: SourceStatus; format: string; sha256: string;
  review_revision: string; review_note: string | null; chunk_count: number;
};
export type Source = SourceMetadata & { id: string; current_version: SourceVersion };
export type SourcePage = { items: Source[]; next_after: string | null };
export type Locator = { kind: string; index: number; start: number; end: number };
export type SearchHit = {
  chunk_id: string; text: string; score?: number; locators: Locator[];
  source: Pick<SourceMetadata, "title" | "url"> & { id: string; version: string; version_id: string };
};

export class KnowledgeApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

const errors: Record<number, string> = {
  401: "Сессия завершена. Войдите снова.",
  403: "Доступ или проверка безопасности отклонены. Обновите страницу.",
  404: "Источник не найден.",
  409: "Источник уже существует или его состояние изменилось. Обновите реестр.",
  413: "Файл превышает допустимый размер.",
  415: "Выберите корректный PDF или DOCX.",
  422: "Проверьте метаданные, формат файла и поля проверки.",
};

const detailMessages: Record<string, string> = {
  "Medical source must be indexed with the current settings": "Сначала выполните индексацию.",
  "Medical source index is incomplete": "В индексе отсутствуют чанки. Повторите индексацию.",
  "Original is ineligible for the medical corpus": "Этот файл нельзя добавить в общий корпус.",
  "Medical source storage unavailable": "Не удалось сохранить оригинал. Проверьте MinIO.",
  "Medical source indexing unavailable": "Не удалось завершить индексацию. Проверьте состояние индекса.",
  "Medical source processing timed out": "Превышено время обработки файла.",
  "Medical source version changed": "Оригинал заменён. Обновите реестр и проверьте новую версию.",
  "Medical source review revision changed": "Данные источника изменились. Обновите реестр и проверьте их снова.",
  "Medical source checksum changed": "Контрольная сумма изменилась. Обновите реестр.",
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.method && init.method !== "GET") headers.set("X-CSRF-Token", await csrf());
  const response = await fetch(`/api/knowledge${path}`, { ...init, headers, cache: "no-store" });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    const code = detail?.code;
    const message = typeof code === "string"
      ? code === "NO_INDEXABLE_TEXT"
        ? "Для индексации нужен PDF с текстовым слоем или DOCX с текстом."
        : `${processingMessage(code)} (${code})`
      : (typeof detail === "string" ? detailMessages[detail] : undefined);
    throw new KnowledgeApiError(
      response.status, message ?? errors[response.status] ?? "Медицинская база временно недоступна.",
    );
  }
  return response.json();
}

const sourcePath = (id: string) => `/sources/${encodeURIComponent(id)}`;

export function listSources(after?: string, signal?: AbortSignal): Promise<SourcePage> {
  const params = new URLSearchParams({ limit: "25" });
  if (after) params.set("after", after);
  return request(`/sources?${params}`, { signal });
}

export function uploadSource(
  metadata: SourceMetadata & { version: string }, file: File, id?: string,
): Promise<Source> {
  const body = new FormData();
  body.set("metadata", JSON.stringify(metadata));
  body.set("file", file);
  return request(id ? `${sourcePath(id)}/version` : "/sources", {
    method: id ? "PUT" : "POST", body,
  });
}

function jsonRequest<T>(path: string, method: "POST" | "PUT", body: unknown): Promise<T> {
  return request(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export const editSource = (id: string, metadata: SourceMetadata) =>
  jsonRequest<Source>(`${sourcePath(id)}/metadata`, "PUT", metadata);

export function setSourceStatus(source: Source, status: SourceStatus) {
  return jsonRequest<Source>(`${sourcePath(source.id)}/status`, "PUT", {
    version_id: source.current_version.id, status,
  });
}

export function reviewPublish(source: Source): Promise<Source> {
  const version = source.current_version;
  return jsonRequest<Source>(`${sourcePath(source.id)}/review-publish`, "POST", {
    version_id: version.id, review_revision: version.review_revision, verified_sha256: version.sha256,
  });
}

export function sourceOriginalUrl(source: Source): string {
  return `/api/knowledge${sourcePath(source.id)}/versions/${encodeURIComponent(source.current_version.id)}/original`;
}

export const searchKnowledge = (query: string, k: number) =>
  jsonRequest<{ items: SearchHit[] }>("/search", "POST", { query, k });
