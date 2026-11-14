"use client";

import * as Dialog from "@radix-ui/react-dialog";
import type { ComponentProps } from "react";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";

export const Sheet = Dialog.Root;
export const SheetTrigger = Dialog.Trigger;
export const SheetClose = Dialog.Close;
export const SheetTitle = Dialog.Title;
export const SheetDescription = Dialog.Description;

export function SheetContent({ children, className, ...props }: ComponentProps<typeof Dialog.Content>) {
  return <Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-40 bg-slate-950/40 backdrop-blur-sm" />
    <Dialog.Content className={cn("fixed inset-y-0 left-0 z-50 w-72 bg-background p-6 shadow-xl", className)} {...props}>
      {children}
      <Dialog.Close aria-label="Закрыть меню" className="absolute right-4 top-4 rounded-md p-2 hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"><X className="size-4" /></Dialog.Close>
    </Dialog.Content>
  </Dialog.Portal>;
}
