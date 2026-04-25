"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Save } from "lucide-react";

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const { data: settings, isLoading } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [form, setForm] = useState<Record<string, Record<string, unknown>>>({});

  useEffect(() => { if (settings) setForm(settings as Record<string, Record<string, unknown>>); }, [settings]);

  const mutation = useMutation({
    mutationFn: (s: Record<string, unknown>) => api.saveSettings(s),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["settings"] }),
  });

  const update = (section: string, key: string, value: unknown) => {
    setForm((prev) => ({ ...prev, [section]: { ...(prev[section] ?? {}), [key]: value } }));
  };

  if (isLoading) return <p className="text-sm text-zinc-500">Loading settings...</p>;

  const db = (form.database ?? {}) as Record<string, unknown>;
  const emb = (form.embedder ?? {}) as Record<string, unknown>;

  return (
    <div className="max-w-2xl">
      <h1 className="text-xl font-semibold mb-6">Settings</h1>
      <section className="mb-6">
        <h2 className="text-sm font-medium text-zinc-400 mb-3">Database</h2>
        <div className="grid grid-cols-2 gap-3">
          <div><label className="text-[11px] text-zinc-500">Host</label><Input value={(db.host as string) ?? ""} onChange={(e) => update("database", "host", e.target.value)} className="mt-1" /></div>
          <div><label className="text-[11px] text-zinc-500">Port</label><Input type="number" value={(db.port as number) ?? 5432} onChange={(e) => update("database", "port", parseInt(e.target.value))} className="mt-1" /></div>
          <div><label className="text-[11px] text-zinc-500">Name</label><Input value={(db.name as string) ?? ""} onChange={(e) => update("database", "name", e.target.value)} className="mt-1" /></div>
          <div><label className="text-[11px] text-zinc-500">User</label><Input value={(db.user as string) ?? ""} onChange={(e) => update("database", "user", e.target.value)} className="mt-1" /></div>
        </div>
      </section>
      <section className="mb-6">
        <h2 className="text-sm font-medium text-zinc-400 mb-3">Embedder</h2>
        <div className="grid grid-cols-2 gap-3">
          <div><label className="text-[11px] text-zinc-500">Provider</label>
            <Select value={(emb.provider as string) ?? "ollama"} onValueChange={(v: string | null) => update("embedder", "provider", v ?? "ollama")}>
              <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="ollama">Ollama</SelectItem>
                <SelectItem value="openrouter">OpenRouter</SelectItem>
                <SelectItem value="openai">OpenAI</SelectItem>
                <SelectItem value="custom">Custom</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div><label className="text-[11px] text-zinc-500">Model</label><Input value={(emb.model as string) ?? ""} onChange={(e) => update("embedder", "model", e.target.value)} className="mt-1" /></div>
        </div>
      </section>
      <Button onClick={() => mutation.mutate(form)} disabled={mutation.isPending}>
        <Save className="h-4 w-4 mr-2" />{mutation.isPending ? "Saving..." : "Save Settings"}
      </Button>
      {mutation.isSuccess && <p className="mt-2 text-sm text-green-500">Saved! Restart services to apply.</p>}
    </div>
  );
}
