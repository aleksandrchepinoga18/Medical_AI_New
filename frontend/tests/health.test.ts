import { describe, expect, it } from "vitest";

import { GET } from "../app/health/live/route";

describe("liveness endpoint", () => {
  it("returns the public health contract without credentials", async () => {
    const response = GET();
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("application/json");
    expect(await response.json()).toEqual({ status: "ok" });
  });
});
