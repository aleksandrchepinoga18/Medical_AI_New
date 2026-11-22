import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { KnowledgeAdmin } from "../../../components/knowledge/admin";
import { hasKnowledgeAccess } from "../../../lib/knowledge-access";

export default async function KnowledgePage() {
  const token = (await cookies()).get("medical_session")?.value;
  if (!token) redirect("/login");
  if (!await hasKnowledgeAccess(token)) {
    return <main className="space-y-4">
      <h1 className="text-3xl font-semibold">Медицинская база знаний</h1>
      <p role="alert">Административный раздел недоступен для этой сессии.</p>
    </main>;
  }
  return <KnowledgeAdmin />;
}
