import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, captureToken } from "./api";
import type { Selection, Snapshot, View } from "./types";
import { TopBar } from "./components/TopBar";
import { NeedsYou } from "./components/NeedsYou";
import { AgentRow } from "./components/AgentRow";
import { Board } from "./components/Board";
import { Drawer } from "./components/Drawer";
import { BottomTabs } from "./components/BottomTabs";
import { ConfirmDialog, type Confirmation } from "./components/ConfirmDialog";
import { Toasts, type Toast } from "./components/Toasts";
import { CodeReview } from "./components/review/CodeReview";

const REFRESH_MS = 2000;

export interface Actions {
  /** Ask first, then POST; refreshes and reports the outcome either way. */
  confirm: (c: Omit<Confirmation, "onConfirm"> & { path: string; payload?: unknown; done: string }) => void;
  /** POST without asking (only for harmless, reversible moves). */
  run: (path: string, done: string, body?: unknown) => Promise<void>;
  select: (s: Selection) => void;
  /** Switch to the code view, showing one change source. */
  openReview: (agent: string) => void;
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [offline, setOffline] = useState(false);
  const [expired, setExpired] = useState(!captureToken());
  const [selection, setSelection] = useState<Selection>(null);
  const [view, setView] = useState<View>(() => (window.location.hash.includes("code") ? "code" : "board"));
  const [reviewSource, setReviewSource] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastId = useRef(0);

  const toast = useCallback((text: string, tone: Toast["tone"] = "ok") => {
    const id = ++toastId.current;
    setToasts((all) => [...all, { id, text, tone }]);
    window.setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), 6000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await api.get<Snapshot>("/api/snapshot");
      setSnapshot(next);
      setOffline(false);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) setExpired(true);
      else setOffline(true);
    }
  }, []);

  useEffect(() => {
    if (expired) return;
    refresh();
    const timer = window.setInterval(refresh, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [expired, refresh]);

  const run = useCallback(
    async (path: string, done: string, body?: unknown) => {
      try {
        await api.post(path, body);
        toast(done);
      } catch (error) {
        toast(error instanceof Error ? error.message : String(error), "error");
      }
      await refresh();
    },
    [refresh, toast],
  );

  const actions: Actions = useMemo(
    () => ({
      run,
      select: setSelection,
      openReview: (agent: string) => {
        setReviewSource(agent);
        setView("code");
        setSelection(null);
      },
      confirm: ({ path, payload, done, ...question }) =>
        setConfirmation({ ...question, onConfirm: () => run(path, done, payload) }),
    }),
    [run],
  );

  if (expired) {
    return (
      <main className="gate">
        <h1>This page needs a fresh link</h1>
        <p>
          The dashboard only answers the link <code>agentctl gui</code> opened, and
          this one has expired or was opened without it. Run <code>agentctl gui</code>{" "}
          in your project again and use the link it opens.
        </p>
      </main>
    );
  }

  if (!snapshot) {
    return (
      <main className="gate">
        <p>{offline ? "Can't reach agentctl gui. Is it still running?" : "Loading…"}</p>
      </main>
    );
  }

  return (
    <div className={`shell ${selection ? "with-drawer" : ""}`}>
      <TopBar snapshot={snapshot} actions={actions} view={view} onView={setView} />
      {offline && (
        <p className="banner" role="status">
          Lost contact with <code>agentctl gui</code>. Showing the last known state.
        </p>
      )}
      {view === "board" ? (
        <main className="main">
          <NeedsYou items={snapshot.attention} selection={selection} actions={actions} />
          <AgentRow snapshot={snapshot} selection={selection} actions={actions} />
          <Board snapshot={snapshot} selection={selection} actions={actions} />
          <BottomTabs snapshot={snapshot} selection={selection} actions={actions} />
        </main>
      ) : (
        <main className="main main-code">
          <CodeReview snapshot={snapshot} source={reviewSource} onSource={setReviewSource} actions={actions} />
        </main>
      )}
      {selection && (
        <Drawer
          snapshot={snapshot}
          selection={selection}
          actions={actions}
          onClose={() => setSelection(null)}
        />
      )}
      <ConfirmDialog confirmation={confirmation} onClose={() => setConfirmation(null)} />
      <Toasts toasts={toasts} />
    </div>
  );
}
