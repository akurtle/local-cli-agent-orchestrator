import type { Actions } from "../App";
import type { AttentionItem, Selection } from "../types";

// Shown only while something is waiting on the human. Each card is one
// decision; the drawer holds the detail and the buttons.

export function NeedsYou({
  items,
  selection,
  actions,
}: {
  items: AttentionItem[];
  selection: Selection;
  actions: Actions;
}) {
  if (!items.length) return null;
  return (
    <section className="needs-you" aria-labelledby="needs-you-title">
      <h2 id="needs-you-title">
        {items.length === 1 ? "1 thing needs you" : `${items.length} things need you`}
      </h2>
      <ul>
        {items.map((item) => {
          const selected = selection?.kind === "attention" && selection.key === item.key;
          return (
            <li key={item.key}>
              <button
                className={`need kind-${item.kind} ${selected ? "selected" : ""}`}
                aria-pressed={selected}
                onClick={() => actions.select({ kind: "attention", key: item.key })}
              >
                <span className="need-label">{item.label}</span>
                <span className="need-title">{item.title}</span>
                <span className="need-reason">{item.reason}</span>
                {item.holding_up.length > 0 && (
                  <span className="need-holds">Holding up {item.holding_up.join(", ")}</span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
