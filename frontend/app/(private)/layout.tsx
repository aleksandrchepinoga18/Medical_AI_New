import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { AppShell } from "../../components/app-shell";
import { hasKnowledgeAccess } from "../../lib/knowledge-access";

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
  return <AppShell knowledgeAdmin={await hasKnowledgeAccess(token)}>{children}</AppShell>;
}
