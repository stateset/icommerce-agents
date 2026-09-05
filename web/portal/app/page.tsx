"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAgentTurn, useSession } from "web-shared";
import type { AgentEvent } from "web-shared";
import {
  api,
  capabilities,
  fetchChanges,
  healthy,
  UNREACHABLE,
} from "../lib/api";
import type { StagedChange } from "../lib/types";
import { ChangesPanel } from "./components/ChangesPanel";

export default function PortalPage() {
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [assistant, setAssistant] = useState<
    "checking" | "available" | "unconfigured"
  >("checking");
  const [changes, setChanges] = useState<Record<string, StagedChange>>({});
  const [stablecoinAvailable, setStablecoinAvailable] = useState(false);
  const [stablecoinRefundsAvailable, setStablecoinRefundsAvailable] =
    useState(false);
  const [input, setInput] = useState("");
  const transcriptRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    healthy().then((ok) => {
      if (cancelled) return;
      setReachable(ok);
      if (!ok) return;
      capabilities().then((caps) => {
        if (!cancelled) {
          setAssistant(caps?.assistant ?? "unconfigured");
          setStablecoinAvailable(caps?.stablecoin_checkout === "available");
          setStablecoinRefundsAvailable(
            caps?.stablecoin_refunds === "available",
          );
        }
      });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const session = useSession(api);

  const refreshChanges = useCallback(async () => {
    const list = await fetchChanges();
    if (!list) return;
    setChanges(
      Object.fromEntries(list.map((change) => [change.change_id, change])),
    );
  }, []);

  // Live store state: fetched as soon as a session exists, independent of whether an
  // assistant is configured. This is the artifact the whole repo exists to show, and it
  // must be visible on first load after a keyless tour run, with no typing.
  useEffect(() => {
    if (!session.sessionId) return;
    void refreshChanges();
  }, [refreshChanges, session.sessionId]);

  const turn = useAgentTurn(api, {
    sessionId: reachable ? session.sessionId : null,
    unreachable: UNREACHABLE,
    onEvent: (event: AgentEvent) => {
      if (event.type === "change_update") {
        const change = event.data.change as StagedChange;
        setChanges((prev) => ({ ...prev, [change.change_id]: change }));
        // Stream events carry the upstream change shape. Refresh immediately to attach
        // the adapter's digest, durable approval state, audit history, and recovery time.
        void refreshChanges();
      }
    },
  });

  useEffect(() => {
    transcriptRef.current?.scrollTo({
      top: transcriptRef.current.scrollHeight,
    });
  }, [turn.items]);

  if (reachable === false) {
    return (
      <main className="app">
        <div className="chat-col">
          <div className="unreachable">
            <strong>API not reachable.</strong>
            <p>
              Start the host (uvicorn on :8000) and reload this page. The portal
              needs it for every request -- there is nothing this page can show
              without it.
            </p>
          </div>
        </div>
      </main>
    );
  }

  const ready = reachable === true && assistant === "available" && turn.ready;

  if (reachable === true && assistant === "unconfigured") {
    return (
      <main className="app">
        <section className="chat-col">
          <header className="header">
            <h1>ACME Supply</h1>
            <span className="sub">merchant portal</span>
            <span className="status-pill down">assistant unconfigured</span>
          </header>
          <div className="assistant-unavailable">
            <h2>The assistant is unavailable</h2>
            <p>
              No model is configured for this deployment, so there is no chat
              here. Set <code>ANTHROPIC_API_KEY</code> and reload this page to
              bring the assistant back.
            </p>
            <p>
              The staged and applied changes to the right are real writes
              against the engine, including anything a keyless tour run staged
              and applied -- each carrying its own sealed kernel receipt or
              activity-log entry as evidence. Nothing here is a mock.
            </p>
          </div>
        </section>
        <ChangesPanel
          changes={changes}
          onRefresh={refreshChanges}
          stablecoinAvailable={stablecoinAvailable}
          stablecoinRefundsAvailable={stablecoinRefundsAvailable}
          sessionId={session.sessionId}
        />
      </main>
    );
  }

  return (
    <main className="app">
      <section className="chat-col">
        <header className="header">
          <h1>ACME Supply</h1>
          <span className="sub">merchant portal</span>
          <span
            className={`status-pill ${reachable === null ? "" : reachable ? "ok" : "down"}`}
          >
            {reachable === null
              ? "checking..."
              : ready
                ? "connected"
                : "starting session..."}
          </span>
        </header>
        <div className="transcript" ref={transcriptRef}>
          {turn.items.length === 0 ? (
            <div className="empty-state">
              <h2>Ask about the business, or stage a change</h2>
              <p>
                Try &ldquo;how did revenue do this week&rdquo; or &ldquo;raise
                the price of SKU-1002 by 10%&rdquo;.
              </p>
            </div>
          ) : null}
          {turn.items.map((item, index) =>
            item.kind === "user" ? (
              <div className="msg user" key={index}>
                {item.text}
              </div>
            ) : (
              <div className="msg assistant" key={index}>
                {item.segments.map((segment, si) => {
                  if (segment.type === "text")
                    return <span key={si}>{segment.text}</span>;
                  if (segment.type === "error")
                    return (
                      <p key={si} style={{ color: "var(--danger)" }}>
                        {segment.text}
                      </p>
                    );
                  return (
                    <p
                      key={si}
                      style={{ color: "var(--ink-soft)", fontSize: 12.5 }}
                    >
                      [{segment.block.component}]
                    </p>
                  );
                })}
                {item.activity ? (
                  <div className="activity">{item.activity}</div>
                ) : null}
              </div>
            ),
          )}
        </div>
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            const text = input.trim();
            if (!text) return;
            setInput("");
            void turn.send(text);
          }}
        >
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder={ready ? "Ask the assistant..." : "Connecting..."}
            disabled={!ready || turn.busy}
          />
          <button type="submit" disabled={!ready || turn.busy || !input.trim()}>
            Send
          </button>
        </form>
      </section>
      <ChangesPanel
        changes={changes}
        onRefresh={refreshChanges}
        stablecoinAvailable={stablecoinAvailable}
        stablecoinRefundsAvailable={stablecoinRefundsAvailable}
        sessionId={session.sessionId}
      />
    </main>
  );
}
