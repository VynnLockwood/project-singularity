import { notFound } from "next/navigation";
import { HomeClient } from "@/components/HomeClient";
import { isAssetPath } from "@/lib/route";

/** Every place in the app is this one page; the client reads the path (lib/route.ts). */
export default async function Page({ params }: { params: Promise<{ path?: string[] }> }) {
  if (isAssetPath((await params).path)) notFound();
  return <HomeClient />;
}
