export type Role = "viewer" | "editor" | "approver";

export interface Me {
  id: number;
  email: string;
  name: string;
  role: Role;
}

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";

/** Returns the signed-in user, or null when not signed in. */
export async function getMe(): Promise<Me | null> {
  const res = await fetch(`${BASE}/me`, { credentials: "same-origin" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`GET /me failed: ${res.status}`);
  return (await res.json()) as Me;
}

export const googleLoginUrl = `${BASE}/auth/google/login`;

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/csrf`, { credentials: "same-origin" }); // makes sure the CSRF cookie exists
  await fetch(`${BASE}/auth/logout`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRFToken": csrfToken() },
  });
}
