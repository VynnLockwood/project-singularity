import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Singularity",
  description: "Design master data with a prompt.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
