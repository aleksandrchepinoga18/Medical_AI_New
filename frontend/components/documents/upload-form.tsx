"use client";

import Link from "next/link";
import { useState, useRef, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, FileUp, ShieldCheck } from "lucide-react";
import { Card, CardContent, CardHeader } from "../ui/card";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { DocumentApiError, uploadDocument } from "../../lib/documents-api";

export function UploadForm() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const key = useRef<string | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file || busy) return;
    setError("");
    if (!/\.(pdf|docx)$/i.test(file.name)) { setError("Выберите PDF или DOCX."); return; }
    setBusy(true);
    try {
      key.current ??= crypto.randomUUID();
      await uploadDocument(file, key.current);
      router.push("/documents");
      router.refresh();
    } catch (failure) {
      if (failure instanceof DocumentApiError && failure.status === 401) router.replace("/login");
      setError(failure instanceof DocumentApiError ? failure.message : "Соединение прервано. Проверьте список или повторите загрузку этого же файла.");
    } finally { setBusy(false); }
  }
  return <main className="mx-auto max-w-2xl space-y-6">
    <Button asChild variant="ghost" className="-ml-3"><Link href="/documents"><ArrowLeft />К документам</Link></Button>
    <div><h1 className="text-3xl font-semibold tracking-tight">Загрузка документа</h1><p className="mt-2 text-sm text-muted-foreground">Сохраним оригинал и подготовим текст для просмотра.</p></div>
    <Card><CardHeader><h2 className="font-semibold">Выберите PDF или DOCX</h2></CardHeader><CardContent>
      <form onSubmit={submit} className="space-y-6">
        <div className="rounded-xl border border-dashed border-primary/30 bg-primary/5 p-6 sm:p-9">
          <FileUp className="mb-4 size-9 text-primary" /><label htmlFor="document-file" className="mb-3 block text-sm font-medium">Документ</label>
          <Input id="document-file" type="file" accept=".pdf,.docx" required disabled={busy} onChange={event => {
            setFile(event.target.files?.[0] ?? null); key.current = null; setError("");
          }} />
          {file && <p className="mt-3 break-all text-sm text-muted-foreground">{file.name} · {(file.size / 1024).toFixed(1)} КБ</p>}
          <p className="mt-4 text-xs leading-relaxed text-muted-foreground">Один файл за раз. Лимиты размера, страниц и текста проверяет сервер. PDF с изображениями может потребовать OCR.</p>
        </div>
        {error && <p role="alert" className="rounded-lg bg-rose-50 p-4 text-sm text-rose-800">{error}</p>}
        <div className="flex flex-wrap items-center justify-between gap-4"><p className="flex items-center gap-2 text-xs text-muted-foreground"><ShieldCheck className="size-4 text-primary" />Доступ только вашему аккаунту</p>
          <Button type="submit" disabled={!file || busy}><FileUp />{busy ? "Загрузка…" : "Загрузить"}</Button></div>
        <p role="status" className="text-xs text-muted-foreground">{busy ? "Не закрывайте страницу до окончания загрузки." : ""}</p>
      </form>
    </CardContent></Card>
  </main>;
}
