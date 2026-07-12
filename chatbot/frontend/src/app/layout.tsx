import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Porto Agent",
  description: "RAG and PRD decomposition workbench",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full">{children}</body>
    </html>
  );
}
