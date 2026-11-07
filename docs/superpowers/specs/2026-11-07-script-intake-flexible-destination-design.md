# Script intake: flexible destination, external refs, agent-driven MCP

**Date:** 2026-11-07
**Status:** approved
**Repo:** VynnLockwood/project-singularity

## Why

`load_subdistricts.sql` (441 KB, 7,720 rows) is valid PostgreSQL but is rejected:

```
[parse] line 6:    only INSERT ... VALUES is supported
[parse] line 7733: SELECT is not allowed in a script
[parse] line 7734: SELECT is not allowed in a script
```

...[truncated]