import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

export function Card({ className, ...props }: ComponentProps<"section">) {
  return <section className={cn("rounded-2xl border border-border bg-card text-card-foreground shadow-sm", className)} {...props} />;
}
export function CardHeader({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("p-6 pb-4", className)} {...props} />;
}
export function CardContent({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("p-6 pt-0", className)} {...props} />;
}
