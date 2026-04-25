"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { format, subDays } from "date-fns";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { MemoryCard } from "@/components/memory-card";
import { MemoryDetail } from "@/components/memory-detail";
import type { Memory } from "@/lib/api";

export default function TimelinePage() {
  const [daysBack, setDaysBack] = useState(7);
  const [jumpDate, setJumpDate] = useState("");
  const [selected, setSelected] = useState<Memory | null>(null);

  const fromDate = jumpDate || format(subDays(new Date(), daysBack), "yyyy-MM-dd");
  const toDate = jumpDate
    ? `${jumpDate}T23:59:59`
    : format(new Date(), "yyyy-MM-dd'T'HH:mm:ss");

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["timeline", fromDate, toDate],
    queryFn: () => api.timeline(fromDate, toDate),
  });

  const days = data?.days ?? {};
  const sortedDays = Object.keys(days).sort().reverse();

  return (
    <div className="flex h-[calc(100dvh-88px)] lg:h-[calc(100vh-48px)]">
      <div className={`flex-1 overflow-y-auto lg:pr-4 ${selected ? "lg:mr-[480px]" : ""}`}>
        <div className="flex flex-col gap-3 mb-4 sm:flex-row sm:items-center sm:justify-between">
          <h1 className="text-xl font-semibold">Timeline</h1>
          <div className="flex items-center gap-3">
            <label className="text-[11px] text-zinc-500">Jump to date</label>
            <Input type="date" value={jumpDate} onChange={(e) => setJumpDate(e.target.value)} className="w-40" />
          </div>
        </div>
        {isLoading && <p className="text-sm text-zinc-500">Loading...</p>}
        {isError && (
          <div className="mb-3 flex items-center justify-between rounded-md border border-red-900/70 bg-red-950/30 px-3 py-2 text-sm text-red-200">
            <span>Timeline failed to load.</span>
            <button className="text-red-100 underline underline-offset-4" onClick={() => refetch()}>Retry</button>
          </div>
        )}
        <div className="space-y-6">
          {sortedDays.map((day) => (
            <div key={day}>
              <h2 className="text-sm font-medium text-zinc-400 mb-2 sticky top-0 bg-zinc-950 py-1 z-10">
                {format(new Date(day + "T00:00:00"), "EEEE, MMMM d, yyyy")}
                <span className="ml-2 text-zinc-600">({days[day].length})</span>
              </h2>
              <div className="space-y-2 pl-4 border-l border-zinc-800">
                {days[day].map((mem) => (<MemoryCard key={mem.id} memory={mem} onClick={() => setSelected(mem)} />))}
              </div>
            </div>
          ))}
        </div>
        {!isLoading && !isError && sortedDays.length === 0 && <p className="text-sm text-zinc-500">No memories in this date range.</p>}
        {data?.has_more && !jumpDate && (
          <button onClick={() => setDaysBack((d) => d + 7)}
            className="mt-4 w-full py-2 text-sm text-zinc-500 hover:text-zinc-300 border border-zinc-800 rounded-md">
            Load more...
          </button>
        )}
      </div>
      {selected && <MemoryDetail key={selected.id} memory={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
