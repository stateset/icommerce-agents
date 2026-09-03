import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ACME Supply Merchant Portal",
  description: "Chat with the ACME Supply merchant assistant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
