const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface Memory {
  id: string;
  source: string;
  content: string;
  tags: string[];
  entities: Record<string, string[]>;
  importance: number;
  created_at: string;
}

export interface SearchParams {
  query: string;
  limit?: number;
  sources?: string[];
  tags?: string[];
  date_from?: string;
  date_to?: string;
  importance_min?: number;
}

export interface TimelineResponse {
  days: Record<string, Memory[]>;
  has_more: boolean;
}

export interface GraphData {
  nodes: { id: string; label: string; type: string; count: number }[];
  edges: { source: string; target: string; weight: number }[];
}

export interface Stats {
  total: number;
  this_week: number;
  this_month: number;
  by_source: Record<string, number>;
  top_tags: Record<string, number>;
}

export const api = {
  recent: (limit = 25, offset = 0, source?: string) => {
    const params = new URLSearchParams();
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    if (source) params.set("source", source);
    return apiFetch<Memory[]>(`/memories?${params}`);
  },
  search: (params: SearchParams) =>
    apiFetch<Memory[]>("/memories/search", { method: "POST", body: JSON.stringify(params) }),
  getMemory: (id: string) => apiFetch<Memory>(`/memories/${id}`),
  createMemory: (content: string, source = "web", tags: string[] = []) =>
    apiFetch<{ id: string; status: string; tags: string[] }>("/memories", {
      method: "POST", body: JSON.stringify({ content, source, tags }),
    }),
  updateMemory: (id: string, data: { content: string; tags: string[]; source: string; importance: number }) =>
    apiFetch<{ status: string }>(`/memories/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteMemory: (id: string) => apiFetch<{ status: string }>(`/memories/${id}`, { method: "DELETE" }),
  stats: () => apiFetch<Stats>("/stats"),
  timeline: (from?: string, to?: string, limitPerDay = 50) => {
    const params = new URLSearchParams();
    if (from) params.set("date_from", from);
    if (to) params.set("date_to", to);
    params.set("limit_per_day", String(limitPerDay));
    return apiFetch<TimelineResponse>(`/memories/timeline?${params}`);
  },
  entityTypes: () => apiFetch<Record<string, number>>("/entities/types"),
  entityNames: (type: string, limit = 50) =>
    apiFetch<{ name: string; count: number }[]>(`/entities/names?type=${type}&limit=${limit}`),
  entityMemories: (type: string, name: string, limit = 20) =>
    apiFetch<Memory[]>(`/entities/memories?type=${type}&name=${name}&limit=${limit}`),
  graph: (types?: string[], limit = 200) => {
    const params = new URLSearchParams();
    if (types?.length) params.set("types", types.join(","));
    params.set("limit", String(limit));
    return apiFetch<GraphData>(`/memories/graph?${params}`);
  },
  getSettings: () => apiFetch<Record<string, unknown>>("/settings"),
  saveSettings: (settings: Record<string, unknown>) =>
    apiFetch<{ status: string }>("/settings", { method: "PUT", body: JSON.stringify(settings) }),
};
