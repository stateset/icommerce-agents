import type { ChangeEvidence } from "../../lib/types";

/** The host attaches this structured field to every `change_update` event
 * (`host/app.py::_with_change_evidence`), parsing `guardrail_notes` server side exactly
 * once. The UI switches on `entry.kind`, never on the note text -- a wording change to
 * the note cannot silently break this distinction the way a browser-side regex would. */
export function Evidence({ entries }: { entries: ChangeEvidence[] | undefined }) {
  if (!entries || !entries.length) return null;
  return (
    <div>
      {entries.map((entry, index) => (
        <div className={`evidence ${entry.kind === "kernel_receipt" ? "kernel" : "log"}`} key={index}>
          <span className="evidence-label">
            {entry.kind === "kernel_receipt"
              ? "Sealed kernel receipt -- engine governed"
              : "Activity log -- engine did not govern this"}
          </span>
          {entry.note}
        </div>
      ))}
    </div>
  );
}
