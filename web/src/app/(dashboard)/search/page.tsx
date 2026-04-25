"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search as SearchIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { api } from "@/lib/api";
import type { Memory } from "@/lib/api";
import { MemoryCard } from "@/components/memory-card";
import { MemoryDetail } from "@/components/memory-detail";
import { useDebounce } from "@/lib/hooks";

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<string>("all");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [importanceMin, setImportanceMin] = useState(0);
  const [selected, setSelected] = useState<Memory | null>(null);
  const debouncedQuery = useDebounce(query, 300);
  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats });

  const hasQuery = debouncedQuery.trim().length > 0;
  const sourceOptions = ["all", ...new Set([...(stats ? Object.keys(stats.by_source) : []), "claude-code", "manual", "web", "telegram", "email", "mcp"])];
  const { data: recent = [], isLoading: recentLoading, isError: recentError, refetch: refetchRecent } = useQuery({
    queryKey: ["recent", source],
    queryFn: () => api.recent(25, 0, source !== "all" ? source : undefined),
    enabled: !hasQuery,
  });
  const { data: results = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["search", debouncedQuery, source, dateFrom, dateTo, importanceMin],
    queryFn: () => api.search({
      query: debouncedQuery, limit: 50,
      sources: source !== "all" ? [source] : undefined,
      date_from: dateFrom || undefined, date_to: dateTo ? `${dateTo}T23:59:59` : undefined,
      importance_min: importanceMin > 0 ? importanceMin : undefined,
    }),
    enabled: hasQuery,
  });
  const visibleMemories = hasQuery ? results : recent;
  const loading = hasQuery ? isLoading : recentLoading;
  const failed = hasQuery ? isError : recentError;

  return (
    <div className="flex h-[calc(100dvh-88px)] w-full min-w-0 lg:h-[calc(100vh-48px)]">
      <div className={`min-w-0 flex-1 overflow-y-auto lg:pr-4 ${selected ? "lg:mr-[480px]" : ""}`}>
        <h1 className="text-xl font-semibold mb-4">Search</h1>
        <div className="relative mb-4">
          <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search memories..." className="pl-10" />
        </div>
        <div className="mb-4 grid grid-cols-1 gap-3 sm:flex sm:flex-wrap sm:items-end">
          <div className="min-w-0"><label className="text-[11px] text-zinc-500 mb-1 block">Source</label>
            <Select value={source} onValueChange={(v) => setSource(v ?? "all")}>
              <SelectTrigger className="!w-full sm:!w-32"><SelectValue /></SelectTrigger>
              <SelectContent>
                {sourceOptions.map((option) => (
                  <SelectItem key={option} value={option}>{option === "all" ? "All" : option}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="min-w-0"><label className="text-[11px] text-zinc-500 mb-1 block">From</label>
            <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-full sm:w-36" /></div>
          <div className="min-w-0"><label className="text-[11px] text-zinc-500 mb-1 block">To</label>
            <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-full sm:w-36" /></div>
          <div className="min-w-0 sm:w-40"><label className="text-[11px] text-zinc-500 mb-1 block">Min importance: {importanceMin.toFixed(1)}</label>
            <Slider value={[importanceMin]} onValueChange={(v) => setImportanceMin(Array.isArray(v) ? v[0] : v)} min={0} max={1} step={0.1} /></div>
        </div>
        {loading && <p className="text-sm text-zinc-500">{hasQuery ? "Searching..." : "Loading recent memories..."}</p>}
        {failed && (
          <div className="mb-3 flex items-center justify-between rounded-md border border-red-900/70 bg-red-950/30 px-3 py-2 text-sm text-red-200">
            <span>{hasQuery ? "Search failed." : "Recent memories failed to load."}</span>
            <button className="text-red-100 underline underline-offset-4" onClick={() => hasQuery ? refetch() : refetchRecent()}>
              Retry
            </button>
          </div>
        )}
        {!hasQuery && !loading && !failed && (
          <h2 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">Recent memories</h2>
        )}
        <div className="space-y-2">
          {visibleMemories.map((mem) => (<MemoryCard key={mem.id} memory={mem} onClick={() => setSelected(mem)} />))}
          {hasQuery && !loading && !failed && results.length === 0 && <p className="text-sm text-zinc-500">No results found.</p>}
          {!hasQuery && !loading && !failed && recent.length === 0 && <p className="text-sm text-zinc-500">No memories captured yet.</p>}
        </div>
      </div>
      {selected && <MemoryDetail key={selected.id} memory={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
