import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn("flex w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 file:mr-4 file:rounded-md file:border-0 file:bg-muted file:px-3 file:py-2 file:text-sm", className)} {...props} />;
}
