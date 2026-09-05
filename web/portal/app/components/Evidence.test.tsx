import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Evidence } from "./Evidence";

afterEach(cleanup);

describe("Evidence", () => {
  it("renders nothing without entries", () => {
    const { container } = render(<Evidence entries={[]} />);
    expect(container.innerHTML).toBe("");
  });

  it("distinguishes a sealed kernel receipt from an activity log by kind, not by note text", () => {
    const { container } = render(
      <Evidence
        entries={[
          { kind: "kernel_receipt", id: "rcpt-1", note: "restock created an inventory item" },
          { kind: "activity_log", id: "log-1", note: "restock created an inventory item" },
        ]}
      />,
    );
    const kernel = container.querySelector(".evidence.kernel .evidence-label");
    const log = container.querySelector(".evidence.log .evidence-label");
    expect(kernel?.textContent).toContain("Sealed kernel receipt");
    expect(log?.textContent).toContain("Activity log");
    expect(kernel?.textContent).not.toBe(log?.textContent);
  });
});
