import { previewOriginal, type ExtractionReport, type Measurement } from "../../lib/documents-api";

const confirmationNames: Record<Measurement["confirmation"], string> = {
  confirmed: "Подтверждено", unverified: "Не проверено", ambiguous: "Неоднозначно",
};

export function ExtractedMeasurements({ id, report }: { id: string; report: ExtractionReport }) {
  const provider = report.provider === "pymupdf_tables" ? "PyMuPDF Tables" : "GigaChat Ultra";
  return <section className="space-y-2">
    <h3 className="font-medium">{provider} <span className="text-sm text-muted-foreground">{report.provider_version}</span></h3>
    {report.truncated && <p className="text-sm text-muted-foreground">Извлечение частичное.</p>}
    {report.measurements.length ? <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead><tr className="border-b">
          {["Показатель", "Результат", "Единица", "Референс", "Дата", "Статус", "Источник"].map(label =>
            <th key={label} scope="col" className="p-2 font-medium">{label}</th>)}
        </tr></thead>
        <tbody>{report.measurements.map((row, index) => <tr key={index} className="border-b">
          <th scope="row" className="p-2 font-medium">{row.name}</th>
          <td className="p-2">{row.value}</td><td className="p-2">{row.unit ?? "—"}</td>
          <td className="p-2">{row.reference ?? "—"}</td><td className="p-2">{row.date ?? "—"}</td>
          <td className="p-2">{confirmationNames[row.confirmation]}</td>
          <td className="p-2">{row.confirmation === "confirmed" && row.source
            ? <a className="underline" href={previewOriginal(id, row.source.page)} target="_blank" rel="noopener noreferrer">
                Стр. {row.source.page}
              </a>
            : row.reported_page ? `Стр. ${row.reported_page} (по модели)` : "—"}</td>
        </tr>)}</tbody>
      </table>
    </div> : <p className="text-sm text-muted-foreground">Измерения не найдены.</p>}
  </section>;
}
