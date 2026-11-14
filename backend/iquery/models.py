from django.conf import settings
from django.db import models
from pgvector.django import HnswIndex, VectorField

DIMENSIONS = 384  # same model as search; see search.models


class QueryExample(models.Model):
    """A comment -> SQL pair someone accepted and ran. Candidates are never used; two confirmations make one active."""

    schema = models.CharField(max_length=63)
    comments = models.TextField()
    sql = models.TextField()
    sql_norm = models.TextField()
    values = models.JSONField(default=list)
    embedding = VectorField(dimensions=DIMENSIONS)
    status = models.CharField(max_length=9, default="candidate")  # candidate | active
    confirmations = models.JSONField(default=list)               # [{"user": id, "day": "YYYY-MM-DD"}]
    use_count = models.IntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iquery_examples"
        indexes = [HnswIndex(name="iqueryExampleHnsw", fields=["embedding"], m=16, ef_construction=64,
                             opclasses=["vector_cosine_ops"])]


class SuggestCall(models.Model):
    """One row per suggestion request, for the per-user rate limit."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iquery_calls"
        indexes = [models.Index(fields=["user", "created_at"], name="iqueryCallUserTime")]
