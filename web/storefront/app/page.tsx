"use client";

import { useEffect, useRef, useState } from "react";
import { useAgentTurn, useSession } from "web-shared";
import type { AgentEvent } from "web-shared";
import { api, healthy, UNREACHABLE } from "../lib/api";
import type { CartPayload, ProductsPayload } from "../lib/types";
import { ProductCards } from "./components/ProductCards";
import { CartPanel } from "./components/CartPanel";

export default function StorefrontPage() {
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [cart, setCart] = useState<CartPayload | null>(null);
  const [input, setInput] = useState("");
  const transcriptRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    healthy().then((ok) => {
      if (!cancelled) setReachable(ok);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const session = useSession(api);
  const turn = useAgentTurn(api, {
    sessionId: reachable ? session.sessionId : null,
    unreachable: UNREACHABLE,
    onEvent: (event: AgentEvent) => {
      if (event.type === "cart_update") {
        setCart(event.data.cart as CartPayload);
      }
    },
  });

  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight });
  }, [turn.items]);

  if (reachable === false) {
    return (
      <main className="app">
        <div className="chat-col">
          <div className="unreachable">
            <strong>API not reachable.</strong>
            <p>
              Start the host (uvicorn on :8000) and reload this page. The storefront needs it for
              every request -- there is nothing this page can show without it.
            </p>
          </div>
        </div>
      </main>
    );
  }

  const ready = reachable === true && turn.ready;

  return (
    <main className="app">
      <section className="chat-col">
        <header className="header">
          <h1>ACME Supply</h1>
          <span className="sub">shopping assistant</span>
          <span className={`status-pill ${reachable === null ? "" : reachable ? "ok" : "down"}`}>
            {reachable === null ? "checking..." : ready ? "connected" : "starting session..."}
          </span>
        </header>
        <div className="transcript" ref={transcriptRef}>
          {turn.items.length === 0 ? (
            <div className="empty-state">
              <h2>Ask for anything ACME Supply sells</h2>
              <p>Try &ldquo;show me your best hiking boots&rdquo; or &ldquo;what&apos;s in my order history?&rdquo;</p>
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
                  if (segment.type === "text") return <span key={si}>{segment.text}</span>;
                  if (segment.type === "error")
                    return (
                      <p key={si} style={{ color: "var(--danger)" }}>
                        {segment.text}
                      </p>
                    );
                  if (segment.block.component === "products") {
                    return (
                      <ProductCards
                        key={si}
                        payload={segment.block.payload as ProductsPayload}
                        onAdded={(next) => setCart(next as CartPayload)}
                      />
                    );
                  }
                  return (
                    <p key={si} style={{ color: "var(--ink-soft)", fontSize: 12.5 }}>
                      [{segment.block.component}]
                    </p>
                  );
                })}
                {item.activity ? <div className="activity">{item.activity}</div> : null}
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
      <CartPanel cart={cart} busy={turn.busy} />
    </main>
  );
}
