"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { authRequest } from "./auth-client";

export default function AuthForm({ register = false }: { register?: boolean }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      const response = await authRequest(register ? "register" : "login", {
        email: data.get("email"), password: data.get("password"),
      });
      if (!response.ok) {
        setError(response.status === 429 ? "Слишком много попыток. Повторите через 15 минут."
          : "Не удалось выполнить вход или регистрацию. Проверьте данные.");
        return;
      }
      router.replace("/settings");
      router.refresh();
    } catch { setError("Сервис временно недоступен. Повторите позже."); }
    finally { setBusy(false); }
  }
  return <main><h1>{register ? "Регистрация" : "Вход"}</h1>
    <form onSubmit={submit}>
      <label>Email <input name="email" type="email" autoComplete="username" required maxLength={254} /></label>
      <label>Пароль <input name="password" type="password" required minLength={12} maxLength={128}
        autoComplete={register ? "new-password" : "current-password"} /></label>
      <p>Пароль: от 12 до 128 символов.</p>
      <button disabled={busy}>{busy ? "Подождите…" : register ? "Создать аккаунт" : "Войти"}</button>
      <p role="alert">{error}</p>
    </form>
    <Link href={register ? "/login" : "/register"}>{register ? "Уже есть аккаунт" : "Регистрация"}</Link>
  </main>;
}
