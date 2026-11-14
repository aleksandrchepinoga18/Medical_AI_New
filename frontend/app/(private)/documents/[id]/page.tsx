import { DocumentDetails } from "../../../../components/documents/document-details";

export default async function DocumentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <DocumentDetails id={id} />;
}
