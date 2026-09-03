import { AgentApi } from "web-shared";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** `/merchant/session`, `/merchant/chat`, `/merchant/changes/{id}/approve` all line up
 * with the host's own routes under this prefix. */
export const api = new AgentApi(API_URL, "/merchant");

export const UNREACHABLE =
  "Couldn't reach the ACME Supply API. Start it and try again.";

export async function healthy(): Promise<boolean> {
  try {
    const response = await fetch(`${API_URL}/healthz`, { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

/** The only place approval happens; the operator comes from the session binding on the
 * host, never from this call's body. */
export async function approveChange(
  changeId: string,
): Promise<{ change_id: string; approved_by: string } | null> {
  return api.post<{ change_id: string; approved_by: string }>(
    `/changes/${encodeURIComponent(changeId)}/approve`,
  );
}
