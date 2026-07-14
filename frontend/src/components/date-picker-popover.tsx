"use client";

import { format } from "date-fns";
import { zhCN } from "date-fns/locale";
import { Calendar, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { DayPicker } from "react-day-picker";

export function DatePickerPopover({
  date,
  onSelect,
}: {
  date: string;
  onSelect: (date: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const selected = date ? new Date(date + "T00:00:00") : undefined;

  return (
    <div className="relative" ref={ref}>
      <div className="flex items-center">
        <button
          type="button"
          className={`rounded-md p-1 ${date ? "bg-zinc-950 text-white" : "text-zinc-500 hover:bg-zinc-100"}`}
          onClick={() => setOpen((v) => !v)}
          title={date ? `筛选: ${date}` : "按日期筛选"}
        >
          <Calendar size={14} />
        </button>
        {date ? (
          <button
            type="button"
            className="ml-0.5 text-zinc-400 hover:text-zinc-600"
            onClick={() => onSelect("")}
            title="清除日期"
          >
            <X size={12} />
          </button>
        ) : null}
      </div>
      {open ? (
        <div className="absolute right-0 z-50 mt-1 rounded-md border border-zinc-200 bg-white p-2 shadow-lg">
          <DayPicker
            mode="single"
            locale={zhCN}
            selected={selected}
            onSelect={(d) => {
              if (d) {
                onSelect(format(d, "yyyy-MM-dd"));
                setOpen(false);
              }
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
