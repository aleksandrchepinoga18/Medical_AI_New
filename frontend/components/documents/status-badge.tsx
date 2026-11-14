import { Badge } from "../ui/badge";
import { activeStatuses, statusNames } from "../../lib/documents-api";
import { cn } from "../../lib/utils";

export function StatusBadge({ status }: { status: string }) {
  return <Badge className={cn("gap-1.5", status === "ready" && "bg-emerald-50 text-emerald-700",
    status === "failed" && "bg-rose-50 text-rose-700", status === "needs_ocr" && "bg-amber-50 text-amber-800",
    activeStatuses.has(status) && "bg-blue-50 text-blue-700")}>
    <span className={cn("size-1.5 rounded-full bg-current", activeStatuses.has(status) && "animate-pulse")} />
    {statusNames[status] ?? "Неизвестный статус"}
  </Badge>;
}
