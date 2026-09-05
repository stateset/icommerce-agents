import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StagedChange } from "../../lib/types";

const api = vi.hoisted(() => ({
  approveChange: vi.fn(),
  fetchReconciliation: vi.fn(),
  resolveReconciliation: vi.fn(),
  startReconciliation: vi.fn(),
}));
vi.mock("../../lib/api", () => api);
// The panel embeds the stablecoin operations panel, which is exercised on its own.
vi.mock("./StablecoinPayments", () => ({ StablecoinPayments: () => null }));

import { ChangesPanel } from "./ChangesPanel";

const DIGEST = `sha256:${"a".repeat(64)}`;

function staged(overrides: Partial<StagedChange> = {}): StagedChange {
  return {
    change_id: "chg-1",
    kind: "price_update",
    status: "staged",
    summary: "Update price for TENT-RIDGE-TAN",
    items: [{ target: "TENT-RIDGE-TAN", field: "price", before: "219.00", after: "199.00" }],
    created_at: "2026-09-05T10:00:00Z",
    created_by: "assistant",
    guardrail_notes: [],
    proposal_digest: DIGEST,
    apply_control: null,
    evidence: [],
    ...overrides,
  };
}

beforeEach(() => {
  api.approveChange.mockReset();
});
afterEach(cleanup);

describe("ChangesPanel", () => {
  it("approves a staged change with its exact proposal digest and refreshes", async () => {
    api.approveChange.mockResolvedValue({ ok: true, data: { change_id: "chg-1", approved_by: "op" } });
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    render(<ChangesPanel changes={{ "chg-1": staged() }} onRefresh={onRefresh} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(api.approveChange).toHaveBeenCalledWith("chg-1", DIGEST));
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(screen.getByText("Approved -- apply it via chat")).toBeTruthy();
  });

  it("shows the host's refusal verbatim and does not mark the change approved", async () => {
    api.approveChange.mockResolvedValue({ ok: false, error: "proposal digest changed" });
    render(<ChangesPanel changes={{ "chg-1": staged() }} onRefresh={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByText("proposal digest changed")).toBeTruthy());
    expect(screen.queryByText("Approved -- apply it via chat")).toBeNull();
  });

  it("never offers approval for a change without a proposal digest", () => {
    render(
      <ChangesPanel changes={{ "chg-1": staged({ proposal_digest: null }) }} onRefresh={vi.fn()} />,
    );
    const button = screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(api.approveChange).not.toHaveBeenCalled();
  });

  it("renders an applied change's evidence rows from the structured field", () => {
    const applied = staged({
      status: "applied",
      applied_by: "op",
      evidence: [{ kind: "kernel_receipt", id: "rcpt-9", note: "engine governed the restock" }],
    });
    const { container } = render(
      <ChangesPanel changes={{ "chg-1": applied }} onRefresh={vi.fn()} />,
    );
    expect(container.querySelector(".evidence.kernel")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});
