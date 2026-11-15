"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { DocumentApiError } from "../../lib/documents-api";
import { indexStatus, reindexDocument, removeDocumentIndex, type IndexStatus } from "../../lib/rag-api";
import { Button } from "../ui/button";

const names: Record<string, string> = {
  not_indexed: "Ещё не подготовлен к поиску", pending: "Подготовка запланирована",
  published: "Ожидает обработки", processing: "Подготавливается к поиску",
  ready: "Доступен для поиска", failed: "Подготовка к поиску не удалась",
  deleted: "Убран из поиска", outdated: "Нужно обновить поиск по документу",
  unavailable: "Текст документа пока недоступен для поиска",
};

export function IndexPanel({ id, ready }: { id: string; ready: boolean }) {
  const router = useRouter();
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const value = await indexStatus(id, controller.signal);
        if (controller.signal.aborted) return;
        setStatus(value); setError("");
        if (["pending", "published", "processing"].includes(value.status))
          timer = setTimeout(load, 4000);
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof DocumentApiError && failure.status === 401) router.replace("/login");
        setError("Статус поиска временно недоступен.");
      }
    }
    void load();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [id, ready, refresh, router]);
  async function action(remove: boolean) {
    setBusy(true);
    try {
      if (remove) await removeDocumentIndex(id); else await reindexDocument(id);
      setRefresh(value => value + 1);
    } catch (failure) {
      if (failure instanceof DocumentApiError && failure.status === 401) router.replace("/login");
      setError("Операция недоступна. Обновите статус перед повторной попыткой.");
    } finally { setBusy(false); }
  }
  const active = !!status && ["pending", "published", "processing"].includes(status.status);
  return <section className="space-y-3 rounded-xl border border-border p-5" aria-label="Поиск по документу">
    <h2 className="font-semibold">Поиск по документу</h2>
    <p role="status">{status ? names[status.status] ?? "Статус недоступен" : "Проверяем готовность…"}</p>
    {status?.operation === "delete" && active && <p>Исключается из поиска.</p>}
    {error && <p role="alert">{error}</p>}
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" disabled={busy || active || !ready || !status}
        onClick={() => action(false)}>Подготовить заново</Button>
      <Button variant="ghost" disabled={busy || !status || status.status === "deleted"}
        onClick={() => action(true)}>Убрать из поиска</Button>
      <Button variant="ghost" onClick={() => setRefresh(value => value + 1)}>Обновить статус</Button>
    </div>
    <p className="text-xs text-muted-foreground">Оригинал и извлечённый текст сохраняются.</p>
  </section>;
}
