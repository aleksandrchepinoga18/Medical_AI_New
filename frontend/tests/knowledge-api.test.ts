import { afterEach, expect, it, vi } from "vitest";
import { reviewPublish, sourceOriginalUrl, uploadSource, type Source, type SourceMetadata } from "../lib/knowledge-api";

const metadata: SourceMetadata = {
  title: "Synthetic", material_type: "synthetic", author: "Synthetic", publisher: "Synthetic",
  url: "https://example.org/guide", doi: null, pmid: null, publication_date: null, update_date: null,
  specialty: "Synthetic", language: "en", jurisdiction: "Synthetic",
};
const source: Source = {
  ...metadata, id: "synthetic-id",
  current_version: {
    id: "version-id", version: "1", review_revision: "review-id",
    format: "pdf", sha256: "0".repeat(64), status: "uploaded", chunk_count: 0, review_note: null,
  },
};
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

afterEach(() => vi.unstubAllGlobals());

it("sends the original as multipart and binds review to the current version and CSRF", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(ok({ csrf_token: "synthetic-csrf" }))
    .mockResolvedValueOnce(ok(source))
    .mockResolvedValueOnce(ok({ csrf_token: "synthetic-csrf" }))
    .mockResolvedValueOnce(ok(source));
  vi.stubGlobal("fetch", fetchMock);
  await uploadSource({ ...metadata, version: "2" }, new File(["%PDF-synthetic"], "source.pdf"), source.id);
  const [uploadUrl, upload] = fetchMock.mock.calls[1];
  expect(uploadUrl).toBe("/api/knowledge/sources/synthetic-id/version");
  expect(upload.method).toBe("PUT");
  expect(upload.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(upload.headers.has("Content-Type")).toBe(false);
  expect(upload.body.get("file").name).toBe("source.pdf");
  expect(JSON.parse(upload.body.get("metadata"))).toEqual({ ...metadata, version: "2" });

  await reviewPublish(source);
  const [reviewUrl, review] = fetchMock.mock.calls[3];
  expect(reviewUrl).toBe("/api/knowledge/sources/synthetic-id/review-publish");
  expect(review.method).toBe("POST");
  expect(review.headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(JSON.parse(review.body)).toEqual({
    version_id: "version-id", review_revision: "review-id", verified_sha256: source.current_version.sha256,
  });
  expect(sourceOriginalUrl(source)).toBe("/api/knowledge/sources/synthetic-id/versions/version-id/original");
});

it("translates extraction failures from the existing index API", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(ok({ csrf_token: "synthetic-csrf" }))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      detail: { message: "Medical source extraction failed", code: "NO_INDEXABLE_TEXT" },
    }), { status: 422 }));
  vi.stubGlobal("fetch", fetchMock);
  await expect(reviewPublish(source)).rejects.toMatchObject({
    status: 422, message: "Для индексации нужен PDF с текстовым слоем или DOCX с текстом.",
  });
});
