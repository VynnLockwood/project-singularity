# Client-Side Hydration Error Report

## Issue
**Application error: a client-side exception has occurred while loading singularity.lunariankids.com**

## Environment
- **URL**: https://singularity.lunariankids.com/
- **Stack**: Next.js 15.5.29 (App Router) + React 19
- **Deployment**: Docker Compose on VPS, Cloudflare Tunnel (port 8088)
- **Auth**: Google OAuth (configured, working)

## Symptoms
1. Server renders HTML correctly with "Loading..." state
2. Static assets (CSS, JS chunks) load with 200 OK
3. Browser console shows hydration/client-side error
4. OAuth flow works end-to-end when tested via curl:
   - `/api/auth/google/login` → redirects to Google with correct HTTPS callback
   - `/api/auth/google/callback` → validates state, exchanges code
   - `/api/me` → returns user data when session cookie valid
5. Session persistence works (Django DB-backed sessions)

## Root Cause Hypothesis
**Hydration mismatch** between server-rendered HTML and client-side React tree.

### Evidence
- Server renders: `<main class="center"><p class="muted">Loading…</p></main>`
- Client `HomeClient` component expects to hydrate this but may have:
  - Different initial state (useEffect runs after hydration)
  - Mismatched `getMe()` promise resolution timing
  - Suspense boundary issues with `use client` component

### Key Code Path
```tsx
// HomeClient.tsx
useEffect(() => {
  getMe().then(setMe).catch(() => setMe(null)).finally(() => setLoaded(true));
}, []);

if (!loaded) return <Loading />;
if (me) return <AppShell />;
return <Home loginUrl={googleLoginUrl} />;
```

The server renders the `!loaded` branch ("Loading...") but client hydration may fail if:
1. `getMe()` resolves differently on client vs server
2. React 19 strict mode double-invokes effects in dev (not in prod)
3. Suspense/async boundary mismatch

## Working Endpoints (Verified via curl)
| Endpoint | Status |
|----------|--------|
| `GET /` | 200 - HTML with "Loading..." |
| `GET /_next/static/*` | 200 - All chunks load |
| `GET /api/auth/google/login` | 302 → Google OAuth |
| `GET /api/auth/google/callback` | 400/302 (state validation works) |
| `GET /api/me` | 401 unauth / 200 with valid session |
| `GET /api/csrf` | 200 - Sets CSRF cookie |

## OAuth Configuration (Working)
- **Callback URL**: `https://singularity.lunariankids.com/api/auth/google/callback`
- **Client ID**: `885040250992-sck60ie3m8h4qotsfir8u376ii4q8j0p.apps.googleusercontent.com`
- **access_type**: `offline` (refresh token)
- **hd parameter**: Omitted (correct for gmail.com)
- **seedAdmin**: `nolanrobinson490@gmail.com` → becomes "approver" on first sign-in

## Files Modified During Deployment
1. `singularity.yaml` - Updated seedAdmin and allowedDomains
2. `.env` - Generated secrets + Google OAuth credentials
3. `docker-compose.yml` - nginx on port 8088 (not 80/443)
4. `nginx/nginx.conf` - Removed SSL, forced `X-Forwarded-Proto: https`
5. `backend/accounts/google.py` - Added `access_type: offline`
6. `backend/accounts/views.py` - Skip `hd` for gmail.com
7. `backend/config/settings.py` - Added `CSRF_TRUSTED_ORIGINS`
8. Cloudflare Tunnel config - Added ingress for singularity.lunariankids.com → 127.0.0.1:8088

## Next Steps for Debugging
1. **Open browser DevTools Console** at https://singularity.lunariankids.com/
2. **Capture exact error message** (React error boundary, hydration mismatch, chunk load error)
3. **Check Network tab** for failed chunk loads (404 on hashed filenames)
4. **Try hard refresh** (Ctrl+Shift+R) to bypass service worker/cache
5. **Disable browser extensions** (especially React DevTools, ad blockers)

## Quick Fixes to Try
```bash
# 1. Rebuild frontend with fresh cache
cd /root/project-singularity && docker compose build --no-cache web && docker compose up -d web

# 2. Check Next.js build output for warnings
docker logs singularity-web-1

# 3. Verify chunk filenames match between HTML and filesystem
# 4. Check for missing `suppressHydrationWarning` on dynamic content
```

## Status
- ✅ Infrastructure deployed and healthy
- ✅ OAuth flow functional (tested via curl)
- ✅ Session management working
- ❌ Client-side hydration error in browser
- 🔍 Needs browser console error for precise diagnosis