"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Search, Clock, Users, Share2, Settings, Brain, Plus } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const navItems = [
  { href: "/search", label: "Search", icon: Search },
  { href: "/timeline", label: "Timeline", icon: Clock },
  { href: "/entities", label: "Entities", icon: Users },
  { href: "/graph", label: "Graph", icon: Share2 },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar({ onQuickCapture }: { onQuickCapture: () => void }) {
  const pathname = usePathname();
  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: 60_000 });

  return (
    <aside className="w-60 h-screen bg-zinc-950 border-r border-zinc-800 flex flex-col fixed left-0 top-0 z-40">
      <div className="p-4 flex items-center gap-3">
        <Brain className="h-6 w-6 text-blue-500" />
        <span className="font-semibold text-sm">Open Brain</span>
      </div>
      <div className="px-4 pb-4 flex gap-4 text-xs text-zinc-500">
        <div><span className="text-zinc-200 font-medium">{stats?.total ?? "\u2014"}</span> memories</div>
        <div><span className="text-zinc-200 font-medium">{stats?.this_week ?? "\u2014"}</span> this week</div>
      </div>
      <div className="border-t border-zinc-800" />
      <nav className="flex-1 p-2 space-y-1">
        {navItems.map(({ href, label, icon: Icon }) => (
          <Link key={href} href={href}
            className={cn("flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors",
              pathname === href ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
            )}>
            <Icon className="h-4 w-4" />{label}
          </Link>
        ))}
      </nav>
      <div className="p-2 border-t border-zinc-800">
        <button onClick={onQuickCapture}
          className="flex items-center gap-3 px-3 py-2 w-full rounded-md text-sm text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900 transition-colors">
          <Plus className="h-4 w-4" />Quick Capture
          <kbd className="ml-auto text-[10px] bg-zinc-800 px-1.5 py-0.5 rounded text-zinc-500">{"\u2318"}K</kbd>
        </button>
      </div>
    </aside>
  );
}
