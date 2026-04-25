"use client";

import { useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { QuickCapture } from "@/components/quick-capture";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const [captureOpen, setCaptureOpen] = useState(false);

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <Sidebar onQuickCapture={() => setCaptureOpen(true)} />
      <QuickCapture open={captureOpen} onOpenChange={setCaptureOpen} />
      <main className="ml-60 p-6">{children}</main>
    </div>
  );
}
