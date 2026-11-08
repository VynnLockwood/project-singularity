import type { Metadata } from "next";
import "./globals.css";
import { Cosmos } from "@/components/Cosmos";

export const metadata: Metadata = {
  title: "Singularity",
  description: "Design master data with a prompt.",
};

// without this a phone renders the desktop layout zoomed out and no CSS breakpoint applies
export const viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Cosmos />
        {children}
      </body>
    </html>
  );
}
