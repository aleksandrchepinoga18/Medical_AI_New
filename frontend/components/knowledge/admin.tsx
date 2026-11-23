"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader } from "../ui/card";
import { Input } from "../ui/input";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "../ui/sheet";
import {
  editSource, KnowledgeApiError, listSources, reviewPublish, savePubMed, searchKnowledge,
  setSourceStatus, sourceOriginalUrl, uploadSource, type CitationSource, type Locator,
  type RegistrySource, type SearchHit, type Source, type SourceMetadata, type SourceStatus,
} from "../../lib/knowledge-api";
import { PubMedBibliography, PubMedPanel } from "./pubmed";

const statuses: Record<SourceStatus, string> = {
  uploaded: "Загружен", verified: "Проверен", published: "Опубликован",
  outdated: "Устарел", rejected: "Отклонён",
};
const fields = [
  ["title", "Название", 500], ["material_type", "Тип материала", 100],
  ["author", "Автор или организация", 300], ["publisher", "Издатель", 300],
  ["url", "Официальный HTTPS URL", 2048], ["specialty", "Направление", 150],
  ["language", "Код языка", 35], ["jurisdiction", "Страна или юрисдикция", 150],
] as const;
const optional = [
  ["doi", "DOI", "text", 255], ["pmid", "PMID", "text", 20],
  ["publication_date", "Дата публикации", "date", undefined],
  ["update_date", "Дата обновления", "date", undefined],
] as const;
type Editor = { kind: "create" } | { kind: "metadata" | "version"; source: RegistrySource };
type Mutation = (work: () => Promise<RegistrySource>, message: string) => void;

function officialLink(url: string) {
  return /^https:\/\//i.test(url)
    ? <a className="break-all text-primary underline" href={url} target="_blank" rel="noreferrer">{url}</a>
    : <span className="break-all">{url}</span>;
}

function SourceEditor({ editor, busy, mutate, cancel }: {
  editor: Editor; busy: boolean; mutate: Mutation; cancel: () => void;
}) {
  const source = "source" in editor ? editor.source : undefined;
  const [file, setFile] = useState<File | null>(null);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value = (key: string) => String(form.get(key) ?? "").trim();
    const metadata = {
      ...Object.fromEntries(fields.map(([key]) => [key, value(key)])),
      ...Object.fromEntries(optional.map(([key]) => [key, value(key) || null])),
    } as SourceMetadata;
    if (editor.kind === "metadata") {
      const id = editor.source.id;
      mutate(() => editSource(id, metadata), "Метаданные сохранены.");
      return;
    }
    mutate(async () => {
      if (!file?.size || !/\.(pdf|docx)$/i.test(file.name)) {
        throw new Error("Выберите PDF или DOCX.");
      }
      const rights = source?.pubmed_citation
        ? { rights_confirmed: form.has("rights_confirmed") }
        : {};
      return uploadSource({ ...metadata, version: value("version"), ...rights }, file, source?.id);
    }, "Оригинал загружен. Материал ожидает проверки.");
  }
  return <Card><CardHeader>
    <h2 className="font-semibold">{editor.kind === "create" ? "Новый источник"
      : editor.kind === "metadata" ? "Метаданные источника"
      : source?.current_version ? "Новая версия" : "Загрузка оригинала"}</h2>
  </CardHeader><CardContent>
    <form onSubmit={submit}>
      <fieldset disabled={busy} className="grid gap-4 sm:grid-cols-2">
        {fields.map(([key, label, max]) => <label key={key} className="space-y-1 text-sm">
          <span>{label}</span>
          <Input name={key} required maxLength={max} type={key === "url" ? "url" : "text"}
            readOnly={Boolean(source?.pubmed_citation) && key === "url"}
            defaultValue={source?.[key]} />
        </label>)}
        {optional.map(([key, label, type, max]) => <label key={key} className="space-y-1 text-sm">
          <span>{label}</span>
          <Input name={key} type={type} maxLength={max} defaultValue={source?.[key] ?? ""}
            readOnly={Boolean(source?.pubmed_citation) && (key === "doi" || key === "pmid")} />
        </label>)}
        {editor.kind !== "metadata" && <>
          <label className="space-y-1 text-sm"><span>Версия</span>
            <Input name="version" required maxLength={100} />
          </label>
          <label className="space-y-1 text-sm"><span>Файл PDF или DOCX</span>
            <Input name="file" type="file" required accept=".pdf,.docx"
              onChange={event => setFile(event.target.files?.[0] ?? null)} />
          </label>
          {source?.pubmed_citation && <label className="flex items-start gap-2 text-sm sm:col-span-2">
            <input type="checkbox" className="mt-0.5" name="rights_confirmed" required />
            Я проверил лицензию или получил разрешение на загрузку и использование полного текста.
          </label>}
        </>}
        {source?.current_version && <p className="text-sm text-muted-foreground sm:col-span-2">
          Изменение метаданных или версии потребует повторной проверки и публикации.
        </p>}
        <div className="flex gap-2 sm:col-span-2">
          <Button type="submit">{editor.kind === "metadata" ? "Сохранить метаданные" : "Загрузить"}</Button>
          <Button type="button" variant="outline" onClick={cancel}>Отмена</Button>
        </div>
      </fieldset>
    </form>
  </CardContent></Card>;
}

function DownloadOriginal({ source }: { source: Source }) {
  return <Button asChild variant="outline" size="sm">
    <a href={sourceOriginalUrl(source)} target="_blank" rel="noreferrer">Скачать оригинал</a>
  </Button>;
}

function CitationCard({ source, busy, edit }: {
  source: CitationSource; busy: boolean; edit: (editor: Editor) => void;
}) {
  const title = source.pubmed_citation.title;
  return <Card>
    <CardHeader>
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="break-words font-semibold">{source.title === title.slice(0, 500) ? title : source.title}</h3>
        <Badge>Библиография · Без оригинала</Badge>
      </div>
    </CardHeader>
    <CardContent className="space-y-4 text-sm">
      <PubMedBibliography citation={source.pubmed_citation} />
      {officialLink(source.url)}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={busy} onClick={() => edit({ kind: "metadata", source })}>
          Изменить метаданные
        </Button>
        <Button size="sm" disabled={busy} onClick={() => edit({ kind: "version", source })}>
          Загрузить оригинал
        </Button>
      </div>
    </CardContent>
  </Card>;
}

function SourceCard({ source, busy, processing, failure, selected, select, confirm, mutate, edit }: {
  source: Source; busy: boolean; processing: boolean; failure?: string; selected: boolean;
  select: () => void; confirm: () => void; mutate: Mutation; edit: (editor: Editor) => void;
}) {
  const version = source.current_version;
  const published = version.status === "published";
  const archived = version.status === "outdated" || version.status === "rejected";
  const status = processing ? "Обрабатывается" : failure ? "Ошибка"
    : published ? "Доступен для поиска" : archived ? "Архивный" : "Загружен";
  return <Card>
    <CardHeader>
      <div className="flex flex-wrap items-center gap-3">
        {!published && <input type="checkbox" className="size-4" checked={selected} disabled={busy}
          aria-label={`Выбрать ${source.title}`} onChange={select} />}
        <h3 className="font-semibold">{source.title}</h3><Badge>{status}</Badge>
      </div>
      <p className="text-sm text-muted-foreground">
        {source.publisher} · Версия {version.version} · {version.format.toUpperCase()}
      </p>
    </CardHeader>
    <CardContent className="space-y-4 text-sm">
      {officialLink(source.url)}
      {failure && <p role="alert" className="rounded-lg bg-rose-50 p-3 text-rose-800">{failure}</p>}
      <div className="flex flex-wrap gap-2">
        <DownloadOriginal source={source} />
        {!published && <Button size="sm" disabled={busy} onClick={confirm}>
          {failure ? "Повторить обработку" : "Подтвердить и опубликовать"}
        </Button>}
      </div>
      <details><summary className="cursor-pointer font-medium">Дополнительно</summary>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => edit({ kind: "metadata", source })}>
            Изменить метаданные
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => edit({ kind: "version", source })}>
            Загрузить новую версию
          </Button>
          {published && <Button size="sm" variant="outline" disabled={busy}
            onClick={() => mutate(() => setSourceStatus(source, "outdated"), "Источник снят с публикации.")}>
            Снять с публикации
          </Button>}
        </div>
        <dl className="mt-2 grid gap-2 sm:grid-cols-2">
          {[...fields, ...optional].filter(([key]) => source[key]).map(([key, label]) =>
            <div key={key}><dt className="font-medium">{label}</dt><dd className="break-words">{source[key]}</dd></div>)}
        </dl>
        <p className="mt-2">Статус реестра · {statuses[version.status]} · Чанков {version.chunk_count}</p>
        <p className="mt-2 break-all">SHA-256 <code>{version.sha256}</code></p>
        {version.review_note && <p>Проверка · {version.review_note}</p>}
        {source.pubmed_citation && <div className="mt-4">
          <PubMedBibliography citation={source.pubmed_citation} />
        </div>}
      </details>
    </CardContent>
  </Card>;
}

const revisionKey = (source: Source) => source.current_version.review_revision;

function ReviewSheet({ sources, cancel, publish }: {
  sources: Source[]; cancel: () => void; publish: () => void;
}) {
  return <Sheet open={sources.length > 0} onOpenChange={open => { if (!open) cancel(); }}>
    <SheetContent className="w-full max-w-md overflow-y-auto">
      <SheetTitle className="pr-10 text-xl font-semibold">Проверка перед публикацией</SheetTitle>
      <SheetDescription className="mt-2 text-sm text-muted-foreground">
        Подтвердите, что проверили оригиналы, метаданные и права на использование полного текста.
        После подтверждения система индексирует каждый источник и публикует его при успехе.
      </SheetDescription>
      <ul className="my-6 space-y-3">
        {sources.map(source => <li key={source.id} className="flex flex-wrap items-center justify-between gap-2 border-b py-2">
          <span className="text-sm">{source.title} · Версия {source.current_version.version}</span>
          <DownloadOriginal source={source} />
        </li>)}
      </ul>
      <div className="flex flex-wrap gap-2">
        <Button onClick={publish}>Подтвердить и опубликовать</Button>
        <Button variant="outline" onClick={cancel}>Отмена</Button>
      </div>
    </SheetContent>
  </Sheet>;
}

function locatorLabel(locator: Locator): string {
  if (locator.kind === "page") return `Страница ${locator.index}`;
  if (locator.kind === "paragraph") return `Абзац ${locator.index}`;
  if (locator.kind.startsWith("table:")) return `Таблица ${locator.kind.slice(6)}, строка ${locator.index}`;
  return `${locator.kind} ${locator.index}`;
}

function SearchPanel({ onAccessLost }: { onAccessLost: (failure: KnowledgeApiError) => void }) {
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    const query = String(form.get("query") ?? "").trim();
    if (!query) { setError("Введите поисковый запрос."); return; }
    setBusy(true);
    setHits(null);
    setError("");
    try { setHits((await searchKnowledge(query, Number(form.get("k")))).items); }
    catch (failure) {
      if (failure instanceof KnowledgeApiError && [401, 403].includes(failure.status)) onAccessLost(failure);
      else setError(failure instanceof Error ? failure.message : "Поиск не завершён.");
    }
    finally { setBusy(false); }
  }
  return <Card><CardHeader><h2 className="font-semibold">Проверка медицинского поиска</h2></CardHeader>
    <CardContent className="space-y-4">
      <form className="flex flex-wrap items-end gap-3" onSubmit={search}>
        <label className="min-w-48 flex-1 space-y-1 text-sm"><span>Запрос</span>
          <Input name="query" required maxLength={2000} /></label>
        <label className="w-28 space-y-1 text-sm"><span>Результатов</span>
          <Input name="k" type="number" min={1} max={50} defaultValue={5} required /></label>
        <Button type="submit" disabled={busy}>{busy ? "Поиск…" : "Найти в базе"}</Button>
      </form>
      {error && <p role="alert" className="text-sm text-rose-800">{error}</p>}
      {hits?.length === 0 && <p role="status">Опубликованные фрагменты не найдены.</p>}
      {hits?.map(hit => <article key={hit.chunk_id} className="space-y-2 rounded-xl border border-border p-4 text-sm">
        <h3 className="font-semibold">{hit.source.title} · Версия {hit.source.version}</h3>
        <p className="text-muted-foreground">
          {hit.locators.length ? hit.locators.map(locatorLabel).join(" · ") : "Локатор отсутствует"}
          {typeof hit.score === "number" && Number.isFinite(hit.score) && ` · RRF ${hit.score.toFixed(4)}`}
        </p>
        {officialLink(hit.source.url)}
        <p className="whitespace-pre-wrap break-words">{hit.text}</p>
      </article>)}
    </CardContent>
  </Card>;
}

export function KnowledgeAdmin() {
  const [sources, setSources] = useState<RegistrySource[]>([]);
  const [tab, setTab] = useState<"registry" | "pubmed">("registry");
  const [after, setAfter] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [accessLost, setAccessLost] = useState(false);
  const [registryReady, setRegistryReady] = useState(false);
  const [searchRevision, setSearchRevision] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [review, setReview] = useState<Source[]>([]);
  const [processing, setProcessing] = useState<string | null>(null);
  const [failures, setFailures] = useState<Record<string, string>>({});
  const selectedSources = sources.filter((source): source is Source => source.current_version !== null
    && source.current_version.status !== "published" && selected.has(revisionKey(source)));
  const savedPmids = new Set(sources.flatMap(source => source.pmid ? [source.pmid] : []));
  const showError = useCallback((failure: unknown) => {
    setError(failure instanceof Error ? failure.message : "Операция не завершена.");
    if (failure instanceof KnowledgeApiError && [401, 403].includes(failure.status)) setAccessLost(true);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    listSources(undefined, controller.signal).then(page => {
      if (!controller.signal.aborted) {
        setSources(page.items);
        setAfter(page.next_after);
        setRegistryReady(true);
      }
    }).catch(failure => {
      if (!controller.signal.aborted) showError(failure);
    }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [showError]);

  async function perform(work: () => Promise<void>) {
    if (busy || accessLost) return;
    setBusy(true);
    setError("");
    setMessage("");
    try { await work(); }
    catch (failure) { showError(failure); }
    finally { setBusy(false); }
  }
  function load(more: boolean) {
    return perform(async () => {
      const page = await listSources(more ? after ?? undefined : undefined);
      setSources(current => more
        ? [...new Map([...current, ...page.items].map(item => [item.id, item])).values()]
        : page.items);
      setAfter(page.next_after);
      setRegistryReady(true);
      if (!more) {
        setEditor(null);
        setSelected(new Set());
      }
    });
  }
  function saveSource(source: RegistrySource) {
    setSources(current => current.some(item => item.id === source.id)
      ? current.map(item => item.id === source.id ? source : item) : [source, ...current]);
  }
  function mutate(work: () => Promise<RegistrySource>, done: string) {
    return perform(async () => {
      saveSource(await work());
      setEditor(null);
      setMessage(done);
      setSearchRevision(revision => revision + 1);
    });
  }
  function select(source: Source, checked: boolean) {
    const key = revisionKey(source);
    setSelected(current => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  }
  function confirmReview() {
    const pending = review;
    setReview([]);
    return perform(async () => {
      let published = 0;
      let failed = 0;
      for (const source of pending) {
        const key = revisionKey(source);
        setProcessing(key);
        setFailures(current => {
          const next = { ...current };
          delete next[key];
          return next;
        });
        try {
          saveSource(await reviewPublish(source));
          select(source, false);
          published++;
        } catch (failure) {
          if (failure instanceof KnowledgeApiError && [401, 403].includes(failure.status)) throw failure;
          const reason = failure instanceof Error ? failure.message : "Обработка не завершена.";
          setFailures(current => ({ ...current, [key]: reason }));
          failed++;
        } finally {
          setProcessing(null);
        }
      }
      if (published) setSearchRevision(revision => revision + 1);
      setMessage(`Опубликовано: ${published}. С ошибкой: ${failed}.`);
    });
  }
  return <main className="space-y-6">
    <h1 className="text-3xl font-semibold tracking-tight">Медицинская база знаний</h1>
    {error && <p role="alert" className="rounded-lg bg-rose-50 p-4 text-sm text-rose-800">{error}</p>}
    {message && <p role="status" className="text-sm text-emerald-700">{message}</p>}
    {busy && <p role="status" className="text-sm text-muted-foreground">Операция выполняется…</p>}
    {!accessLost && <>
      <div role="tablist" aria-label="Медицинская база знаний" className="flex flex-wrap gap-2">
        {(["registry", "pubmed"] as const).map((name, index, tabs) =>
          <Button key={name} id={`knowledge-tab-${name}`} type="button" role="tab"
            aria-selected={tab === name} aria-controls={`knowledge-panel-${name}`}
            tabIndex={tab === name ? 0 : -1} variant={tab === name ? "default" : "outline"}
            onClick={() => setTab(name)} onKeyDown={event => {
              const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[1]
                : ["ArrowLeft", "ArrowRight"].includes(event.key) ? tabs[1 - index] : undefined;
              if (next) {
                event.preventDefault();
                setTab(next);
                document.getElementById(`knowledge-tab-${next}`)?.focus();
              }
            }}>
            {name === "registry" ? "Реестр источников" : "Поиск PubMed"}
          </Button>)}
      </div>
      <div role="tabpanel" id="knowledge-panel-registry" aria-labelledby="knowledge-tab-registry"
        hidden={tab !== "registry"} className="space-y-6">
        <section aria-label="Реестр источников" className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Button className="ml-auto" variant="outline" disabled={busy} onClick={() => load(false)}>
              Обновить реестр
            </Button>
            <Button disabled={busy} onClick={() => setEditor({ kind: "create" })}>Добавить источник</Button>
          </div>
          {selectedSources.length > 0 && <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm text-muted-foreground">Выбрано: {selectedSources.length}</span>
            <Button disabled={busy} onClick={() => setReview(selectedSources)}>
              Подтвердить и опубликовать выбранные
            </Button>
            <Button variant="ghost" disabled={busy} onClick={() => setSelected(new Set())}>Снять выделение</Button>
          </div>}
          {editor && <SourceEditor key={editor.kind + ("source" in editor ? editor.source.id : "")}
            editor={editor} busy={busy} mutate={mutate} cancel={() => setEditor(null)} />}
          {!sources.length && !busy && !error && <p className="text-muted-foreground">Источников пока нет.</p>}
          {sources.map(source => source.current_version === null
            ? <CitationCard key={source.id} source={source} busy={busy} edit={setEditor} />
            : <SourceCard key={source.current_version.id} source={source}
              busy={busy} mutate={mutate} edit={setEditor}
              processing={processing === revisionKey(source)}
              failure={source.current_version.status === "published" ? undefined : failures[revisionKey(source)]}
              selected={selected.has(revisionKey(source))}
              select={() => select(source, !selected.has(revisionKey(source)))}
              confirm={() => setReview([source])} />)}
          {after && <Button variant="outline" disabled={busy} onClick={() => load(true)}>Показать ещё</Button>}
        </section>
        <ReviewSheet sources={review} cancel={() => setReview([])} publish={confirmReview} />
        {registryReady && <SearchPanel key={searchRevision} onAccessLost={showError} />}
      </div>
      <div role="tabpanel" id="knowledge-panel-pubmed" aria-labelledby="knowledge-tab-pubmed"
        hidden={tab !== "pubmed"}>
        <PubMedPanel saving={busy} savedPmids={savedPmids} onAccessLost={showError}
          save={pmid => mutate(() => savePubMed(pmid), "Библиография сохранена в реестре.")} />
      </div>
    </>}
  </main>;
}
