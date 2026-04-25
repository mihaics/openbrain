"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Memory } from "@/lib/api";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { X, Save, Trash2 } from "lucide-react";
import { format } from "date-fns";

export function MemoryDetail({ memory, onClose }: { memory: Memory; onClose: () => void }) {
  const [content, setContent] = useState(memory.content);
  const [tags, setTags] = useState(memory.tags.join(", "));
  const [source, setSource] = useState(memory.source);
  const [importance, setImportance] = useState(memory.importance);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const queryClient = useQueryClient();

  const invalidateMemoryViews = () => {
    const keys = ["recent", "search", "timeline", "entityMemories", "entityTypes", "entityNames", "graph", "stats"];
    keys.forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }));
  };

  const updateMut = useMutation({
    mutationFn: () => api.updateMemory(memory.id, {
      content, tags: tags.split(",").map((t) => t.trim()).filter(Boolean), source, importance,
    }),
    onSuccess: () => { invalidateMemoryViews(); onClose(); },
  });

  const deleteMut = useMutation({
    mutationFn: () => api.deleteMemory(memory.id),
    onSuccess: () => { invalidateMemoryViews(); setConfirmDelete(false); onClose(); },
  });

  return (
    <div className="fixed inset-x-0 bottom-0 top-14 z-30 overflow-y-auto border-t border-zinc-800 bg-zinc-950 p-4 lg:inset-x-auto lg:top-0 lg:right-0 lg:h-screen lg:w-[480px] lg:border-l lg:border-t-0 lg:p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-medium text-zinc-400">Memory Detail</h2>
        <Button variant="ghost" size="icon" onClick={onClose}><X className="h-4 w-4" /></Button>
      </div>
      <textarea value={content} onChange={(e) => setContent(e.target.value)}
        className="w-full bg-zinc-900 border border-zinc-800 rounded-md p-3 text-sm text-zinc-200 min-h-[200px] resize-y focus:outline-none focus:border-zinc-600" />
      <div className="mt-4 space-y-3">
        <div><label className="text-xs text-zinc-500">Source</label>
          <Input value={source} onChange={(e) => setSource(e.target.value)} className="mt-1" /></div>
        <div><label className="text-xs text-zinc-500">Tags (comma-separated)</label>
          <Input value={tags} onChange={(e) => setTags(e.target.value)} className="mt-1" /></div>
        <div><label className="text-xs text-zinc-500">Importance: {importance.toFixed(2)}</label>
          <Slider value={[importance]} onValueChange={(v) => setImportance(Array.isArray(v) ? v[0] : v)} min={0} max={1} step={0.05} className="mt-2" /></div>
        {memory.entities && Object.keys(memory.entities).length > 0 && (
          <div><label className="text-xs text-zinc-500 mb-1 block">Entities</label>
            <div className="flex flex-wrap gap-1">
              {Object.entries(memory.entities).flatMap(([type, names]) =>
                (names as string[]).map((name) => (
                  <Badge key={`${type}:${name}`} variant="outline" className="text-[10px]">{type}: {name}</Badge>
                ))
              )}
            </div>
          </div>
        )}
        <div className="text-[11px] text-zinc-600">
          ID: {memory.id} &middot; {memory.created_at ? format(new Date(memory.created_at), "PPpp") : "\u2014"}
        </div>
      </div>
      <div className="mt-6 flex gap-2">
        <Button onClick={() => updateMut.mutate()} disabled={updateMut.isPending} className="flex-1">
          <Save className="h-4 w-4 mr-2" />{updateMut.isPending ? "Saving..." : "Save"}
        </Button>
        <Button variant="destructive" onClick={() => setConfirmDelete(true)} disabled={deleteMut.isPending} aria-label="Delete memory">
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>
      {(updateMut.isError || deleteMut.isError) && (
        <p className="mt-3 rounded-md border border-red-900/70 bg-red-950/30 px-3 py-2 text-sm text-red-200">
          {updateMut.isError ? "Could not save this memory." : "Could not delete this memory."}
        </p>
      )}
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete memory?</DialogTitle>
            <DialogDescription>This permanently removes the selected memory from Open Brain.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={deleteMut.isPending}>Cancel</Button>
            <Button variant="destructive" onClick={() => deleteMut.mutate()} disabled={deleteMut.isPending}>
              {deleteMut.isPending ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
