"use client";

import Link from "next/link";
import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, Download, RefreshCw } from "lucide-react";
import { Card, CardContent, CardHeader } from "../ui/card";
import { Button } from "../ui/button";
import { StatusBadge } from "./status-badge";
import { IndexPanel } from "./index-panel";
import { ExtractedMeasurements } from "./extracted-measurements";
import {
  activeIndexStatuses, activeStatuses, authorizeUltra, canDownload, canRetry, DocumentApiError,
  downloadDocument, downloadExtractedText, extractionJobStatuses, getDocument, previewOriginal,
  processingMessage, refreshExtraction, retryDocument, type DocumentDetails as Details,
  type ExtractionJob,
} from "../../lib/documents-api";

const activeJob = (job?: ExtractionJob | null) => !!job && activeIndexStatuses.has(job.status);
const unresolvedExternal = new Set([
  "EXTERNAL_UPLOAD_UNCONFIRMED", "EXTERNAL_CLEANUP_UNCONFIRMED", "EXTERNAL_INTERRUPTED",
]);

function JobStatus({ job, label }: { job?: ExtractionJob | null; label: string }) {
  if (!job) return null;
  return <p role={job.error_code ? "alert" : "status"} className="text-sm">
    {label} · {extractionJobStatuses[job.status] ?? job.status}
    {job.error_code && ` · ${processingMessage(job.error_code)}`}
  </p>;
}

export function DocumentDetails({ id }: { id: string }) {
  const router = useRouter();
  const [loaded, setData] = useState<Details | null>(null);
  const data = loaded?.id === id ? loaded : null;
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [consent, setConsent] = useState<{
    id: string; sha256: string; version: number; filename: string; number: number;
  } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const result = await getDocument(id, controller.signal);
        if (controller.signal.aborted) return;
        setData(result); setError("");
        if (activeStatuses.has(result.status) || activeJob(result.local_refresh) ||
            activeJob(result.external_extraction)) timer = setTimeout(load, 4000);
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
  function startExtraction(
    field: "local_refresh" | "external_extraction", request: () => Promise<ExtractionJob>,
  ) {
    return action(async () => {
      const job = await request();
      setData(current => current?.id === id ? { ...current, [field]: job } : current);
    });
  }
  const textVersion = data?.text_version;
  const canExtract = data?.format === "pdf" && !!textVersion &&
    ["ready", "needs_ocr"].includes(data.status);
  const extracting = activeJob(data?.local_refresh) || activeJob(data?.external_extraction);
  const canSend = canExtract && !extracting && !busy &&
    !unresolvedExternal.has(data?.external_extraction?.error_code ?? "");
  const consentCurrent = consent?.id === data?.id && consent?.sha256 === data?.sha256 &&
    consent?.version === textVersion?.version;
  const reports = textVersion?.structured_result;
  return <main className="space-y-6">
    <Button asChild variant="ghost" className="-ml-3"><Link href="/documents"><ArrowLeft />К документам</Link></Button>
    {error && <div role="alert" className="rounded-xl bg-rose-50 p-4 text-sm text-rose-800">{error}<Button variant="ghost" size="sm" onClick={() => setRefresh(value => value + 1)}>Обновить</Button></div>}
    {!data && !error && <p role="status">Загружаем документ…</p>}
    {data && <>
      <div className="flex flex-wrap items-start justify-between gap-4"><div className="min-w-0"><h1 className="break-all text-2xl font-semibold">№ {data.document_number} · {data.original_filename}</h1>
        <div className="mt-3 flex items-center gap-3"><StatusBadge status={data.status} /><span className="text-sm text-muted-foreground">{data.format.toUpperCase()} · {(data.size_bytes / 1024).toFixed(1)} КБ</span></div></div>
        <div className="flex flex-wrap gap-2">
          {data.format === "pdf" && canDownload(data) && <Button asChild variant="outline">
            <a href={previewOriginal(data.id)} target="_blank" rel="noopener noreferrer">Просмотреть оригинал</a>
          </Button>}
          <Button variant="outline" disabled={busy || !canDownload(data)}
            onClick={() => action(() => downloadDocument(data.id, data.original_filename))}><Download />Скачать оригинал</Button>
          {canRetry(data) && <Button disabled={busy} onClick={() => action(() => retryDocument(data.id))}><RefreshCw />Повторить</Button>}</div>
      </div>
      {data.error_code && <p className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">{processingMessage(data.error_code)}</p>}
      <IndexPanel id={id} ready={data.status === "ready"} />
      {data.format === "pdf" && <Card>
        <CardHeader><h2 className="font-semibold">Измерения</h2></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled={!canExtract || extracting || busy}
              onClick={() => startExtraction("local_refresh", () => refreshExtraction(data.id))}>
              Повторить локальное извлечение
            </Button>
            {data.external_extraction_enabled && <Dialog.Root open={!!consent && consent.id === data.id}
              onOpenChange={open => setConsent(open && textVersion ? {
                id: data.id, sha256: data.sha256, version: textVersion.version,
                filename: data.original_filename, number: data.document_number,
              } : null)}>
              <Dialog.Trigger asChild>
                <Button variant="outline" disabled={!canSend}>Обработать через GigaChat Ultra</Button>
              </Dialog.Trigger>
              <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 z-50 bg-black/40" />
                <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[min(90vw,32rem)] -translate-x-1/2 -translate-y-1/2 space-y-4 rounded-xl border bg-background p-6 shadow-lg">
                  <Dialog.Title className="font-semibold">Передать PDF в GigaChat Ultra?</Dialog.Title>
                  <Dialog.Description className="break-words text-sm text-muted-foreground">
                    Внешнему провайдеру будет передан полный оригинал «{consent?.filename}» (документ № {consent?.number}, версия {consent?.version}).
                  </Dialog.Description>
                  {!consentCurrent && <p role="status" className="text-sm">Версия изменилась. Откройте подтверждение заново.</p>}
                  <div className="flex flex-wrap justify-end gap-2">
                    <Dialog.Close asChild><Button variant="outline">Отмена</Button></Dialog.Close>
                    <Button disabled={!consentCurrent || !canSend} onClick={() => {
                      if (!consent || !consentCurrent || !canSend) return;
                      setConsent(null);
                      void startExtraction("external_extraction",
                        () => authorizeUltra(consent.id, consent.sha256, consent.version));
                    }}>Подтвердить передачу PDF</Button>
                  </div>
                </Dialog.Content>
              </Dialog.Portal>
            </Dialog.Root>}
          </div>
          <JobStatus job={data.local_refresh} label="PyMuPDF Tables" />
          <JobStatus job={data.external_extraction} label="GigaChat Ultra" />
          {reports?.local && <ExtractedMeasurements id={data.id} report={reports.local} />}
          {reports?.external && <ExtractedMeasurements id={data.id} report={reports.external} />}
          {!reports?.local && !reports?.external && <p className="text-sm text-muted-foreground">
            {canExtract ? "Измерения ещё не сохранены. Можно извлечь их из оригинала." : "Измерения пока недоступны."}
          </p>}
        </CardContent>
      </Card>}
      <Card><CardHeader><h2 className="font-semibold">Извлечённый текст</h2><p className="mt-1 text-xs text-muted-foreground">{data.text_version ? `Версия ${data.text_version.version}. Текст показан без медицинской интерпретации.` : "Текст появится после обработки документа."}</p></CardHeader>
        {textVersion?.normalized_text && <CardContent><Button variant="outline"
          onClick={() => downloadExtractedText(textVersion.normalized_text, data.original_filename)}>
          <Download />Скачать извлечённый текст
        </Button></CardContent>}
        <CardContent>{data.text_version ? <pre id="extracted-text" className="max-h-[65vh] overflow-auto whitespace-pre-wrap break-words font-sans text-sm leading-7">{data.text_version.locators.length
          ? data.text_version.locators.map((locator, index) => <span key={index}
            id={`source-${locator.kind}-${locator.index}`} className="block scroll-mt-24">
            {data.text_version!.normalized_text.slice(Number(locator.start), Number(locator.end))}
          </span>) : data.text_version.normalized_text || "Текст не найден."}</pre>
          : <p className="py-8 text-center text-sm text-muted-foreground">{activeStatuses.has(data.status) ? "Обработка продолжается…" : "Текст пока недоступен."}</p>}</CardContent></Card>
    </>}
  </main>;
}
