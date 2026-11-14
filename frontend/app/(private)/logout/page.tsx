"use client";

import { useState } from "react";
import { authRequest } from "../../auth-client";

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
  return <main><h1>Выход</h1><button disabled={busy} onClick={logout}>Выйти из аккаунта</button>
    <p role="alert">{error}</p></main>;
}
