"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { DocumentApiError } from "../../lib/documents-api";
import { searchDocuments, type SearchHit } from "../../lib/rag-api";
import { Button } from "../ui/button";
import { Input } from "../ui/input";

export function PersonalSearch() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<SearchHit[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [searched, setSearched] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setBusy(true); setError(""); setItems([]); setSearched(false);
    try {
      const result = await searchDocuments(query.trim(), current.signal);
      if (!current.signal.aborted) { setItems(result.items); setSearched(true); }
    } catch (failure) {
      if (current.signal.aborted) return;
      if (failure instanceof DocumentApiError && failure.status === 401) router.replace("/login");
      setError(failure instanceof DocumentApiError && failure.status === 403
        ? failure.message : "Поиск временно недоступен. Попробуйте позже.");
    } finally { if (!current.signal.aborted) setBusy(false); }
  }

  return <main className="space-y-6">
    <h1 className="text-2xl font-semibold">Поиск по моим документам</h1>
    <p className="text-sm text-muted-foreground">Найдите фрагменты по смыслу. В поиске участвуют документы, подготовленные для поиска.</p>
    <form onSubmit={submit} className="flex flex-wrap gap-3">
      <label htmlFor="personal-query" className="sr-only">Что найти</label>
      <Input id="personal-query" value={query} maxLength={2000} required
        onChange={event => setQuery(event.target.value)} placeholder="Что найти в документах?"
        className="min-w-48 flex-1" />
      <Button disabled={busy || !query.trim()}>{busy ? "Ищем…" : "Найти"}</Button>
    </form>
    {error && <p role="alert">{error}</p>}
    {busy && <p role="status">Ищем подходящие фрагменты…</p>}
    {searched && !items.length && <p role="status">Подходящих фрагментов не найдено. Проверьте готовность документов к поиску.</p>}
    <ol className="space-y-4" aria-label="Результаты поиска">{items.map(hit => <li
      key={hit.chunk_id} className="rounded-xl border border-border bg-card p-5">
      <Link className="font-medium text-primary underline" href={hit.source_url}>{hit.filename}</Link>
      <p className="mt-2 text-xs text-muted-foreground">{hit.sources.map(source =>
        <Link key={`${source.kind}-${source.index}`} href={source.url ?? hit.source_url}
          className="mr-2 underline">{source.kind === "page" ? "Страница" : "Блок"} {source.index}</Link>)}
        {` · Сходство: ${hit.score.toFixed(3)}`}</p>
      <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7">{hit.text}</p>
    </li>)}</ol>
  </main>;
}
