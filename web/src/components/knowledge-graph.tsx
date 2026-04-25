"use client";

import { useEffect, useRef, useCallback } from "react";
import cytoscape from "cytoscape";
import type { Core } from "cytoscape";
import coseBilkent from "cytoscape-cose-bilkent";
import type { GraphData } from "@/lib/api";

if (typeof window !== "undefined") {
  try {
    cytoscape.use(coseBilkent);
  } catch {
    // already registered (e.g. after Fast Refresh) — safe to ignore
  }
}

const typeColors: Record<string, string> = {
  people: "#3b82f6", organizations: "#f97316", locations: "#ef4444",
  technologies: "#a855f7", projects: "#22c55e", emails: "#06b6d4",
  urls: "#64748b", phones: "#eab308", dates: "#ec4899",
  hashtags: "#8b5cf6", mentions: "#14b8a6",
};

export function KnowledgeGraph({ data, onNodeClick }: {
  data: GraphData;
  onNodeClick?: (nodeId: string, label: string, type: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);

  const init = useCallback(() => {
    if (!containerRef.current || !data.nodes.length) return;
    if (cyRef.current) cyRef.current.destroy();

    const maxCount = Math.max(...data.nodes.map((n) => n.count), 1);

    cyRef.current = cytoscape({
      container: containerRef.current,
      elements: [
        ...data.nodes.map((n) => ({
          data: { id: n.id, label: n.label, type: n.type,
            size: 20 + (n.count / maxCount) * 40,
            color: typeColors[n.type] ?? "#6b7280" },
        })),
        ...data.edges.map((e) => ({
          data: { source: e.source, target: e.target, weight: e.weight },
        })),
      ],
      style: [
        { selector: "node", style: {
          label: "data(label)", "background-color": "data(color)" as unknown as string,
          width: "data(size)" as unknown as number, height: "data(size)" as unknown as number,
          "font-size": "9px", color: "#d4d4d8",
          "min-zoomed-font-size": 7 as unknown as string, "text-max-width": 80 as unknown as string, "text-wrap": "ellipsis" as const,
          "text-outline-color": "#09090b", "text-outline-width": 2 as unknown as string,
          "text-valign": "bottom" as const, "text-margin-y": 5, "overlay-opacity": 0,
        }},
        { selector: "edge", style: {
          width: "mapData(weight, 1, 20, 1, 6)" as unknown as number,
          "line-color": "#333", opacity: 0.4, "curve-style": "bezier" as const,
        }},
        { selector: "node:selected", style: { "border-width": 3, "border-color": "#fff" }},
      ],
      layout: { name: "cose-bilkent", animate: false, nodeRepulsion: 8000, idealEdgeLength: 120 } as cytoscape.LayoutOptions,
    });

    cyRef.current.on("tap", "node", (evt) => {
      const node = evt.target;
      onNodeClick?.(node.id(), node.data("label"), node.data("type"));
    });
  }, [data, onNodeClick]);

  useEffect(() => { init(); return () => { cyRef.current?.destroy(); }; }, [init]);

  return <div ref={containerRef} className="w-full h-full bg-zinc-950 rounded-lg border border-zinc-800" />;
}
