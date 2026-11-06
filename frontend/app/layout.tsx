import type { Metadata } from "next";
import "./globals.css";
import { Cosmos } from "@/components/Cosmos";

export const metadata: Metadata = {
  title: "Singularity",
  description: "Design master data with a prompt.",
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
