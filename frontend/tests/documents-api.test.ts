import { afterEach, expect, it, vi } from "vitest";
import { DocumentApiError, canRetry, listDocuments, retryDocument, uploadDocument } from "../lib/documents-api";

afterEach(() => vi.unstubAllGlobals());

it("uploads multipart with CSRF and a stable idempotency key without overriding the boundary", async () => {
  const fetch = vi.fn().mockResolvedValueOnce(Response.json({ csrf_token: "synthetic-token" }))
    .mockResolvedValueOnce(Response.json({ id: "synthetic-id" }, { status: 202 }));
  vi.stubGlobal("fetch", fetch);
  const file = new File(["%PDF-synthetic"], "synthetic.pdf");
  await uploadDocument(file, "stable-key");
  const options = fetch.mock.calls[1][1];
  expect(options.body).toBeInstanceOf(FormData);
  expect(options.headers).toEqual({ "X-CSRF-Token": "synthetic-token", "Idempotency-Key": "stable-key" });
  expect(options.headers["Content-Type"]).toBeUndefined();
});

it("does not retry unauthorized requests or expose server error bodies", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response("private details", { status: 401 }));
  vi.stubGlobal("fetch", fetch);
  await expect(listDocuments(0)).rejects.toBeInstanceOf(DocumentApiError);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("does not post a retry when CSRF fails", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 403 }));
  vi.stubGlobal("fetch", fetch);
  await expect(retryDocument("synthetic-id")).rejects.toMatchObject({ status: 403 });
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("never offers processing retry for an incomplete original", () => {
  expect(canRetry({ status: "failed", error_code: "UPLOAD_INCOMPLETE" } as Parameters<typeof canRetry>[0])).toBe(false);
});
