import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import Link from "next/link";
import type { ReactNode } from "react";

export const dynamic = "force-dynamic";

export default async function PrivateLayout({ children }: { children: ReactNode }) {
  const jar = await cookies();
  const token = jar.get("medical_session")?.value;
  if (!token) redirect("/login");
  const response = await fetch("http://backend:8000/auth/me", {
    headers: { Cookie: `medical_session=${encodeURIComponent(token)}` }, cache: "no-store",
  });
  if (response.status === 401) redirect("/login");
  if (!response.ok) throw new Error("Сервис авторизации временно недоступен");
  return <><nav aria-label="Основная навигация">
    <Link href="/chat">Чат</Link> <Link href="/documents">Документы</Link>{" "}
    <Link href="/analyses">Анализы</Link> <Link href="/settings">Настройки</Link>{" "}
    <Link href="/logout">Выход</Link>
  </nav>{children}</>;
}
