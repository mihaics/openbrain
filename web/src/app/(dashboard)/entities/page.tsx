"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Memory } from "@/lib/api";
import { MemoryCard } from "@/components/memory-card";
import { MemoryDetail } from "@/components/memory-detail";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

const typeIcons: Record<string, string> = {
  people: "\u{1F464}", organizations: "\u{1F3E2}", locations: "\u{1F4CD}", technologies: "\u{1F4BB}", projects: "\u{1F4C1}",
  emails: "\u{1F4E7}", urls: "\u{1F517}", phones: "\u{1F4F1}", dates: "\u{1F4C5}", hashtags: "#", mentions: "@",
};

export default function EntitiesPage() {
  const [selectedTypeOverride, setSelectedTypeOverride] = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [entityFilter, setEntityFilter] = useState("");

  const { data: types = {}, isLoading: typesLoading, isError: typesError, refetch: refetchTypes } = useQuery({ queryKey: ["entityTypes"], queryFn: api.entityTypes });
  const selectedType = selectedTypeOverride ?? Object.entries(types).sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
  const { data: names = [], isError: namesError, refetch: refetchNames } = useQuery({
    queryKey: ["entityNames", selectedType], queryFn: () => api.entityNames(selectedType!, 100), enabled: !!selectedType,
  });
  const { data: memories = [], isError: memoriesError, refetch: refetchMemories } = useQuery({
    queryKey: ["entityMemories", selectedType, selectedName, 50],
    queryFn: () => api.entityMemories(selectedType!, selectedName!, 50),
    enabled: !!selectedType && !!selectedName,
  });
  const filteredNames = names.filter(({ name }) => name.toLowerCase().includes(entityFilter.trim().toLowerCase()));

  return (
    <div className="flex h-[calc(100dvh-88px)] lg:h-[calc(100vh-48px)]">
      <div className={`flex-1 flex min-w-0 flex-col gap-4 overflow-y-auto lg:flex-row lg:gap-0 lg:overflow-hidden ${selectedMemory ? "lg:mr-[480px]" : ""}`}>
        <div className="shrink-0 border-zinc-800 lg:w-48 lg:overflow-y-auto lg:border-r lg:pr-3">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">Types</h2>
          {typesLoading && <p className="text-sm text-zinc-500">Loading...</p>}
          {typesError && (
            <div className="rounded-md border border-red-900/70 bg-red-950/30 px-3 py-2 text-sm text-red-200">
              <div>Entity types failed to load.</div>
              <button className="mt-1 text-red-100 underline underline-offset-4" onClick={() => refetchTypes()}>Retry</button>
            </div>
          )}
          {Object.entries(types).map(([type, count]) => (
            <button key={type} onClick={() => { setSelectedTypeOverride(type); setSelectedName(null); setEntityFilter(""); }}
              className={cn("w-full flex items-center justify-between px-2 py-1.5 rounded text-sm",
                selectedType === type ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900")}>
              <span>{typeIcons[type] ?? "\u2022"} {type}</span>
              <Badge variant="secondary" className="text-[10px]">{count}</Badge>
            </button>
          ))}
        </div>
        <div className="shrink-0 border-zinc-800 lg:w-64 lg:overflow-y-auto lg:border-r lg:px-3">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">{selectedType ?? "Select a type"}</h2>
          {selectedType && <Input value={entityFilter} onChange={(e) => setEntityFilter(e.target.value)} placeholder="Filter entities..." className="mb-2" />}
          {namesError && (
            <div className="rounded-md border border-red-900/70 bg-red-950/30 px-3 py-2 text-sm text-red-200">
              <div>Entities failed to load.</div>
              <button className="mt-1 text-red-100 underline underline-offset-4" onClick={() => refetchNames()}>Retry</button>
            </div>
          )}
          {filteredNames.map(({ name, count }) => (
            <button key={name} onClick={() => setSelectedName(name)}
              className={cn("w-full flex items-center justify-between px-2 py-1.5 rounded text-sm",
                selectedName === name ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900")}>
              <span className="truncate">{name}</span>
              <Badge variant="secondary" className="text-[10px] ml-2 shrink-0">{count}</Badge>
            </button>
          ))}
          {selectedType && !namesError && filteredNames.length === 0 && <p className="text-sm text-zinc-500">No matching entities.</p>}
        </div>
        <div className="min-w-0 flex-1 lg:overflow-y-auto lg:pl-3">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">
            {selectedName ? `Memories with "${selectedName}"` : "Select an entity"}
          </h2>
          {memoriesError && (
            <div className="mb-3 flex items-center justify-between rounded-md border border-red-900/70 bg-red-950/30 px-3 py-2 text-sm text-red-200">
              <span>Entity memories failed to load.</span>
              <button className="text-red-100 underline underline-offset-4" onClick={() => refetchMemories()}>Retry</button>
            </div>
          )}
          <div className="space-y-2">
            {memories.map((mem) => (<MemoryCard key={mem.id} memory={mem} onClick={() => setSelectedMemory(mem)} />))}
          </div>
          {selectedName && !memoriesError && memories.length === 0 && <p className="text-sm text-zinc-500">No memories found for this entity.</p>}
        </div>
      </div>
      {selectedMemory && <MemoryDetail key={selectedMemory.id} memory={selectedMemory} onClose={() => setSelectedMemory(null)} />}
    </div>
  );
}
