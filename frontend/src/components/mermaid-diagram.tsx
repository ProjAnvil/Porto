"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

/**
 * 可复用的 mermaid 流程图渲染组件。
 *
 * 在 useEffect 内动态 import("mermaid") —— 只在浏览器执行，
 * 不影响 SSR。mermaid.render() 返回 SVG 字符串，注入 ref 容器。
 * 初始化只执行一次（模块级 flag）。
 */

let mermaidInitialized = false;

export function MermaidDiagram({
  chart,
  className,
}: {
  chart: string;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  // useId 返回含冒号的 ID（如 «r0»），mermaid render id 需合法 HTML id
  const rawId = useId();
  const renderId = `mmd-${rawId.replace(/[^a-zA-Z0-9]/g, "")}`;
  const [status, setStatus] = useState<"loading" | "done" | "error">("loading");
  // chart 变化时重置回 loading。采用 React 推荐的「渲染期调整 state」模式
  // （条件性 setState during render），避免在 effect 内同步调用 setState
  // 触发 react-hooks/set-state-in-effect 报错。
  const [prevChart, setPrevChart] = useState(chart);
  if (chart !== prevChart) {
    setPrevChart(chart);
    setStatus("loading");
  }

  useEffect(() => {
    let cancelled = false;

    async function renderChart() {
      try {
        const mermaid = (await import("mermaid")).default;
        if (!mermaidInitialized) {
          mermaid.initialize({
            startOnLoad: false,
            theme: "default",
            securityLevel: "loose",
          });
          mermaidInitialized = true;
        }
        const { svg } = await mermaid.render(renderId, chart);
        if (!cancelled && containerRef.current) {
          containerRef.current.innerHTML = svg;
          setStatus("done");
        }
      } catch {
        if (!cancelled) setStatus("error");
      }
    }

    renderChart();
    return () => {
      cancelled = true;
    };
  }, [chart, renderId]);

  if (status === "error") {
    return (
      <pre className="overflow-x-auto rounded-lg bg-zinc-50 p-3 text-xs text-zinc-600">
        {chart}
      </pre>
    );
  }

  return (
    <div className={className}>
      {status === "loading" ? (
        <div className="flex items-center gap-2 py-8 text-sm text-zinc-400">
          <Loader2 size={16} className="animate-spin" />
          渲染图中…
        </div>
      ) : null}
      <div
        ref={containerRef}
        className="overflow-x-auto"
        style={{ display: status === "done" ? "block" : "none" }}
      />
    </div>
  );
}
