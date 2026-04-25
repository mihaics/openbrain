"use client";

import { useState } from "react";
import { Sidebar } from "@/components/sidebar";
import { QuickCapture } from "@/components/quick-capture";
import { Button } from "@/components/ui/button";
import { Brain, Menu } from "lucide-react";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const [captureOpen, setCaptureOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="hidden lg:block fixed left-0 top-0 z-40">
        <Sidebar onQuickCapture={() => setCaptureOpen(true)} />
      </div>
      <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-zinc-800 bg-zinc-950 px-4 lg:hidden">
        <Button variant="ghost" size="icon" onClick={() => setNavOpen(true)} aria-label="Open navigation">
          <Menu className="h-5 w-5" />
        </Button>
        <Brain className="h-5 w-5 text-blue-500" />
        <span className="text-sm font-semibold">Open Brain</span>
      </header>
      {navOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            className="absolute inset-0 bg-black/60"
            onClick={() => setNavOpen(false)}
          />
          <div className="relative h-full w-60">
            <Sidebar onQuickCapture={() => setCaptureOpen(true)} onNavigate={() => setNavOpen(false)} />
          </div>
        </div>
      )}
      <QuickCapture open={captureOpen} onOpenChange={setCaptureOpen} />
      <main className="overflow-x-hidden p-4 lg:ml-60 lg:p-6">{children}</main>
    </div>
  );
}
