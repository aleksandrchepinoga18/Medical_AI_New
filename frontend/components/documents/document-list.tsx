"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Download, FileText, FolderOpen, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import { Button } from "../ui/button";
import { Card } from "../ui/card";
import { StatusBadge } from "./status-badge";
import { activeStatuses, canDownload, canRetry, DocumentApiError, downloadDocument, listDocuments,
  processingMessage, retryDocument, type DocumentPage } from "../../lib/documents-api";

export function DocumentList() {
  const router = useRouter();
  const [data, setData] = useState<DocumentPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const result = await listDocuments(offset, controller.signal);
        if (controller.signal.aborted) return;
        setData(result);
        setError("");
        if (result.items.some(item => activeStatuses.has(item.status))) timer = setTimeout(load, 4000);
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof DocumentApiError && failure.status === 401) router.replace("/login");
        setError(failure instanceof DocumentApiError ? failure.message : "Не удалось обновить документы. Повторите попытку.");
      }
    }
    void load();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [offset, refresh, router]);

  async function action(id: string, task: () => Promise<unknown>) {
    setBusy(id);
    setError("");
    try { await task(); setRefresh(value => value + 1); }
    catch (failure) {
      if (failure instanceof DocumentApiError && failure.status === 401) router.replace("/login");
      setError(failure instanceof DocumentApiError ? failure.message : "Не удалось выполнить операцию. Повторите позже.");
    } finally { setBusy(null); }
  }
  return <main className="space-y-7">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div><p className="mb-2 text-xs font-semibold uppercase tracking-widest text-primary">Ваши материалы</p>
        <h1 className="text-3xl font-semibold tracking-tight">Документы</h1>
        <p className="mt-2 text-sm text-muted-foreground">Оригиналы и извлечённый текст в одном месте.</p></div>
      <Button asChild><Link href="/documents/upload"><Plus />Загрузить документ</Link></Button>
    </div>
    <div className="grid gap-4 sm:grid-cols-2">
      <Card className="flex items-center gap-4 p-5"><span className="rounded-xl bg-primary/10 p-3 text-primary"><FileText className="size-5" /></span>
        <div><p className="text-2xl font-semibold">{data?.total ?? "—"}</p><p className="text-sm text-muted-foreground">Всего документов</p></div></Card>
      <Card className="flex items-center gap-4 p-5"><ShieldCheck className="size-7 shrink-0 text-primary" />
        <div><p className="font-medium">Личный доступ</p><p className="mt-1 text-sm text-muted-foreground">PDF и DOCX. Оригиналы сохраняются без изменений.</p></div></Card>
    </div>
    {error && <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">{error}</div>}
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-6 py-4">
        <h2 className="font-semibold">Мои документы</h2><Button variant="ghost" size="sm" onClick={() => setRefresh(value => value + 1)}><RefreshCw />Обновить</Button>
      </div>
      {!data && !error && <p role="status" className="p-10 text-center text-muted-foreground">Загружаем список…</p>}
      {data?.items.length === 0 && <div className="px-6 py-16 text-center"><FolderOpen className="mx-auto mb-4 size-12 text-primary/50" />
        <h3 className="text-lg font-medium">Здесь появятся ваши документы</h3><p className="mx-auto mb-6 mt-2 max-w-sm text-sm text-muted-foreground">Загрузите первый PDF или DOCX, чтобы сохранить оригинал и извлечь текст.</p>
        <Button asChild variant="outline"><Link href="/documents/upload">Выбрать документ</Link></Button></div>}
      {data && data.items.length > 0 && <div className="overflow-x-auto"><table className="w-full text-left text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground"><tr><th className="px-6 py-3 font-medium">Документ</th><th className="px-4 py-3 font-medium">Статус</th><th className="hidden px-4 py-3 font-medium lg:table-cell">Добавлен</th><th className="px-6 py-3 text-right font-medium">Действия</th></tr></thead>
        <tbody className="divide-y divide-border">{data.items.map(item => <tr key={item.id} className="hover:bg-muted/30">
          <td className="max-w-xs px-6 py-5"><Link href={`/documents/${item.id}`} className="flex items-center gap-3 hover:text-primary"><span className="rounded-lg border border-border bg-background p-2 text-primary"><FileText className="size-5" /></span>
            <span className="min-w-0"><span className="block truncate font-medium">{item.original_filename}</span><span className="mt-1 block text-xs text-muted-foreground">{item.format.toUpperCase()} · {(item.size_bytes / 1024).toFixed(1)} КБ</span></span></Link>
            {item.error_code && <p className="mt-2 max-w-xs text-xs text-muted-foreground">{processingMessage(item.error_code)}</p>}</td>
          <td className="px-4 py-5"><StatusBadge status={item.status} /></td>
          <td className="hidden whitespace-nowrap px-4 py-5 text-muted-foreground lg:table-cell">{new Date(item.created_at).toLocaleDateString("ru-RU")}</td>
          <td className="px-6 py-5"><div className="flex justify-end gap-1">
            <Button variant="ghost" size="icon" aria-label={`Скачать ${item.original_filename}`} disabled={busy !== null || !canDownload(item)}
              onClick={() => action(item.id, () => downloadDocument(item.id, item.original_filename))}><Download /></Button>
            {canRetry(item) && <Button variant="outline" size="sm" disabled={busy !== null} onClick={() => action(item.id, () => retryDocument(item.id))}><RefreshCw />Повторить</Button>}
          </div></td>
        </tr>)}</tbody>
      </table></div>}
      {data && data.total > 25 && <div className="flex items-center justify-between border-t border-border px-6 py-4 text-sm">
        <span className="text-muted-foreground">{offset + 1}–{Math.min(offset + 25, data.total)} из {data.total}</span>
        <div className="flex gap-2"><Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 25))}>Назад</Button>
          <Button variant="outline" size="sm" disabled={offset + 25 >= data.total} onClick={() => setOffset(value => value + 25)}>Далее</Button></div>
      </div>}
    </Card>
    <p className="text-xs leading-relaxed text-muted-foreground">Сканированные PDF могут потребовать OCR. Распознавание изображений и медицинский анализ пока недоступны.</p>
  </main>;
}
