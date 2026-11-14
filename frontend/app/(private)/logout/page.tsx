"use client";

import { useState } from "react";
import { authRequest } from "../../auth-client";
import { Button } from "../../../components/ui/button";

export default function Logout() {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function logout() {
    setBusy(true);
    try {
      const response = await authRequest("logout");
      if (response.ok || response.status === 401) window.location.replace("/login");
      else { setError("Не удалось выйти. Повторите попытку."); setBusy(false); }
    } catch { setError("Сервис временно недоступен"); setBusy(false); }
  }
  return <main className="space-y-5"><h1 className="text-2xl font-semibold">Выход</h1>
    <Button disabled={busy} onClick={logout}>Выйти из аккаунта</Button>
    <p role="alert" className="text-sm text-rose-700">{error}</p></main>;
}
