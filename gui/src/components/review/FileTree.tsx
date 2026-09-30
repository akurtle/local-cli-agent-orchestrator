import type { FileDiff } from "../../types";

// Changed files grouped by folder, like VS Code's source control list: a
// status letter, the file name, and how much it changed.

const LETTER: Record<FileDiff["status"], string> = {
  modified: "M",
  added: "A",
  deleted: "D",
  renamed: "R",
};

const STATUS_WORD: Record<FileDiff["status"], string> = {
  modified: "modified",
  added: "added",
  deleted: "deleted",
  renamed: "renamed",
};

export function FileTree({
  files,
  selected,
  onSelect,
}: {
  files: FileDiff[];
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const folders = new Map<string, FileDiff[]>();
  for (const file of files) {
    const slash = file.path.lastIndexOf("/");
    const folder = slash === -1 ? "" : file.path.slice(0, slash);
    folders.set(folder, [...(folders.get(folder) ?? []), file]);
  }
  const ordered = [...folders.entries()].sort(([a], [b]) => a.localeCompare(b));

  return (
    <nav className="file-tree" aria-label="Changed files">
      {ordered.map(([folder, members]) => (
        <div key={folder} className="tree-folder">
          <p className="tree-folder-name" title={folder || "project root"}>
            {folder || "project root"}
          </p>
          <ul>
            {members.map((file) => {
              const name = file.path.slice(file.path.lastIndexOf("/") + 1);
              const isSelected = file.path === selected;
              return (
                <li key={file.path}>
                  <button
                    className={`tree-file ${isSelected ? "selected" : ""}`}
                    aria-current={isSelected ? "true" : undefined}
                    onClick={() => onSelect(file.path)}
                    title={`${file.path} (${STATUS_WORD[file.status]})`}
                  >
                    <span className={`status-letter s-${file.status}`} aria-label={STATUS_WORD[file.status]}>
                      {LETTER[file.status]}
                    </span>
                    <span className={`tree-name ${file.status === "deleted" ? "struck" : ""}`}>{name}</span>
                    <span className="tree-counts">
                      {file.binary ? (
                        "bin"
                      ) : (
                        <>
                          {file.added > 0 && <span className="plus">+{file.added}</span>}
                          {file.removed > 0 && <span className="minus">−{file.removed}</span>}
                        </>
                      )}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}
