import { Fragment, type ReactNode } from "react";
import type { Citation } from "../lib/chat-api";

export function safeSourceHref(value: string): string | undefined {
  if (/^\/documents\/[\w-]+(?:#[\w-]+)?$/.test(value)) return value;
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && !url.username && !url.password) return value;
  } catch {
    // Invalid URLs are rendered as text.
  }
}

function inline(text: string, links: ReadonlySet<string>) {
  const tokens = /\[([^\]\n]+)\]\(([^\s()]+)\)|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|(https:\/\/[^\s<>()\x5b\x5d]+)/g;
  const nodes: ReactNode[] = [];
  let start = 0;
  for (const match of text.matchAll(tokens)) {
    const index = match.index;
    const url = match[2] ?? match[5]?.replace(/[.,]+$/, "");
    const label = match[1] ?? url;
    nodes.push(text.slice(start, index));
    if (url && label) {
      nodes.push(<Fragment key={index}>
        {links.has(url) ? <a className="underline" href={url}>{label}</a> : label}
        {match[5]?.slice(url.length)}
      </Fragment>);
    } else if (match[3]) {
      nodes.push(<strong key={index}>{match[3]}</strong>);
    } else {
      nodes.push(<em key={index}>{match[4]}</em>);
    }
    start = index + match[0].length;
  }
  nodes.push(text.slice(start));
  return nodes;
}

export function ChatAnswer({ text, sources }: { text: string; sources: Citation[] }) {
  const links = new Set(sources.flatMap(source => safeSourceHref(source.source_url) ?? []));
  const blocks: { kind: "text" | "ul" | "ol"; lines: string[]; first?: number }[] = [];
  for (const line of text.replace(/\\([*_[\]()])/g, "$1").split("\n")) {
    const bullet = /^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/.exec(line);
    const kind = bullet?.[1] ? "ul" : bullet?.[2] ? "ol" : "text";
    const block = blocks.at(-1);
    const value = bullet ? bullet[3] : line;
    if (block?.kind === kind) block.lines.push(value);
    else blocks.push({ kind, lines: [value], first: bullet?.[2] ? Number(bullet[2]) : undefined });
  }
  return <div className="break-words">
    {blocks.map(({ kind, lines, first }, index) => {
      if (kind === "text") {
        return <p key={index} className="whitespace-pre-wrap">{inline(lines.join("\n"), links)}</p>;
      }
      const items = lines.map((line, item) => <li key={item}>{inline(line, links)}</li>);
      return kind === "ul"
        ? <ul key={index} className="list-disc pl-6">{items}</ul>
        : <ol key={index} start={first} className="list-decimal pl-6">{items}</ol>;
    })}
  </div>;
}
