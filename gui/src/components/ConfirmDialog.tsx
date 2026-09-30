import { useEffect, useRef } from "react";

export interface Confirmation {
  title: string;
  body: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
}

/** A real modal <dialog>: traps focus, Esc cancels, the safe choice is focused. */
export function ConfirmDialog({
  confirmation,
  onClose,
}: {
  confirmation: Confirmation | null;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (confirmation && !dialog.open) {
      dialog.showModal();
      cancel.current?.focus();
    } else if (!confirmation && dialog.open) {
      dialog.close();
    }
  }, [confirmation]);

  return (
    <dialog ref={ref} className="confirm" onClose={onClose} aria-labelledby="confirm-title">
      {confirmation && (
        <form
          method="dialog"
          onSubmit={(event) => {
            event.preventDefault();
            confirmation.onConfirm();
            onClose();
          }}
        >
          <h2 id="confirm-title">{confirmation.title}</h2>
          <p>{confirmation.body}</p>
          <div className="confirm-buttons">
            <button type="button" ref={cancel} className="button quiet" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className={`button ${confirmation.danger ? "danger" : "primary"}`}>
              {confirmation.confirmLabel}
            </button>
          </div>
        </form>
      )}
    </dialog>
  );
}
