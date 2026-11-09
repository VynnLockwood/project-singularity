from django.db import models
from pgvector.django import HnswIndex, VectorField

DIMENSIONS = 384  # multilingual-e5-small; changing the model means a new migration and a rebuild


class SchemaEmbedding(models.Model):
    """One searchable document per table and per column of `master`."""

    schema = models.CharField(max_length=63, default="master")
    kind = models.CharField(max_length=8)  # table | column
    table_name = models.CharField(max_length=63, db_index=True)
    column_name = models.CharField(max_length=63, blank=True)
    content = models.TextField()
    content_hash = models.CharField(max_length=64)
    embedding = VectorField(dimensions=DIMENSIONS)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "schema_embeddings"
        constraints = [models.UniqueConstraint(fields=["schema", "kind", "table_name", "column_name"], name="schemaEmbeddingUnique")]
        indexes = [HnswIndex(name="schemaEmbeddingHnsw", fields=["embedding"], m=16, ef_construction=64,
                             opclasses=["vector_cosine_ops"])]
