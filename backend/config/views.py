from django.db import connection
from django.http import JsonResponse


def health(request):
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return JsonResponse({"status": "ok", "db": True})
    except Exception:
        return JsonResponse({"status": "error", "db": False}, status=503)
