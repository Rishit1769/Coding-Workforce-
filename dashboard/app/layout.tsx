import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Coding Workforce",
  description: "Live state of the local multi-agent engineering workforce",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
