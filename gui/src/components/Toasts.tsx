export interface Toast {
  id: number;
  text: string;
  tone: "ok" | "error";
}

export function Toasts({ toasts }: { toasts: Toast[] }) {
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <p key={t.id} className={`toast ${t.tone}`}>
          {t.text}
        </p>
      ))}
    </div>
  );
}
