"use client";

import { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CommandDialog, CommandInput } from "@/components/ui/command";
import { api } from "@/lib/api";

export function QuickCapture({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [value, setValue] = useState("");
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (content: string) => api.createMemory(content, "web"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["stats"] });
      setValue("");
      onOpenChange(false);
    },
  });

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && value.trim() && !e.shiftKey) {
      e.preventDefault();
      mutation.mutate(value.trim());
    }
  };

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        onOpenChange(!open);
      }
    };
    document.addEventListener("keydown", down);
    return () => document.removeEventListener("keydown", down);
  }, [open, onOpenChange]);

  useEffect(() => {
    if (open) mutation.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput placeholder="Capture a memory... (Enter to save)" value={value} onValueChange={setValue} onKeyDown={handleKeyDown} />
      {mutation.isPending && <div className="p-4 text-sm text-zinc-500">Saving...</div>}
      {mutation.isSuccess && <div className="p-4 text-sm text-green-500">Saved!</div>}
      {mutation.isError && <div className="p-4 text-sm text-red-500">Error saving memory</div>}
    </CommandDialog>
  );
}
