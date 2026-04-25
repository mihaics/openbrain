"use client";

import type { Memory } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { format } from "date-fns";

const sourceColors: Record<string, string> = {
  "claude-code": "bg-purple-900/50 text-purple-300 border-purple-800",
  manual: "bg-blue-900/50 text-blue-300 border-blue-800",
  telegram: "bg-sky-900/50 text-sky-300 border-sky-800",
  web: "bg-green-900/50 text-green-300 border-green-800",
  email: "bg-amber-900/50 text-amber-300 border-amber-800",
  mcp: "bg-indigo-900/50 text-indigo-300 border-indigo-800",
};

export function MemoryCard({ memory, onClick }: { memory: Memory; onClick?: () => void }) {
  const date = memory.created_at ? format(new Date(memory.created_at), "MMM d, HH:mm") : "";

  return (
    <button onClick={onClick}
      className="w-full max-w-full overflow-hidden text-left p-4 rounded-lg border border-zinc-800 hover:border-zinc-600 bg-zinc-900/50 hover:bg-zinc-900 transition-colors">
      <p className="text-sm text-zinc-300 line-clamp-2 break-words">{memory.content}</p>
      <div className="mt-2 flex min-w-0 items-center gap-2 flex-wrap">
        <Badge variant="outline" className={sourceColors[memory.source] ?? "bg-zinc-800/50 text-zinc-400 border-zinc-700"}>
          {memory.source}
        </Badge>
        {memory.tags.slice(0, 4).map((tag) => (
          <Badge key={tag} variant="secondary" className="text-[11px] bg-zinc-800 text-zinc-400">{tag}</Badge>
        ))}
        {memory.tags.length > 4 && <span className="text-[11px] text-zinc-600">+{memory.tags.length - 4}</span>}
        <span className="w-full text-[11px] text-zinc-600 sm:ml-auto sm:w-auto">{date}</span>
      </div>
    </button>
  );
}
