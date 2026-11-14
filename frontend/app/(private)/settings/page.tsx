import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export default async function Settings() {
  const token = (await cookies()).get("medical_session")?.value;
  if (!token) redirect("/login");
  const response = await fetch("http://backend:8000/auth/me", {
    headers: { Cookie: `medical_session=${encodeURIComponent(token)}` }, cache: "no-store",
  });
  if (response.status === 401) redirect("/login");
  if (!response.ok) throw new Error("Не удалось получить профиль");
  const profile: { id: string; email: string } = await response.json();
  return <main><h1>Настройки и профиль</h1><p>Email: {profile.email}</p><p>ID: {profile.id}</p></main>;
}
