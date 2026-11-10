import type { Metadata } from "next";
import "@fontsource/chakra-petch/600.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans-thai/400.css";
import "@fontsource/ibm-plex-sans-thai/500.css";
import "@fontsource/ibm-plex-mono/400.css";
import "./tokens.css";
import "./base.css";
import "./shell.css";
import "./ui.css";
import "./legacy.css";
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
