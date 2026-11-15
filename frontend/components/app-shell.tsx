"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { Activity, Files, FlaskConical, LogOut, Menu, MessageSquare, Search, Settings, ShieldCheck } from "lucide-react";
import { Button } from "./ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from "./ui/sheet";
import { cn } from "../lib/utils";

const links = [
  { href: "/chat", title: "Чат", icon: MessageSquare },
  { href: "/documents", title: "Документы", icon: Files },
  { href: "/search", title: "Поиск", icon: Search },
  { href: "/analyses", title: "Анализы", icon: FlaskConical },
  { href: "/settings", title: "Настройки", icon: Settings },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  function navigation() {
    return <nav aria-label="Основная навигация" className="space-y-1.5">
      {links.map(({ href, title, icon: Icon }) => <Link key={href} href={href}
        onClick={() => setOpen(false)} aria-current={pathname.startsWith(href) ? "page" : undefined}
        className={cn("flex items-center gap-3 rounded-xl px-4 py-3 text-sm font-medium transition-colors",
          pathname.startsWith(href) ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground")}>
        <Icon className="size-5" />{title}</Link>)}
    </nav>;
  }
  const brand = <Link href="/documents" className="flex items-center gap-3 font-semibold tracking-tight">
    <span className="rounded-xl bg-primary p-2 text-white"><Activity className="size-6" /></span>
    <span className="text-xl">Medical<span className="text-primary"> AI</span></span></Link>;
  return <div className="min-h-screen">
    <aside className="fixed inset-y-0 left-0 hidden w-64 flex-col border-r border-border bg-card p-6 md:flex">
      {brand}<p className="mb-8 mt-2 pl-14 text-xs text-muted-foreground">Личное пространство</p>
      {navigation()}
      <div className="mt-auto space-y-5 pt-8">
        <div className="rounded-xl bg-muted/70 p-4 text-xs leading-relaxed text-muted-foreground">
          <ShieldCheck className="mb-2 size-5 text-primary" />Документы доступны только вашему аккаунту.
        </div>
        <Button asChild variant="ghost" className="w-full justify-start"><Link href="/logout"><LogOut />Выйти</Link></Button>
      </div>
    </aside>
    <div className="md:pl-64">
      <header className="flex h-20 items-center justify-between border-b border-border bg-card/90 px-5 sm:px-8">
        <div className="flex items-center gap-3">
          <Sheet open={open} onOpenChange={setOpen}><SheetTrigger asChild>
            <Button variant="ghost" size="icon" className="md:hidden" aria-label="Открыть меню"><Menu /></Button>
          </SheetTrigger><SheetContent>
            <SheetTitle className="mb-2 text-xl font-semibold">Medical AI</SheetTitle>
            <SheetDescription className="mb-8 text-sm text-muted-foreground">Личное пространство</SheetDescription>
            {navigation()}<Button asChild variant="ghost" className="mt-8"><Link href="/logout">Выйти</Link></Button>
          </SheetContent></Sheet>
          <span className="text-sm text-muted-foreground">Моё пространство <span className="mx-2 text-border">/</span>
            <span className="font-medium text-foreground">{links.find(link => pathname.startsWith(link.href))?.title ?? "Профиль"}</span></span>
        </div>
        <Button asChild variant="ghost" size="icon"><Link href="/settings" aria-label="Открыть профиль"><Settings /></Link></Button>
      </header>
      <div className="mx-auto max-w-7xl p-5 sm:p-8 lg:p-10">{children}</div>
    </div>
  </div>;
}
