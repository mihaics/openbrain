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

  const { data: results = [], isLoading } = useQuery({
    queryKey: ["search", debouncedQuery, source, dateFrom, dateTo, importanceMin],
    queryFn: () => api.search({
      query: debouncedQuery, limit: 50,
      sources: source !== "all" ? [source] : undefined,
      date_from: dateFrom || undefined, date_to: dateTo ? `${dateTo}T23:59:59` : undefined,
      importance_min: importanceMin > 0 ? importanceMin : undefined,
    }),
    enabled: debouncedQuery.length > 0,
  });

  return (
    <div className="flex h-[calc(100vh-48px)]">
      <div className={`flex-1 overflow-y-auto pr-4 ${selected ? "mr-[480px]" : ""}`}>
        <h1 className="text-xl font-semibold mb-4">Search</h1>
        <div className="relative mb-4">
          <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search memories..." className="pl-10" />
        </div>
        <div className="flex gap-3 mb-4 items-end flex-wrap">
          <div><label className="text-[11px] text-zinc-500 mb-1 block">Source</label>
            <Select value={source} onValueChange={(v) => setSource(v ?? "all")}>
              <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All</SelectItem>
                <SelectItem value="claude-code">Claude Code</SelectItem>
                <SelectItem value="manual">Manual</SelectItem>
                <SelectItem value="web">Web</SelectItem>
                <SelectItem value="telegram">Telegram</SelectItem>
                <SelectItem value="email">Email</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div><label className="text-[11px] text-zinc-500 mb-1 block">From</label>
            <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-36" /></div>
          <div><label className="text-[11px] text-zinc-500 mb-1 block">To</label>
            <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-36" /></div>
          <div className="w-40"><label className="text-[11px] text-zinc-500 mb-1 block">Min importance: {importanceMin.toFixed(1)}</label>
            <Slider value={[importanceMin]} onValueChange={(v) => setImportanceMin(Array.isArray(v) ? v[0] : v)} min={0} max={1} step={0.1} /></div>
        </div>
        {isLoading && <p className="text-sm text-zinc-500">Searching...</p>}
        <div className="space-y-2">
          {results.map((mem) => (<MemoryCard key={mem.id} memory={mem} onClick={() => setSelected(mem)} />))}
          {debouncedQuery && !isLoading && results.length === 0 && <p className="text-sm text-zinc-500">No results found.</p>}
        </div>
      </div>
      {selected && <MemoryDetail key={selected.id} memory={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
