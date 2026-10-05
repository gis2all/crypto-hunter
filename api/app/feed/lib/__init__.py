"""跨模块的原语: 这些东西只写一次。

对应 AIHOT 的 `lib/*`（`ids.ts`、`cache.ts`、`cursor.ts`、`text.ts`、
`shutdown.ts`、`contracts/time.ts`、`contracts/http-policy.ts`）。放在一个包里，
因为它们共同的点是“不依赖业务”—— `feed/*` 可以读它，它不读 `feed/*`。
"""
