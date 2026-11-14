export async function authRequest(path: string, data?: unknown): Promise<Response> {
  const csrfResponse = await fetch("/api/auth/csrf", { cache: "no-store" });
  if (!csrfResponse.ok) throw new Error("Сервис временно недоступен");
  const { csrf_token } = await csrfResponse.json();
  return fetch(`/api/auth/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf_token },
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: "no-store",
  });
}
