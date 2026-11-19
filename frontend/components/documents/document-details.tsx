"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, Download, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader } from "../ui/card";
import { Button } from "../ui/button";
import { StatusBadge } from "./status-badge";
import { IndexPanel } from "./index-panel";
import { activeStatuses, canDownload, canRetry, DocumentApiError, downloadDocument, getDocument,
  processingMessage, retryDocument, type DocumentDetails as Details } from "../../lib/documents-api";

export function DocumentDetails({ id }: { id: string }) {
  const router = useRouter();
  const [data, setData] = useState<Details | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const result = await getDocument(id, controller.signal);
        if (controller.signal.aborted) return;
        setData(result); setError("");
        if (activeStatuses.has(result.status)) timer = setTimeout(load, 4000);
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof DocumentApiError && failure.status === 401) router.replace("/login");
        setError(failure instanceof DocumentApiError ? failure.message : "Не удалось получить документ.");
      }
    }
    void load();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [id, refresh, router]);

  async function action(task: () => Promise<unknown>) {
    setBusy(true);
    try { await task(); setRefresh(value => value + 1); }
    catch (failure) {
      if (failure instanceof DocumentApiError && failure.status === 401) router.replace("/login");
      setError(failure instanceof DocumentApiError ? failure.message : "Операция не выполнена.");
    } finally { setBusy(false); }
  }
  return <main className="space-y-6">
    <Button asChild variant="ghost" className="-ml-3"><Link href="/documents"><ArrowLeft />К документам</Link></Button>
    {error && <div role="alert" className="rounded-xl bg-rose-50 p-4 text-sm text-rose-800">{error}<Button variant="ghost" size="sm" onClick={() => setRefresh(value => value + 1)}>Обновить</Button></div>}
    {!data && !error && <p role="status">Загружаем документ…</p>}
    {data && <>
      <div className="flex flex-wrap items-start justify-between gap-4"><div className="min-w-0"><h1 className="break-all text-2xl font-semibold">№ {data.document_number} · {data.original_filename}</h1>
        <div className="mt-3 flex items-center gap-3"><StatusBadge status={data.status} /><span className="text-sm text-muted-foreground">{data.format.toUpperCase()} · {(data.size_bytes / 1024).toFixed(1)} КБ</span></div></div>
        <div className="flex gap-2"><Button variant="outline" disabled={busy || !canDownload(data)} onClick={() => action(() => downloadDocument(data.id, data.original_filename))}><Download />Оригинал</Button>
          {canRetry(data) && <Button disabled={busy} onClick={() => action(() => retryDocument(data.id))}><RefreshCw />Повторить</Button>}</div>
      </div>
      {data.error_code && <p className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">{processingMessage(data.error_code)}</p>}
      <IndexPanel id={id} ready={data.status === "ready"} />
      <Card><CardHeader><h2 className="font-semibold">Извлечённый текст</h2><p className="mt-1 text-xs text-muted-foreground">{data.text_version ? `Версия ${data.text_version.version}. Текст показан без медицинской интерпретации.` : "Текст появится после обработки документа."}</p></CardHeader>
        <CardContent>{data.text_version ? <pre id="extracted-text" className="max-h-[65vh] overflow-auto whitespace-pre-wrap break-words font-sans text-sm leading-7">{data.text_version.locators.length
          ? data.text_version.locators.map((locator, index) => <span key={index}
            id={`source-${locator.kind}-${locator.index}`} className="block scroll-mt-24">
            {data.text_version!.normalized_text.slice(Number(locator.start), Number(locator.end))}
          </span>) : data.text_version.normalized_text || "Текст не найден."}</pre>
          : <p className="py-8 text-center text-sm text-muted-foreground">{activeStatuses.has(data.status) ? "Обработка продолжается…" : "Текст пока недоступен."}</p>}</CardContent></Card>
    </>}
  </main>;
}
