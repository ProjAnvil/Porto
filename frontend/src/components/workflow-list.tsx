// frontend/src/components/workflow-list.tsx
"use client";

import { Braces, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { listWorkflows } from "@/lib/api";
import type { WorkflowListItem } from "@/lib/types";
import { DatePickerPopover } from "./date-picker-popover";

const STEP_LABELS: Record<string, string> = {
  retrieve: "检索",
  understand: "理解",
  identify: "子系统",
  generate: "规格",
};

export function WorkflowList({
  activeWorkflowId,
  onPickWorkflow,
  refreshKey,
}: {
  activeWorkflowId: string | null;
  onPickWorkflow: (id: string) => void;
  refreshKey: number;
}) {
  const [items, setItems] = useState<WorkflowListItem[]>([]);
  const [date, setDate] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const data = await listWorkflows({ date: date || undefined, limit: 20, offset: 0 });
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
  }, [date, refreshKey]);

  async function loadMore() {
    if (loading || !hasMore) return;
    setLoading(true);
    try {
      const data = await listWorkflows({ date: date || undefined, limit: 20, offset });
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
          <Braces size={15} /> Workflows
        </h2>
        <DatePickerPopover date={date} onSelect={setDate} />
      </div>
      <div className="max-h-72 space-y-2 overflow-y-auto" onScroll={onScroll}>
        {items.map((wf) => {
          const active = wf.workflow_id === activeWorkflowId;
          const stepLabel = wf.current_step
            ? STEP_LABELS[wf.current_step] ?? wf.current_step
            : "—";
          return (
            <button
              key={wf.workflow_id}
              className={`block w-full rounded-md border p-2 text-left ${
                active
                  ? "border-zinc-950 bg-zinc-50"
                  : "border-zinc-200 hover:bg-zinc-50"
              }`}
              onClick={() => onPickWorkflow(wf.workflow_id)}
            >
              <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                <span className="truncate font-medium">
                  {wf.project_name || wf.workflow_id.slice(0, 8)}
                </span>
                <span className="truncate text-zinc-400">{stepLabel}</span>
              </div>
              <div className="flex items-center justify-between text-xs text-zinc-500">
                <span>{wf.status}</span>
                <span className="truncate">{wf.created_at.slice(0, 10)}</span>
              </div>
            </button>
          );
        })}
        {loading ? (
          <div className="flex justify-center py-2">
            <Loader2 size={14} className="animate-spin text-zinc-400" />
          </div>
        ) : null}
        {!loading && items.length === 0 ? (
          <p className="text-sm text-zinc-400">暂无拆解记录。</p>
        ) : null}
        {!loading && items.length > 0 && !hasMore ? (
          <p className="py-1 text-center text-xs text-zinc-400">无更多</p>
        ) : null}
      </div>
    </section>
  );
}
