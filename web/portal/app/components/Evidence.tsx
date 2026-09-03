/** Every applied change's `guardrail_notes` carries the write's evidence, in one of two
 * shapes: "governed via kernel command ...; sealed receipt <id>" for a write the engine's
 * kernel vouched for, or "applied via direct binding write; activity log <id>" for one it
 * only logged. This tells the two apart so the portal never renders them as the same grey
 * text -- that distinction is the whole point of the repo. */
export interface EvidenceEntry {
  kind: "kernel" | "log";
  id: string | null;
  note: string;
}

export function parseEvidence(notes: string[]): EvidenceEntry[] {
  return notes
    .map((note): EvidenceEntry | null => {
      const receiptMatch = note.match(/sealed receipt (\S+)/);
      if (receiptMatch) return { kind: "kernel", id: receiptMatch[1], note };
      const logMatch = note.match(/activity log (\S+)/);
      if (logMatch) return { kind: "log", id: logMatch[1], note };
      return null;
    })
    .filter((entry): entry is EvidenceEntry => entry !== null);
}

export function Evidence({ entries }: { entries: EvidenceEntry[] }) {
  if (!entries.length) return null;
  return (
    <div>
      {entries.map((entry, index) => (
        <div className={`evidence ${entry.kind}`} key={index}>
          <span className="evidence-label">
            {entry.kind === "kernel" ? "Sealed kernel receipt -- engine governed" : "Activity log -- engine did not govern this"}
          </span>
          {entry.note}
        </div>
      ))}
    </div>
  );
}
