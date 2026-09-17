<!-- takaro-maint: kind=dashboard v=1 -->

State for `takaro-maint scan`. Everything between the dashboard markers is rewritten by the command (compare-and-swap: a concurrent edit makes the run stop rather than overwrite); edit anything else freely. Closing this issue resets nothing: the next scan still finds it by its marker, closed or not, and rewrites it in place.

<!-- takaro-maint:dashboard:begin -->
## Sources

| Source | Status | Head | Seen | Last success | Last error |
| --- | --- | --- | --- | --- | --- |
| minecraft/mojang-meta | ok | release 26.3 | 2 | 2026-09-17T12:00:00Z | — |

## Work

| Work | Issue | State | Since |
| --- | --- | --- | --- |
| provider=mojang component=minecraft branch=release rev=26.3 | #187 | detected | 2026-09-17T12:00:00Z |

## State

```json
{
 "lastSuccess": "2026-09-17T12:00:00Z",
 "schema": "takaro-maint-dashboard/1",
 "sources": {
  "minecraft/mojang-meta": {
   "checkpoint": {
    "at": "2026-09-17T12:00:00Z",
    "floor": null,
    "seen": [
     [
      "26.3",
      "2026-09-15T11:23:02+00:00"
     ],
     [
      "26.2",
      "2026-06-16T12:03:33+00:00"
     ]
    ]
   },
   "heads": {
    "release": "26.3"
   },
   "history": "full",
   "lastError": null,
   "lastSuccess": "2026-09-17T12:00:00Z",
   "status": "ok"
  }
 },
 "targets": {
  "minecraft": [
   {
    "id": "fabric-26.2",
    "platform": "fabric",
    "revision": "26.2",
    "status": "maintained"
   }
  ]
 },
 "updatedAt": "2026-09-17T12:00:00Z",
 "work": {
  "provider=mojang component=minecraft branch=release rev=26.3": {
   "issue": 187,
   "since": "2026-09-17T12:00:00Z",
   "state": "detected"
  }
 }
}
```
<!-- takaro-maint:dashboard:end -->
