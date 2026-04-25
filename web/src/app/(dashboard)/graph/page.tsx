"use client";

import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { KnowledgeGraph } from "@/components/knowledge-graph";
import { MemoryCard } from "@/components/memory-card";
import { Badge } from "@/components/ui/badge";

const allTypes = ["people", "organizations", "locations", "technologies", "projects"];

export default function GraphPage() {
  const [enabledTypes, setEnabledTypes] = useState<string[]>(allTypes);
  const [nodeLimit] = useState(150);
  const [selectedEntity, setSelectedEntity] = useState<{ type: string; name: string } | null>(null);

  const { data: graphData } = useQuery({
    queryKey: ["graph", enabledTypes, nodeLimit],
    queryFn: () => api.graph(enabledTypes, nodeLimit),
  });

  const { data: entityMemories = [] } = useQuery({
    queryKey: ["entityMemories", selectedEntity?.type, selectedEntity?.name, 20],
    queryFn: () => api.entityMemories(selectedEntity!.type, selectedEntity!.name, 20),
    enabled: !!selectedEntity,
  });

  const handleNodeClick = useCallback((id: string, label: string, type: string) => {
    setSelectedEntity({ type, name: label });
  }, []);

  const toggleType = (type: string) => {
    setEnabledTypes((prev) => prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]);
  };

  return (
    <div className="flex h-[calc(100vh-48px)] gap-4">
      <div className="flex-1 flex flex-col">
        <div className="flex items-center justify-between mb-3">
          <h1 className="text-xl font-semibold">Knowledge Graph</h1>
          <div className="flex gap-2">
            {allTypes.map((type) => (
              <button key={type} onClick={() => toggleType(type)}
                className={`px-2 py-1 text-xs rounded border transition-colors ${
                  enabledTypes.includes(type) ? "border-zinc-600 text-zinc-200" : "border-zinc-800 text-zinc-600"
                }`}>{type}</button>
            ))}
          </div>
        </div>
        <div className="flex-1">{graphData && <KnowledgeGraph data={graphData} onNodeClick={handleNodeClick} />}</div>
      </div>
      {selectedEntity && (
        <div className="w-80 border-l border-zinc-800 pl-4 overflow-y-auto">
          <h2 className="text-sm font-medium mb-1">{selectedEntity.name}</h2>
          <Badge variant="outline" className="text-[10px] mb-3">{selectedEntity.type}</Badge>
          <div className="space-y-2">
            {entityMemories.map((mem) => (<MemoryCard key={mem.id} memory={mem} />))}
          </div>
        </div>
      )}
    </div>
  );
}
