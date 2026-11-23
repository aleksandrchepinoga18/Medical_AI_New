"use client";

import { useState, type FormEvent } from "react";
import {
  KnowledgeApiError, searchPubMed, type PubMedCitation, type PubMedHit,
} from "../../lib/knowledge-api";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader } from "../ui/card";
import { Input } from "../ui/input";

const accessLabels: Record<PubMedHit["access"], string> = {
  free_link_reported: "PubMed указывает ссылку для бесплатного чтения.",
  link_reported: "PubMed указывает ссылку. Условия чтения уточняются у поставщика.",
  unknown: "Условия чтения неизвестны.",
};

export function PubMedBibliography({ citation }: { citation: PubMedCitation }) {
  return <dl className="grid gap-2 break-words sm:grid-cols-2">
    <div className="sm:col-span-2">
      <dt className="font-medium">Авторы</dt><dd>{citation.authors.join(", ") || "Не указаны"}</dd>
    </div>
    <div><dt className="font-medium">Журнал или издание</dt><dd>{citation.journal ?? "Не указано"}</dd></div>
    <div><dt className="font-medium">Год публикации</dt><dd>{citation.year ?? "Не указан"}</dd></div>
    <div><dt className="font-medium">PMID</dt><dd>{citation.pmid}</dd></div>
    {citation.doi && <div><dt className="font-medium">DOI</dt><dd>{citation.doi}</dd></div>}
    {citation.pmcid && <div><dt className="font-medium">PMCID</dt><dd>{citation.pmcid}</dd></div>}
  </dl>;
}

export function PubMedPanel({ saving, savedPmids, save, onAccessLost }: {
  saving: boolean;
  savedPmids: ReadonlySet<string>;
  save: (pmid: string) => void;
  onAccessLost: (error: KnowledgeApiError) => void;
}) {
  const [hits, setHits] = useState<PubMedHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (searching || saving) return;
    const form = new FormData(event.currentTarget);
    const query = String(form.get("query") ?? "").trim();
    const date_from = String(form.get("date_from") ?? "") || null;
    const date_to = String(form.get("date_to") ?? "") || null;
    if (!query || (date_from && date_to && date_from > date_to)) {
      setError(query ? "Дата начала должна предшествовать дате окончания или совпадать с ней."
        : "Введите поисковый запрос.");
      return;
    }
    setSearching(true);
    setHits(null);
    setError("");
    try {
      const result = await searchPubMed({ query, limit: Number(form.get("limit")), date_from, date_to });
      setHits(result.items);
    } catch (failure) {
      if (failure instanceof KnowledgeApiError && [401, 403].includes(failure.status)) {
        onAccessLost(failure);
      } else {
        setError(failure instanceof Error ? failure.message : "Поиск PubMed не завершён.");
      }
    } finally {
      setSearching(false);
    }
  }
  return <div className="space-y-4">
    <Card><CardContent className="pt-6">
      <form onSubmit={search}>
        <fieldset disabled={searching || saving} className="grid gap-4 sm:grid-cols-3">
          <label className="space-y-1 text-sm sm:col-span-3"><span>Медицинский запрос, PMID или DOI</span>
            <Input name="query" required maxLength={500} aria-describedby="pubmed-query-note" />
          </label>
          <p id="pubmed-query-note" className="text-sm text-muted-foreground sm:col-span-3">
            Запрос отправляется в NCBI. Используйте общие медицинские термины и идентификаторы публикаций.
          </p>
          <label className="space-y-1 text-sm"><span>Дата публикации с</span>
            <Input name="date_from" type="date" max="9999-12-31" />
          </label>
          <label className="space-y-1 text-sm"><span>Дата публикации по</span>
            <Input name="date_to" type="date" max="9999-12-31" />
          </label>
          <label className="space-y-1 text-sm"><span>Количество результатов</span>
            <Input name="limit" type="number" defaultValue={10} min={1} max={25} required />
          </label>
          <div className="sm:col-span-3">
            <Button type="submit">{searching ? "Поиск…" : "Найти в PubMed"}</Button>
          </div>
        </fieldset>
      </form>
    </CardContent></Card>
    {error && <p role="alert" className="text-sm text-rose-800">{error}</p>}
    {searching && <p role="status" className="text-sm text-muted-foreground">Запрос к PubMed выполняется…</p>}
    {hits?.length === 0 && <p role="status">Публикации не найдены.</p>}
    {hits?.map(hit => <Card key={hit.pmid}>
      <CardHeader><h2 className="break-words font-semibold">{hit.title}</h2></CardHeader>
      <CardContent className="space-y-4 text-sm">
        <PubMedBibliography citation={hit} />
        {hit.abstract
          ? <details><summary className="cursor-pointer font-medium">Аннотация доступна</summary>
            <p className="mt-2 whitespace-pre-wrap break-words">{hit.abstract}</p></details>
          : <p>Аннотация отсутствует в записи PubMed.</p>}
        <div className="space-y-1">
          <p>{hit.full_text_url
            ? <a className="text-primary underline" href={hit.full_text_url} target="_blank" rel="noreferrer">
              {hit.pmcid ? "Полный текст в PubMed Central" : "Ссылки на полный текст в PubMed"}
            </a>
            : "Ссылка на полный текст в PubMed не подтверждена."}</p>
          <p>{accessLabels[hit.access]} Перед копированием проверьте лицензию у поставщика.</p>
        </div>
        <div className="flex flex-wrap items-center gap-4">
          <a className="text-primary underline" href={hit.url} target="_blank" rel="noreferrer">Запись PubMed</a>
          {hit.publisher_url && <a className="text-primary underline" href={hit.publisher_url}
            target="_blank" rel="noreferrer">Страница публикации по DOI</a>}
          <Button size="sm" disabled={saving || savedPmids.has(hit.pmid)} onClick={() => save(hit.pmid)}>
            {savedPmids.has(hit.pmid) ? "Уже в реестре" : "Сохранить в реестр"}
          </Button>
        </div>
      </CardContent>
    </Card>)}
  </div>;
}
