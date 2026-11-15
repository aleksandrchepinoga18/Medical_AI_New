import { DocumentApiError } from "./documents-api";

export type Source = { kind: string; index: number; start: number; end: number; url?: string };
export type SearchHit = {
  chunk_id: string; document_id: string; text_version_id: string; ordinal: number;
  text: string; score: number; sources: Source[]; filename: string; source_url: string;
};
export type IndexStatus = {
  status: string; operation: string; attempts: number; chunk_count: number;
  error_code: string | null; text_version_id: string | null;
};

async function request<T>(path: string, method = "GET", body?: unknown,
  signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    const csrf = await fetch("/api/auth/csrf", { cache: "no-store", signal });
    if (!csrf.ok) throw new DocumentApiError(csrf.status);
    headers["X-CSRF-Token"] = (await csrf.json()).csrf_token;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api/rag${path}`, {
    method, headers, cache: "no-store", signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new DocumentApiError(response.status);
  return response.json();
}

export function searchDocuments(query: string, signal?: AbortSignal) {
  return request<{ items: SearchHit[]; k: number }>("/search", "POST", { query, k: 5 }, signal);
}
export function indexStatus(id: string, signal?: AbortSignal) {
  return request<IndexStatus>(`/documents/${encodeURIComponent(id)}/index`, "GET", undefined, signal);
}
export function reindexDocument(id: string) {
  return request<{ status: string }>(`/documents/${encodeURIComponent(id)}/index`, "POST");
}
export function removeDocumentIndex(id: string) {
  return request<{ status: string }>(`/documents/${encodeURIComponent(id)}/index`, "DELETE");
}
