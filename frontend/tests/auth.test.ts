import { afterEach, expect, it, vi } from "vitest";
import { authRequest } from "../app/auth-client";

afterEach(() => vi.unstubAllGlobals());

it("obtains CSRF and sends it with credentials to the same origin", async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json({ csrf_token: "token" }))
    .mockResolvedValueOnce(Response.json({ id: "user" }));
  vi.stubGlobal("fetch", fetch);
  await authRequest("login", { email: "test@example.org", password: "synthetic" });
  expect(fetch.mock.calls[0][0]).toBe("/api/auth/csrf");
  expect(fetch.mock.calls[1][0]).toBe("/api/auth/login");
  expect(fetch.mock.calls[1][1].headers["X-CSRF-Token"]).toBe("token");
});

it("does not submit credentials when CSRF acquisition fails", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
  vi.stubGlobal("fetch", fetch);
  await expect(authRequest("login", {})).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
});
