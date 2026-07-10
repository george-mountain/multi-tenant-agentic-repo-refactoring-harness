import { useEffect, useRef, useState } from "react";
import { eventStreamUrl } from "./api";
import type { RunEvent } from "./types";

export function useRunStream(runId: string, active: boolean): RunEvent[] {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const seenRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    setEvents([]);
    seenRef.current = new Set();
    if (!active) return;

    const source = new EventSource(eventStreamUrl(runId));
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as RunEvent;
      if (event.type === "run_completed" || event.type === "run_failed") source.close();
      if (seenRef.current.has(event.seq)) return;
      seenRef.current.add(event.seq);
      setEvents((previous) => [...previous, event].sort((a, b) => a.seq - b.seq));
    };
    source.onerror = () => {
      /* EventSource auto-reconnects; server replays history and we dedupe by seq */
    };
    return () => source.close();
  }, [runId, active]);

  return events;
}
