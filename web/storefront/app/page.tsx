"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAgentTurn, useSession } from "web-shared";
import type { AgentEvent } from "web-shared";
import { api, capabilities, fetchCart, fetchOrders, healthy, UNREACHABLE } from "../lib/api";
import type { CartPayload, Order, ProductsPayload } from "../lib/types";
import { ProductCards } from "./components/ProductCards";
import { CartPanel } from "./components/CartPanel";
import { OrdersPanel } from "./components/OrdersPanel";

export default function StorefrontPage() {
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [assistant, setAssistant] = useState<"checking" | "available" | "unconfigured">(
    "checking",
  );
  const [stablecoinAvailable, setStablecoinAvailable] = useState(false);
  const [directCheckoutAvailable, setDirectCheckoutAvailable] = useState(false);
  const [cart, setCart] = useState<CartPayload | null>(null);
  const [orders, setOrders] = useState<Order[] | null>(null);
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
          setDirectCheckoutAvailable(caps?.direct_checkout === "available");
        }
      });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const session = useSession(api);

  // Live store state: fetched as soon as a session exists, independent of whether an
  // assistant is configured -- this is what makes a keyless tour's order visible on
  // first load, with no typing.
  const refreshCart = useCallback(() => {
    fetchCart().then((next) => {
      if (next) setCart(next);
    });
  }, []);
  const refreshOrders = useCallback(() => {
    fetchOrders().then((next) => {
      if (next) setOrders(next.orders);
    });
  }, []);
  useEffect(() => {
    if (!session.sessionId) return;
    refreshCart();
    refreshOrders();
  }, [refreshCart, refreshOrders, session.sessionId]);

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

  const ready = reachable === true && assistant === "available" && turn.ready;
  const sideCol = (
    <div className="cart-col">
      <CartPanel
        cart={cart}
        busy={turn.busy}
        stablecoinAvailable={stablecoinAvailable}
        directCheckoutAvailable={directCheckoutAvailable}
        sessionId={session.sessionId}
        onPlaced={refreshOrders}
      />
      <OrdersPanel orders={orders} />
    </div>
  );

  if (reachable === true && assistant === "unconfigured") {
    return (
      <main className="app">
        <section className="chat-col">
          <header className="header">
            <h1>ACME Supply</h1>
            <span className="sub">shopping assistant</span>
            <span className="status-pill down">assistant unconfigured</span>
          </header>
          <div className="assistant-unavailable">
            <h2>The assistant is unavailable</h2>
            <p>
              No model is configured for this deployment, so there is no chat here. Set{" "}
              <code>ANTHROPIC_API_KEY</code> and reload this page to bring the assistant back.
            </p>
            <p>
              The store itself is real: the bag and order history to the right came from actual
              writes through the engine, including anything a keyless tour run placed. Nothing
              here is a mock.
            </p>
          </div>
        </section>
        {sideCol}
      </main>
    );
  }

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
      {sideCol}
    </main>
  );
}
