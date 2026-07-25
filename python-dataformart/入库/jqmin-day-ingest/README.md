# jqmin-day-ingest

逐日回测 → 滚动提取日志 → 解析入库。详见 [SKILL.md](./SKILL.md)。

```bash
python scripts/run_backfill.py --from 2026-07-13 --to 2026-07-01 --dry-run
python scripts/run_backfill.py --from 2026-07-13 --to 2026-07-01
```
