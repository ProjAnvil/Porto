// frontend/src/components/session-list.tsx
"use client";

import { Loader2, MessageSquare } from "lucide-react";
import { useEffect, useState } from "react";
import { listSessions } from "@/lib/api";
import type { SessionItem } from "@/lib/types";
import { DatePickerPopover } from "./date-picker-popover";

export function SessionList({
  activeSessionId,
  onPickSession,
}: {
  activeSessionId: string;
  onPickSession: (sessionId: string) => void;
}) {
  const [items, setItems] = useState<SessionItem[]>([]);
  const [date, setDate] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const data = await listSessions(date || undefined, 20, 0);
        if (cancelled) return;
        setItems(data.items);
        setOffset(data.items.length);
        setHasMore(data.has_more);
      } catch {
        /* 非关键 */
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [date]);

  async function loadMore() {
    if (loading || !hasMore) return;
    setLoading(true);
    try {
      const data = await listSessions(date || undefined, 20, offset);
      setItems((prev) => [...prev, ...data.items]);
      setOffset((o) => o + data.items.length);
      setHasMore(data.has_more);
    } catch {
      /* 非关键 */
    } finally {
      setLoading(false);
    }
  }

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 20) {
      loadMore();
    }
  }

  return (
    <section className="mt-4 rounded-lg border border-zinc-200 bg-white p-3">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <MessageSquare size={15} /> Sessions
        </h2>
        <DatePickerPopover date={date} onSelect={setDate} />
      </div>
      <div className="max-h-72 space-y-2 overflow-y-auto" onScroll={onScroll}>
        {items.map((s) => (
          <button
            key={s.session_id}
            className={`block w-full rounded-md border p-2 text-left ${
              s.session_id === activeSessionId
                ? "border-zinc-950 bg-zinc-50"
                : "border-zinc-200 hover:bg-zinc-50"
            }`}
            onClick={() => onPickSession(s.session_id)}
          >
            <div className="mb-1 flex items-center justify-between gap-2 text-xs">
              <span className="truncate font-medium">{s.session_id}</span>
              <span className="truncate text-zinc-400">
                {s.last_at.slice(0, 10)}
              </span>
            </div>
            <p className="line-clamp-2 text-xs leading-5 text-zinc-600">
              {s.preview}
            </p>
            <span className="text-xs text-zinc-400">{s.message_count} 条</span>
          </button>
        ))}
        {loading ? (
          <div className="flex justify-center py-2">
            <Loader2 size={14} className="animate-spin text-zinc-400" />
          </div>
        ) : null}
        {!loading && items.length === 0 ? (
          <p className="text-sm text-zinc-400">暂无 session。</p>
        ) : null}
        {!loading && items.length > 0 && !hasMore ? (
          <p className="py-1 text-center text-xs text-zinc-400">无更多</p>
        ) : null}
      </div>
    </section>
  );
}
