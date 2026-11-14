"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { authRequest } from "./auth-client";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader } from "../components/ui/card";
import { Input } from "../components/ui/input";

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
  return <main className="flex min-h-screen items-center justify-center p-6">
    <Card className="w-full max-w-md"><CardHeader>
      <p className="mb-4 text-sm font-semibold tracking-widest text-primary">MEDICAL AI</p>
      <h1 className="text-2xl font-semibold tracking-tight">{register ? "Регистрация" : "Вход"}</h1>
      <p className="mt-2 text-sm text-muted-foreground">Ваше личное пространство для документов.</p>
    </CardHeader><CardContent>
      <form onSubmit={submit} className="space-y-5">
        <div><label htmlFor="auth-email" className="mb-2 block text-sm font-medium">Email</label>
          <Input id="auth-email" name="email" type="email" autoComplete="username" required maxLength={254} /></div>
        <div><label htmlFor="auth-password" className="mb-2 block text-sm font-medium">Пароль</label>
          <Input id="auth-password" name="password" type="password" required minLength={12} maxLength={128}
            autoComplete={register ? "new-password" : "current-password"} /></div>
        <p className="text-xs text-muted-foreground">Пароль: от 12 до 128 символов.</p>
        <Button type="submit" disabled={busy} className="w-full">{busy ? "Подождите…" : register ? "Создать аккаунт" : "Войти"}</Button>
        <p role="alert" className="text-sm text-rose-700">{error}</p>
      </form>
      <Link className="mt-5 block text-center text-sm font-medium text-primary hover:underline" href={register ? "/login" : "/register"}>{register ? "Уже есть аккаунт" : "Регистрация"}</Link>
    </CardContent></Card>
  </main>;
}
