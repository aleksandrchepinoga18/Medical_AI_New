import { cache } from "react";

export const hasKnowledgeAccess = cache(async (token: string) => {
  try {
    const response = await fetch("http://backend:8000/knowledge/access", {
      headers: { Cookie: `medical_session=${encodeURIComponent(token)}` },
      cache: "no-store",
    });
    return response.status === 204;
  } catch {
    return false;
  }
});
