"use client";

import { useState, useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { KnowledgeGraph } from "@/components/knowledge-graph";
import { MemoryCard } from "@/components/memory-card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Search } from "lucide-react";

const allTypes = ["people", "organizations", "locations", "technologies", "projects"];

export default function GraphPage() {
  const [enabledTypes, setEnabledTypes] = useState<string[]>(allTypes);
  const [nodeLimit, setNodeLimit] = useState(100);
  const [graphSearch, setGraphSearch] = useState("");
  const [selectedEntity, setSelectedEntity] = useState<{ type: string; name: string } | null>(null);

  const { data: graphData, isLoading, isError, refetch } = useQuery({
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

  const visibleGraphData = useMemo(() => {
    if (!graphData || !graphSearch.trim()) return graphData;
    const needle = graphSearch.trim().toLowerCase();
    const nodes = graphData.nodes.filter((node) => node.label.toLowerCase().includes(needle));
    const nodeIds = new Set(nodes.map((node) => node.id));
    return { nodes, edges: graphData.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target)) };
  }, [graphData, graphSearch]);

  return (
    <div className="flex h-[calc(100dvh-88px)] flex-col gap-4 lg:h-[calc(100vh-48px)] lg:flex-row">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="mb-3 flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <h1 className="text-xl font-semibold">Knowledge Graph</h1>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
              <Input value={graphSearch} onChange={(e) => setGraphSearch(e.target.value)} placeholder="Find node..." className="w-full pl-8 sm:w-44" />
            </div>
            <div className="flex flex-wrap gap-2">
            {allTypes.map((type) => (
              <button key={type} onClick={() => toggleType(type)}
                className={`px-2 py-1 text-xs rounded border transition-colors ${
                  enabledTypes.includes(type) ? "border-zinc-600 text-zinc-200" : "border-zinc-800 text-zinc-600"
                }`}>{type}</button>
            ))}
            </div>
            <Button variant="outline" size="sm" onClick={() => setNodeLimit((limit) => limit === 100 ? 200 : 100)}>
              {nodeLimit} nodes
            </Button>
          </div>
        </div>
        <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-500">
          {allTypes.map((type) => (
            <span key={type} className="inline-flex items-center gap-1.5">
              <span className={`h-2.5 w-2.5 rounded-full ${type === "people" ? "bg-blue-500" : type === "organizations" ? "bg-orange-500" : type === "locations" ? "bg-red-500" : type === "technologies" ? "bg-purple-500" : "bg-green-500"}`} />
              {type}
            </span>
          ))}
        </div>
        {isLoading && <p className="text-sm text-zinc-500">Loading graph...</p>}
        {isError && (
          <div className="mb-3 flex items-center justify-between rounded-md border border-red-900/70 bg-red-950/30 px-3 py-2 text-sm text-red-200">
            <span>Graph failed to load.</span>
            <button className="text-red-100 underline underline-offset-4" onClick={() => refetch()}>Retry</button>
          </div>
        )}
        <div className="min-h-[420px] flex-1">
          {visibleGraphData && visibleGraphData.nodes.length > 0 && <KnowledgeGraph data={visibleGraphData} onNodeClick={handleNodeClick} />}
          {visibleGraphData && visibleGraphData.nodes.length === 0 && <p className="text-sm text-zinc-500">No graph nodes match this filter.</p>}
        </div>
      </div>
      {selectedEntity && (
        <div className="shrink-0 border-zinc-800 lg:w-80 lg:overflow-y-auto lg:border-l lg:pl-4">
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
