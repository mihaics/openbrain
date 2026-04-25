"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Memory } from "@/lib/api";
import { MemoryCard } from "@/components/memory-card";
import { MemoryDetail } from "@/components/memory-detail";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const typeIcons: Record<string, string> = {
  people: "\u{1F464}", organizations: "\u{1F3E2}", locations: "\u{1F4CD}", technologies: "\u{1F4BB}", projects: "\u{1F4C1}",
  emails: "\u{1F4E7}", urls: "\u{1F517}", phones: "\u{1F4F1}", dates: "\u{1F4C5}", hashtags: "#", mentions: "@",
};

export default function EntitiesPage() {
  const [selectedType, setSelectedType] = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);

  const { data: types = {} } = useQuery({ queryKey: ["entityTypes"], queryFn: api.entityTypes });
  const { data: names = [] } = useQuery({
    queryKey: ["entityNames", selectedType], queryFn: () => api.entityNames(selectedType!, 100), enabled: !!selectedType,
  });
  const { data: memories = [] } = useQuery({
    queryKey: ["entityMemories", selectedType, selectedName, 50],
    queryFn: () => api.entityMemories(selectedType!, selectedName!, 50),
    enabled: !!selectedType && !!selectedName,
  });

  return (
    <div className="flex h-[calc(100vh-48px)]">
      <div className={`flex-1 flex gap-0 ${selectedMemory ? "mr-[480px]" : ""}`}>
        <div className="w-48 border-r border-zinc-800 pr-3 overflow-y-auto">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">Types</h2>
          {Object.entries(types).map(([type, count]) => (
            <button key={type} onClick={() => { setSelectedType(type); setSelectedName(null); }}
              className={cn("w-full flex items-center justify-between px-2 py-1.5 rounded text-sm",
                selectedType === type ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900")}>
              <span>{typeIcons[type] ?? "\u2022"} {type}</span>
              <Badge variant="secondary" className="text-[10px]">{count}</Badge>
            </button>
          ))}
        </div>
        <div className="w-64 border-r border-zinc-800 px-3 overflow-y-auto">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">{selectedType ?? "Select a type"}</h2>
          {names.map(({ name, count }) => (
            <button key={name} onClick={() => setSelectedName(name)}
              className={cn("w-full flex items-center justify-between px-2 py-1.5 rounded text-sm",
                selectedName === name ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900")}>
              <span className="truncate">{name}</span>
              <Badge variant="secondary" className="text-[10px] ml-2 shrink-0">{count}</Badge>
            </button>
          ))}
        </div>
        <div className="flex-1 pl-3 overflow-y-auto">
          <h2 className="text-xs font-medium text-zinc-500 mb-2 uppercase tracking-wider">
            {selectedName ? `Memories with "${selectedName}"` : "Select an entity"}
          </h2>
          <div className="space-y-2">
            {memories.map((mem) => (<MemoryCard key={mem.id} memory={mem} onClick={() => setSelectedMemory(mem)} />))}
          </div>
        </div>
      </div>
      {selectedMemory && <MemoryDetail key={selectedMemory.id} memory={selectedMemory} onClose={() => setSelectedMemory(null)} />}
    </div>
  );
}
